"""针对排障平台核心机制与 DI 架构的自动化回归单元测试
覆盖：
1. EvidenceCollector 敏感数据脱敏 (redact_text) 与确定性证据编号签发 (build_evidence)
2. ReportDraft -> valid_refs 防幻觉拦截校验器与 SHA-256 指纹幂等判重 (fingerprint)
3. RRF (k=60) 倒数秩融合算法 (rrf_fuse_runbooks)
4. 依赖注入链路 (app/clients/ + app/api/deps.py + app/repositories/ + IncidentGraphContext)
5. LangGraph 主图 + 三大子图全链路异步执行 (astream / ainvoke 传入 context=IncidentGraphContext)
"""

import asyncio
from typing import Any
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.evidence_collector import build_evidence, redact_text
from app.core.report_verifier import verify_and_finalize_report
from app.graphs.context import IncidentGraphContext
from app.graphs.crag_subgraph import rrf_fuse_runbooks
from app.graphs.main_graph import build_main_incident_graph
from app.graphs.state import IncidentMainState
from app.models import Base
from app.repositories.mysql.incident_repo import IncidentRepository
from app.repositories.mysql.mall_probe_repo import MallProbeRepository
from app.repositories.mysql.runbook_repo import RunbookRepository
from app.schemas.incident_schemas import Claim, ReportDraft
from app.services.knowledge_service import INITIAL_RUNBOOKS
from app.services.range_service import INITIAL_RUNTIME_LOGS


class _DummyEmbedding:
    async def aembed_query(self, text: str) -> list[float]:
        return [0.1] * 1024

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 1024 for _ in texts]


