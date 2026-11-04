"""全链路命令行演示与自检脚本 (run_demo_cli.py)
通过 app/clients/ 客户端管理器与 app/repositories/ 仓储层构造 IncidentGraphContext，
在调用 main_incident_graph.astream / ainvoke 时通过 context=... 传入依赖。
"""

import asyncio
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from sqlalchemy import text

from app.clients.embedding_client_manager import embedding_client_manager
from app.clients.es_client_manager import es_client_manager
from app.clients.mysql_client_manager import mysql_client_manager
from app.clients.qdrant_client_manager import qdrant_client_manager
from app.clients.redis_client_manager import redis_client_manager
from app.graphs.context import IncidentGraphContext
from app.graphs.main_graph import main_incident_graph
from app.graphs.state import IncidentMainState
from app.models import Base
from app.repositories.es.runbook_es_repo import RunbookESRepository
from app.repositories.es.runtime_log_es_repo import RuntimeLogESRepository
from app.repositories.mysql.incident_repo import IncidentRepository
from app.repositories.mysql.mall_probe_repo import MallProbeRepository
from app.repositories.mysql.runbook_repo import RunbookRepository
from app.repositories.qdrant.runbook_qdrant_repo import RunbookQdrantRepository
from app.repositories.redis.incident_cache_repo import IncidentCacheRepository
from app.services.knowledge_service import INITIAL_RUNBOOKS, KnowledgeService
from app.services.range_service import INITIAL_RUNTIME_LOGS


async def run_single_scenario(
    incident_id: str,
    scenario_title: str,
    user_query: str,
    context: IncidentGraphContext,
) -> None:
    print("\n" + "=" * 88)
    print(f"▶ 开始演练: {scenario_title} (thread_id / incident_id = {incident_id})")
    print(f"▶ 用户输入: {user_query}")
    print("=" * 88)

    config = {"configurable": {"thread_id": incident_id}}
    initial_state = IncidentMainState(
        incident_id=incident_id,
        workspace_id="ws_mall_prod",
        messages=[HumanMessage(content=user_query)],
        evidence=[],
        loop_count=0,
        verify_retry_count=0,
        test_passed=False,
        human_approval="pending",
    )

    async for step in main_incident_graph.astream(
        initial_state,
        config=config,
        context=context,
        stream_mode="updates",
    ):
        for node_name, update in step.items():
            if node_name == "__interrupt__":
                intr_val = update[0].value if update else {}
                print("\n[⏸ HITL 断点挂起 (interrupt)] 等待人工审批:")
                print(f"  - 动作要求: {intr_val.get('action_required')}")
                print(f"  - 报告指纹 (SHA-256): {intr_val.get('report_digest')}")
                print(f"  - 是否需要代码补丁: {intr_val.get('need_code_patch')}")
                print(f"  - 沙箱 pytest 是否通过: {intr_val.get('pytest_passed')}")
                continue

            print(f"\n[Node 完成] -> {node_name}")
            if node_name == "triage_node":
                print(f"  提取线索: {update.get('extracted_clues')}")
            elif node_name == "crag_subgraph_node":
                print(f"  CRAG 命中状态: crag_hit={update.get('crag_hit')}")
            elif node_name == "hypothesis_planner_node":
                for h in update.get("hypotheses", []):
                    print(f"  派发并行探针假设 [{h.id}] (probe_type={h.probe_type}): {h.title}")
            elif node_name == "probe_worker_node":
                for ev in update.get("evidence", []):
                    print(f"  自动签发防伪证据 [{ev.id}] ({ev.tool_name}): {ev.summary}")
            elif node_name == "verify_report_node":
                rep = update.get("final_report")
                if rep:
                    print(f"  ✔ 防幻觉校验通过! 正式报告摘要: {rep.summary}")
                    print(f"  ✔ 根因结论 (含真实证据引用): {rep.cause}")
            elif node_name == "patch_subgraph_node":
                print(f"  ✔ 沙箱 pytest 单测结果: test_passed={update.get('test_passed')}")
                diff_txt = update.get("patch_diff", "")
                if diff_txt:
                    print("  ✔ 沙箱生成的 Unified Diff 补丁:\n" + diff_txt)

    print("\n▶ 模拟人工审批通过: 调用 Command(resume={'decision': 'approved'})...")
    final_state = await main_incident_graph.ainvoke(
        Command(resume={"decision": "approved", "feedback": "LGTM，同意合并补丁并归档"}),
        config=config,
        context=context,
    )
    last_msg = final_state["messages"][-1].content if final_state.get("messages") else ""
    print(f"✔ 结案输出:\n{last_msg}")


async def main() -> None:
    mysql_client_manager.init()
    redis_client_manager.init()
    qdrant_client_manager.init()
    es_client_manager.init()
    embedding_client_manager.init()

    try:
        async with mysql_client_manager.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text("SELECT 1"))

        async with mysql_client_manager.session_factory() as session:
            incident_repo = IncidentRepository(session)
            runbook_repo = RunbookRepository(session)
            mall_probe_repo = MallProbeRepository(session)
            runtime_log_es_repo = RuntimeLogESRepository(es_client_manager.client)
            runbook_es_repo = RunbookESRepository(es_client_manager.client)
            runbook_qdrant_repo = RunbookQdrantRepository(
                qdrant_client_manager.client,
                embedding_client_manager.config.dim,
            )
            cache_repo = IncidentCacheRepository(redis_client_manager.client)

            await mall_probe_repo.seed_initial_data(INITIAL_RUNBOOKS)
            await runtime_log_es_repo.bulk_write_logs(INITIAL_RUNTIME_LOGS)

            knowledge_svc = KnowledgeService(
                runbook_repo=runbook_repo,
                runbook_es_repo=runbook_es_repo,
                runbook_qdrant_repo=runbook_qdrant_repo,
                embedding=embedding_client_manager.client,
            )
            await knowledge_svc.reindex_all_from_mysql()

            ctx = IncidentGraphContext(
                incident_id="inc_demo_case1",
                workspace_id="ws_mall_prod",
                embedding=embedding_client_manager.client,
                incident_repo=incident_repo,
                runbook_repo=runbook_repo,
                mall_probe_repo=mall_probe_repo,
                runtime_log_es_repo=runtime_log_es_repo,
                runbook_es_repo=runbook_es_repo,
                runbook_qdrant_repo=runbook_qdrant_repo,
                cache_repo=cache_repo,
            )

            await run_single_scenario(
                incident_id="inc_demo_case1",
                scenario_title="剧本一：优惠券 NoneType 引发订单结算 500 崩溃",
                user_query="用户 10086 刚刚反馈在商城结算订单（调用 /api/orders/checkout）时页面直接报 500 系统异常，麻烦帮我排查一下原因并修复。",
                context=ctx,
            )
    finally:
        await qdrant_client_manager.close()
        await es_client_manager.close()
        await redis_client_manager.close()
        await mysql_client_manager.close()


if __name__ == "__main__":
    asyncio.run(main())
