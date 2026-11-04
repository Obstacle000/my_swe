"""针对排障平台核心机制的自动化回归单元测试
覆盖：
1. EvidenceCollector 敏感数据脱敏 (redact_text) 与确定性证据编号签发 (build_evidence)
2. ReportDraft -> valid_refs 防幻觉拦截校验器与 SHA-256 指纹幂等判重 (fingerprint)
3. ES (BM25) + Qdrant (Vector) + RRF (k=60) 倒数秩融合检索
4. LangGraph 主图 + 三大子图全链路执行、interrupt() 断点挂起与 Command(resume=...) 恢复
"""

import asyncio
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from app.core.evidence_collector import build_evidence, redact_text
from app.core.report_verifier import verify_and_finalize_report
from app.core.storage_backends import seed_databases_if_empty, storage_manager
from app.graphs.main_graph import build_main_incident_graph
from app.schemas.incident_schemas import Claim, ReportDraft


def test_evidence_redaction_and_id_signing():
    raw_log = "MYSQL_PASSWORD=my_super_secret_pwd, phone=13812345678, api_key=sk-abcdef1234567890"
    cleaned = redact_text(raw_log)
    assert "my_super_secret_pwd" not in cleaned
    assert "13812345678" not in cleaned
    assert "sk-abcdef1234567890" not in cleaned

    ev = build_evidence("search_error_logs", {"user_id": "10086"}, raw_log)
    assert ev.id.startswith("ev_log:")
    assert ev.source == "es_log"


def test_anti_hallucination_verifier_rejects_fake_evidence_id():
    real_ev = build_evidence("read_file_window", {"file_path": "order_service.py"}, "line 64: TypeError")
    # 故意伪造不存在的 ev_code:fake99
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
    results = storage_manager.hybrid_search_rrf("send-sms ReadTimeoutError timeout=2.0", top_k=3)
    assert len(results) >= 1
    assert results[0]["incident_code"] == "INC-2026-089"
    assert results[0]["rrf_score"] > 0


def test_full_langgraph_workflow_and_hitl_resume():
    asyncio.run(seed_databases_if_empty())
    graph = build_main_incident_graph()
    config = {"configurable": {"thread_id": "inc_pytest_case1"}}

    init_state = {
        "incident_id": "inc_pytest_case1",
        "workspace_id": "ws_mall_prod",
        "messages": [HumanMessage(content="用户 10086 结算订单 /api/orders/checkout 报 500 异常，请排查修复")],
        "extracted_clues": {},
        "historical_context": "",
        "crag_hit": False,
        "hypotheses": [],
        "evidence": [],
        "loop_count": 0,
        "report_draft": None,
        "final_report": None,
        "verification_error": "",
        "verify_retry_count": 0,
        "patch_diff": "",
        "test_passed": False,
        "human_approval": "pending",
        "human_feedback": "",
    }

    # 1. 运行至 human_approval_node 的 interrupt() 断点
    graph.invoke(init_state, config=config)
    snapshot = graph.get_state(config)
    vals = snapshot.values

    assert vals["final_report"] is not None
    assert len(vals["final_report"].digest) == 64
    assert vals["test_passed"] is True
    assert "--- a/app/services/order_service.py" in vals["patch_diff"]

    # 2. 使用 Command(resume=...) 唤醒人工审批节点
    resumed = graph.invoke(
        Command(resume={"decision": "approved", "feedback": "批准合并"}),
        config=config,
    )
    assert resumed["human_approval"] == "approved"