class _FakeRuntimeLogESRepo:
    def __init__(self) -> None:
        self.logs = list(INITIAL_RUNTIME_LOGS)

    async def write_log(self, log_doc: dict[str, Any]) -> None:
        self.logs.insert(0, log_doc)

    async def search_logs(
        self,
        keyword: str = "",
        api_path: str = "",
        user_id: str = "",
        level: str = "",
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        out = []
        for log in self.logs:
            if user_id and str(log.get("user_id")) != str(user_id):
                continue
            if api_path and api_path not in log.get("api_path", ""):
                continue
            out.append(log)
        return out[:limit] or self.logs[:limit]

    async def get_logs_by_trace_id(self, trace_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return [log for log in self.logs if log.get("trace_id") == trace_id][:limit]


class _FakeRunbookESRepo:
    def __init__(self) -> None:
        self.docs = list(INITIAL_RUNBOOKS)

    async def index_runbook(self, runbook_doc: dict[str, Any]) -> None:
        self.docs.insert(0, runbook_doc)

    async def bulk_index(self, docs: list[dict[str, Any]]) -> None:
        self.docs = list(docs)

    async def search_bm25(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        q_lower = query.lower()
        scored = []
        for doc in self.docs:
            score = 5.0 if "readtimeouterror" in q_lower and doc["incident_code"] == "INC-2026-089" else 1.0
            scored.append({**doc, "bm25_score": score})
        scored.sort(key=lambda d: d["bm25_score"], reverse=True)
        return scored[:limit]


class _FakeRunbookQdrantRepo:
    def __init__(self) -> None:
        self.docs = list(INITIAL_RUNBOOKS)

    async def upsert_runbook(self, vector: list[float], payload: dict[str, Any]) -> None:
        self.docs.insert(0, payload)

    async def upsert_vectors(
        self, vectors: list[list[float]], payloads: list[dict[str, Any]]
    ) -> None:
        self.docs = list(payloads)

    async def search_vector(
        self, query_vector: list[float], limit: int = 10
    ) -> list[dict[str, Any]]:
        return [{**doc, "vector_score": 0.85} for doc in self.docs[:limit]]


class _FakeCacheRepo:
    pass


def test_evidence_redaction_and_id_signing():
    raw_log = "MYSQL_PASSWORD=my_super_secret_pwd, phone=13812345678, api_key=sk-abcdef1234567890"
    cleaned = redact_text(raw_log)
    assert "my_super_secret_pwd" not in cleaned
    assert "13812345678" not in cleaned
    assert "sk-abcdef1234567890" not in cleaned

    ev = build_evidence("search_error_logs", {"user_id": "10086"}, raw_log)
    assert ev.id.startswith("ev_log:")
    assert ev.source == "error_log"

    ev_code = build_evidence("read_file_window", {"file_path": "a.py"}, "code content")
    assert ev_code.source == "source_code"
    assert ev_code.id.startswith("ev_code:")

    ev_db = build_evidence("execute_readonly_sql", {"sql_query": "SELECT 1"}, "result")
    assert ev_db.source == "db_record"
    assert ev_db.id.startswith("ev_db:")


def test_anti_hallucination_verifier_rejects_fake_evidence_id():
    real_ev = build_evidence(
        "read_file_window", {"file_path": "order_service.py"}, "line 64: TypeError"
    )
    bad_draft = ReportDraft(
        summary=[Claim(text="发现空指针异常", evidence_ids=["ev_code:fake99"])],
        problem=[Claim(text="接口 500", evidence_ids=[real_ev.id])],
        cause=[Claim(text="根因明确", evidence_ids=[real_ev.id])],
        next_steps=[Claim(text="修复代码", evidence_ids=[real_ev.id])],
    )
    report, err_msg = verify_and_finalize_report("inc_test_01", bad_draft, [real_ev])
    assert report is None
    assert "防幻觉校验器拦截" in err_msg
    assert "ev_code:fake99" in err_msg


def test_rrf_hybrid_search_ranks_sms_runbook_first():
    bm25_hits = [
        {**INITIAL_RUNBOOKS[0], "bm25_score": 12.5},
        {**INITIAL_RUNBOOKS[1], "bm25_score": 2.1},
    ]
    vec_hits = [
        {**INITIAL_RUNBOOKS[0], "vector_score": 0.92},
        {**INITIAL_RUNBOOKS[2], "vector_score": 0.61},
    ]
    results = rrf_fuse_runbooks(bm25_hits=bm25_hits, vec_hits=vec_hits, top_k=3, k_constant=60)
    assert len(results) >= 1
    assert results[0]["incident_code"] == "INC-2026-089"
    assert results[0]["rrf_score"] == round(1 / 61 + 1 / 61, 6)


def test_full_langgraph_workflow_with_injected_context_and_hitl_resume():
    async def _run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as session:
            incident_repo = IncidentRepository(session)
            runbook_repo = RunbookRepository(session)
            mall_probe_repo = MallProbeRepository(session)
            await mall_probe_repo.seed_initial_data(INITIAL_RUNBOOKS)

            context = IncidentGraphContext(
                incident_id="inc_pytest_case1",
                workspace_id="ws_mall_prod",
                embedding=_DummyEmbedding(),  # type: ignore[typeddict-item]
                incident_repo=incident_repo,
                runbook_repo=runbook_repo,
                mall_probe_repo=mall_probe_repo,
                runtime_log_es_repo=_FakeRuntimeLogESRepo(),  # type: ignore[typeddict-item]
                runbook_es_repo=_FakeRunbookESRepo(),  # type: ignore[typeddict-item]
                runbook_qdrant_repo=_FakeRunbookQdrantRepo(),  # type: ignore[typeddict-item]
                cache_repo=_FakeCacheRepo(),  # type: ignore[typeddict-item]
            )

            graph = build_main_incident_graph()
            config = {"configurable": {"thread_id": "inc_pytest_case1"}}

            init_state = IncidentMainState(
                incident_id="inc_pytest_case1",
                workspace_id="ws_mall_prod",
                messages=[
                    HumanMessage(
                        content="用户 10086 结算订单 /api/orders/checkout 报 500 异常，请排查修复"
                    )
                ],
                evidence=[],
                loop_count=0,
                verify_retry_count=0,
                test_passed=False,
                human_approval="pending",
            )

            # 1. 传入 context 运行至 human_approval_node 的 interrupt() 断点
            await graph.ainvoke(init_state, config=config, context=context)
            snapshot = graph.get_state(config)
            vals = snapshot.values

            assert vals["final_report"] is not None
            assert len(vals["final_report"].digest) == 64
            assert vals["test_passed"] is True
            assert "--- a/app/services/order_service.py" in vals["patch_diff"]

            # 2. 使用 Command(resume=...) 唤醒人工审批节点，验证通过 context 自动写入 RunbookRepository
            resumed = await graph.ainvoke(
                Command(resume={"decision": "approved", "feedback": "批准合并"}),
                config=config,
                context=context,
            )
            assert resumed["human_approval"] == "approved"

            all_runbooks = await runbook_repo.list_all()
            codes = {r.incident_code for r in all_runbooks}
            assert "INC-AUTO-ASE1" in codes

        await engine.dispose()

    asyncio.run(_run())


def test_fastapi_routers_with_annotated_deps_and_service_injection():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.deps import (
        get_embedding_client,
        get_incident_cache_repository,
        get_runbook_es_repository,
        get_runbook_qdrant_repository,
        get_runtime_log_es_repository,
        get_session,
    )
    from app.api.incidents_router import router as incidents_router
    from app.api.knowledge_router import router as knowledge_router
    from app.api.range_router import router as range_router

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _init_db():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_factory() as s:
            await MallProbeRepository(s).seed_initial_data(INITIAL_RUNBOOKS)

    asyncio.run(_init_db())

    fake_log_es = _FakeRuntimeLogESRepo()
    fake_rb_es = _FakeRunbookESRepo()
    fake_rb_qdrant = _FakeRunbookQdrantRepo()
    fake_cache = _FakeCacheRepo()
    fake_emb = _DummyEmbedding()

    async def _override_session():
        async with session_factory() as s:
            yield s

    test_app = FastAPI()
    test_app.include_router(range_router)
    test_app.include_router(incidents_router)
    test_app.include_router(knowledge_router)

    test_app.dependency_overrides[get_session] = _override_session
    test_app.dependency_overrides[get_runtime_log_es_repository] = lambda: fake_log_es
    test_app.dependency_overrides[get_runbook_es_repository] = lambda: fake_rb_es
    test_app.dependency_overrides[get_runbook_qdrant_repository] = lambda: fake_rb_qdrant
    test_app.dependency_overrides[get_incident_cache_repository] = lambda: fake_cache
    test_app.dependency_overrides[get_embedding_client] = lambda: fake_emb

    with TestClient(test_app) as client:
        r_fault = client.post(
            "/api/v1/range/trigger-fault",
            json={"scenario_id": "case_2_vip_discount_bug"},
        )
        assert r_fault.status_code == 200
        fault_data = r_fault.json()["data"]

        r_create = client.post(
            "/api/v1/incidents",
            json={
                "workspace_id": "ws_mall_prod",
                "trigger_source": "user_chat",
                "initial_query": fault_data["suggested_prompt"],
            },
        )
        assert r_create.status_code == 200
        inc_id = r_create.json()["data"]["incident_id"]

        r_stream = client.post(
            f"/api/v1/incidents/{inc_id}/chat-stream",
            json={"message": fault_data["suggested_prompt"]},
        )
        assert r_stream.status_code == 200
        assert "event: report_verified" in r_stream.text
        assert "event: hitl_interrupt" in r_stream.text

        r_approve = client.post(
            f"/api/v1/incidents/{inc_id}/approval",
            json={"decision": "approved", "feedback": "LGTM"},
        )
        assert r_approve.status_code == 200
        assert r_approve.json()["data"]["new_status"] == "resolved"

    asyncio.run(engine.dispose())


def test_evidence_evaluate_node_and_routing():
    from app.graphs.main_graph import evidence_evaluate_node, _route_after_evaluate
    from app.graphs.state import IncidentMainState

    async def _test():
        # 1. 模拟空证据池
        state_empty: IncidentMainState = {
            "incident_id": "inc_empty",
            "workspace_id": "ws_test",
            "messages": [],
            "evidence": [],
            "loop_count": 0,
            "verify_retry_count": 0,
            "test_passed": False,
            "human_approval": "pending",
        }
        res_empty = await evidence_evaluate_node(state_empty, runtime=None)
        assert res_empty["evidence_sufficient"] is False
        assert "evaluate_retry_reason" in res_empty
        assert len(res_empty["evaluate_retry_reason"]) > 0
        state_empty.update(res_empty)
        # 证据不足且 loop_count < 2 时，路由打回重新规划
        assert _route_after_evaluate(state_empty) == "hypothesis_planner_node"

        # 若达到循环上限 (loop_count >= 2)，即便证据不足也推进到生成报告
        state_empty["loop_count"] = 2
        assert _route_after_evaluate(state_empty) == "generate_report_node"

        # 2. 模拟包含有效证据
        real_ev = build_evidence(
            "read_file_window", {"file_path": "order_service.py"}, "line 64: TypeError"
        )
        state_with_ev: IncidentMainState = {
            "incident_id": "inc_with_ev",
            "workspace_id": "ws_test",
            "messages": [],
            "evidence": [real_ev],
            "loop_count": 0,
            "verify_retry_count": 0,
            "test_passed": False,
            "human_approval": "pending",
        }
        res_with_ev = await evidence_evaluate_node(state_with_ev, runtime=None)
        assert res_with_ev["evidence_sufficient"] is True
        assert res_with_ev["evaluate_retry_reason"] == ""
        state_with_ev.update(res_with_ev)
        assert _route_after_evaluate(state_with_ev) == "generate_report_node"

    asyncio.run(_test())


def test_dispatch_probe_workers_carries_known_facts():
    from app.graphs.main_graph import _dispatch_probe_workers
    from app.graphs.state import IncidentMainState
    from app.schemas.incident_schemas import Hypothesis

    ev1 = build_evidence(
        "search_error_logs",
        {"api_path": "/api/orders/checkout"},
        "TypeError at order_service.py:64",
        summary_override="已定位到接口报错发生在 order_service.py 第 64 行",
    )
    h1 = Hypothesis(id="h1", title="检查 order_service.py 源码", probe_type="code")
    state: IncidentMainState = {
        "incident_id": "inc_fact_test",
        "workspace_id": "ws_test",
        "messages": [],
        "evidence": [ev1],
        "hypotheses": [h1],
        "loop_count": 1,
        "verify_retry_count": 0,
        "test_passed": False,
        "human_approval": "pending",
    }
    sends = _dispatch_probe_workers(state)
    assert len(sends) == 1
    worker_state = sends[0].arg
    assert "known_facts" in worker_state
    assert len(worker_state["known_facts"]) == 1
    assert "order_service.py 第 64 行" in worker_state["known_facts"][0]



