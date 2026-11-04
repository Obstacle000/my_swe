"""全链路命令行演示与自检脚本 (run_demo_cli.py)
一键验证《01_USE_CASES.md》中的三大经典排障剧本：
- 剧本一：用户 10086 优惠券 NoneType 引发结算 500 崩溃 (并行探针 + 沙箱补丁 + pytest 单测自愈 + HITL 审批)
- 剧本二：用户 10087 VIP 2 黄金会员未打 8 折静默逻辑缺陷排查与沙箱修复
- 剧本三：登录短信网关 ReadTimeoutError 历史故障复发 (CRAG 双路 RRF 混合检索命中 INC-2026-089)
"""

import asyncio
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from app.core.storage_backends import seed_databases_if_empty, storage_manager
from app.graphs.main_graph import main_incident_graph


def run_single_scenario(incident_id: str, scenario_title: str, user_query: str) -> None:
    print("\n" + "=" * 88)
    print(f"▶ 开始演练: {scenario_title} (thread_id / incident_id = {incident_id})")
    print(f"▶ 用户输入: {user_query}")
    print("=" * 88)

    config = {"configurable": {"thread_id": incident_id}}
    initial_state = {
        "incident_id": incident_id,
        "workspace_id": "ws_mall_prod",
        "messages": [HumanMessage(content=user_query)],
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

    for step in main_incident_graph.stream(initial_state, config=config, stream_mode="updates"):
        for node_name, update in step.items():
            if node_name == "__interrupt__":
                intr_val = update[0].value if update else {}
                print(f"\n[⏸ HITL 断点挂起 (interrupt)] 等待人工审批:")
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

    # 模拟工程师点击【批准合并并归档】按钮，通过 Command(resume=...) 唤醒图执行
    print("\n▶ 模拟人工审批通过: 调用 Command(resume={'decision': 'approved'})...")
    final_state = main_incident_graph.invoke(
        Command(resume={"decision": "approved", "feedback": "LGTM，同意合并补丁并归档"}),
        config=config,
    )
    last_msg = final_state["messages"][-1].content if final_state.get("messages") else ""
    print(f"✔ 结案输出:\n{last_msg}")


async def main() -> None:
    await seed_databases_if_empty()

    # 1. 验证 RRF 双路混合检索公式输出
    print("=" * 88)
    print("▶ 预检: 验证 ES (BM25) + Qdrant (Vector) + RRF (k=60) 倒数秩融合检索")
    rrf_hits = storage_manager.hybrid_search_rrf("登录接口 /api/auth/send-sms 报错 ReadTimeoutError timeout=2.0", top_k=2)
    for item in rrf_hits:
        print(
            f"  - [{item['incident_code']}] RRF={item['rrf_score']} "
            f"(BM25 Rank={item['bm25_rank']}, Vector Rank={item['vector_rank']}) | {item['title']}"
        )

    # 2. 演练剧本一 (空指针 500 + 沙箱补丁 + pytest 单测自愈)
    run_single_scenario(
        incident_id="inc_demo_case1",
        scenario_title="剧本一：优惠券 NoneType 引发订单结算 500 崩溃",
        user_query="用户 10086 刚刚反馈在商城结算订单（调用 /api/orders/checkout）时页面直接报 500 系统异常，麻烦帮我排查一下原因并修复。",
    )

    # 3. 演练剧本三 (CRAG 命中历史故障 INC-2026-089)
    run_single_scenario(
        incident_id="inc_demo_case3",
        scenario_title="剧本三：短信验证码接口超时历史故障复发 (CRAG 命中)",
        user_query="线上告警：登录发送验证码接口 /api/auth/send-sms 突然大面积报 ReadTimeoutError (read timeout=2.0)，请紧急排查是否历史故障复发。",
    )


if __name__ == "__main__":
    asyncio.run(main())
