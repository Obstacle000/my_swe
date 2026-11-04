"""子图二：单假设探针验证子图 (ProbeWorkerSubgraph)
通过 Runtime[IncidentGraphContext] 从运行上下文中获取注入的：
- runtime_log_es_repo (ES 运行态日志仓储)
- mall_probe_repo (MySQL 电商靶场只读探针仓储)
"""

import json
from typing import Literal
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.config import get_llm, settings
from app.core.evidence_collector import build_evidence
from app.graphs.context import IncidentGraphContext
from app.graphs.state import ProbeWorkerState
from app.prompts.loader import load_prompt
from app.schemas.incident_schemas import Evidence
from app.tools.code_tools import CODE_TOOLS
from app.tools.db_tools import DB_TOOLS
from app.tools.log_tools import LOG_TOOLS

_TOOLSET_BY_PROBE_TYPE = {
    "log": LOG_TOOLS,
    "code": CODE_TOOLS,
    "db": DB_TOOLS,
}

_CODE_TOOLS_BY_NAME = {t.name: t for t in CODE_TOOLS}


def _has_valid_llm_key() -> bool:
    key = (settings.llm_api_key or "").strip()
    return bool(key) and not key.startswith("sk-your-") and key != "sk-placeholder"

# 最初的一版有缺陷,探针不能依靠重试机制,把前面几轮探针得出的结论有效利用起来,
# 必须在Send的时候传入以得出的事实,以及生成假设时,根据以得出前序探针事实和检索证据进行生成
# 模型理解并排错是一个循环的过程 提出假设->探针排查->得出证据->证据不足->提出新假设->带着以得出结论探针排查->...
# 这个过程中,三个探针会把局部结论更新到全局evidence,让三个探针互相知道对方得出的证据
async def probe_agent_node(
    state: ProbeWorkerState, runtime: Runtime[IncidentGraphContext]
) -> dict:
    """探针推理节点：根据假设类型挂载隔离工具集并决定调用哪个探针工具"""
    hypothesis = state["hypothesis"]
    clues = state.get("clues", {})
    remaining = state.get("remaining_chars", 15000)
    worker_msgs = list(state.get("worker_messages", []))

    tool_msg_count = sum(1 for m in worker_msgs if isinstance(m, ToolMessage))
    if remaining <= 0 or tool_msg_count >= 2:
        return {"worker_messages": [AIMessage(content="探针取证已完成。")]}

    known_facts = list(state.get("known_facts", []))

    tools = _TOOLSET_BY_PROBE_TYPE.get(hypothesis.probe_type, CODE_TOOLS)
    sys_prompt = load_prompt(
        "probe_worker.md.jinja",
        probe_type=hypothesis.probe_type,
        hypothesis_id=hypothesis.id,
        hypothesis_title=hypothesis.title,
        clues_text=json.dumps(clues, ensure_ascii=False),
        remaining_chars=remaining,
        known_facts=known_facts,
    )

    if _has_valid_llm_key():
        try:
            llm_with_tools = get_llm(temperature=0.0).bind_tools(tools)
            invoke_msgs = [SystemMessage(content=sys_prompt)] + worker_msgs
            ai_resp = await llm_with_tools.ainvoke(invoke_msgs)
            return {"worker_messages": [ai_resp]}
        except Exception:
            pass

    if tool_msg_count == 0:
        fallback_calls = _build_deterministic_tool_calls(
            hypothesis.probe_type, clues, hypothesis.title
        )
        return {
            "worker_messages": [
                AIMessage(
                    content=f"正在针对假设 [{hypothesis.id}] 执行 {hypothesis.probe_type} 探针工具取证。",
                    tool_calls=fallback_calls,
                )
            ]
        }

    return {"worker_messages": [AIMessage(content="探针取证完毕。")]}

# 兜底工具调用
def _build_deterministic_tool_calls(
    probe_type: str, clues: dict[str, str], title: str
) -> list[dict]:
    """根据排障线索与探针类型构造真实的工具调用请求"""
    uid = clues.get("user_id", "")
    api_path = clues.get("api_path", "")
    keyword = clues.get("error_keyword", "")
    combined = f"{title} {api_path} {keyword} {uid}".lower()

    if probe_type == "log":
        return [
            {
                "id": "call_log_1",
                "name": "search_error_logs",
                "args": {
                    "keyword": "" if uid else keyword,
                    "api_path": api_path,
                    "user_id": uid,
                    "level": "",
                },
            }
        ]

    if probe_type == "code":
        if "vip" in combined or "10087" in combined or "折扣" in combined or "pricing" in combined:
            return [
                {
                    "id": "call_code_1",
                    "name": "read_file_window",
                    "args": {
                        "file_path": "app/services/pricing_service.py",
                        "start_line": 1,
                        "end_line": 70,
                    },
                },
                {
                    "id": "call_code_2",
                    "name": "git_blame_diff",
                    "args": {"file_path": "app/services/pricing_service.py"},
                },
            ]
        if "sms" in combined or "验证码" in combined or "timeout" in combined or "10088" in combined:
            return [
                {
                    "id": "call_code_1",
                    "name": "read_file_window",
                    "args": {
                        "file_path": "config/settings.yaml",
                        "start_line": 1,
                        "end_line": 50,
                    },
                }
            ]
        return [
            {
                "id": "call_code_1",
                "name": "read_file_window",
                "args": {
                    "file_path": "app/services/order_service.py",
                    "start_line": 1,
                    "end_line": 85,
                },
            },
            {
                "id": "call_code_2",
                "name": "git_blame_diff",
                "args": {"file_path": "app/services/order_service.py"},
            },
        ]

    # probe_type == "db"
    if "10087" in combined or "vip" in combined or "折扣" in combined:
        return [
            {
                "id": "call_db_1",
                "name": "execute_readonly_sql",
                "args": {
                    "sql_query": "SELECT id, username, vip_level, status FROM mall_users WHERE id = 10087"
                },
            },
            {
                "id": "call_db_2",
                "name": "execute_readonly_sql",
                "args": {
                    "sql_query": "SELECT order_id, user_id, base_price, final_price, order_status, trace_id FROM mall_orders WHERE user_id = 10087"
                },
            },
        ]

    return [
        {
            "id": "call_db_1",
            "name": "execute_readonly_sql",
            "args": {
                "sql_query": "SELECT coupon_id, user_id, coupon_type, discount_amount, discount_rate, status FROM mall_coupons WHERE user_id = 10086"
            },
        },
        {
            "id": "call_db_2",
            "name": "execute_readonly_sql",
            "args": {
                "sql_query": "SELECT order_id, user_id, sku_id, base_price, coupon_id, final_price, order_status, trace_id FROM mall_orders WHERE user_id = 10086"
            },
        },
    ]


async def _execute_tool_with_context(
    t_name: str,
    t_args: dict,
    ctx: IncidentGraphContext,
) -> str:
    """使用 runtime.context 中注入的 Repository 实例执行对应的探针查询"""
    log_es_repo = ctx["runtime_log_es_repo"]
    mall_probe_repo = ctx["mall_probe_repo"]
    # 有的工具需要额外的参数,模型不知道只能手动传入
    if t_name == "search_error_logs":
        hits = await log_es_repo.search_logs(
            keyword=t_args.get("keyword", ""),
            api_path=t_args.get("api_path", ""),
            user_id=str(t_args.get("user_id", "")),
            level=t_args.get("level", ""),
            limit=5,
        )
        if not hits:
            return "未检索到匹配的运行态日志，请尝试放宽过滤条件。"
        return json.dumps(hits, ensure_ascii=False, indent=2)

    if t_name == "get_trace_logs":
        logs = await log_es_repo.get_logs_by_trace_id(str(t_args.get("trace_id", "")))
        if not logs:
            return f"未找到 trace_id={t_args.get('trace_id')} 对应的链路日志。"
        return json.dumps(logs, ensure_ascii=False, indent=2)
    # 以下是测试用于访问数据源的工具,有点丑陋,删的化记得也把deps,repo,lifespan,依赖注入也删了
    if t_name == "inspect_table_schema":
        return mall_probe_repo.get_table_ddl(t_args.get("table_name", "all"))

    if t_name == "execute_readonly_sql":
        sql_query = str(t_args.get("sql_query", ""))
        ok, result = await mall_probe_repo.execute_readonly_select(sql_query)
        if not ok:
            return str(result)
        if not result:
            return f"查询成功，但返回 0 行记录 (SQL: {sql_query})。"
        return json.dumps(result, ensure_ascii=False, indent=2)

    code_tool = _CODE_TOOLS_BY_NAME.get(t_name)
    if code_tool is not None:
        return str(code_tool.invoke(t_args))

    return f"未找到工具: {t_name}"


async def probe_tool_node(
    state: ProbeWorkerState, runtime: Runtime[IncidentGraphContext]
) -> dict:
    """工具执行与自动证据签发包装节点：通过 runtime.context 中的仓储访问 ES 与 MySQL"""
    ctx = runtime.context
    worker_msgs = state.get("worker_messages", [])
    remaining = state.get("remaining_chars", 15000)
    if not worker_msgs:
        return {}

    last_msg = worker_msgs[-1]
    tool_calls = getattr(last_msg, "tool_calls", None) or []
    if not tool_calls:
        return {}

    tool_messages: list[ToolMessage] = []
    signed_evidences: list[Evidence] = []
    recorded_gaps: list[str] = []

    for tc in tool_calls:
        t_name = tc["name"]
        t_args = tc.get("args", {})
        t_id = tc.get("id", f"call_{t_name}")

        if remaining <= 0:
            gap_msg = f"[探针 {state['hypothesis'].id} 预算熔断] 字符采集预算 (15,000 chars) 已耗尽，跳过工具 {t_name}。"
            recorded_gaps.append(gap_msg)
            tool_messages.append(
                ToolMessage(
                    tool_call_id=t_id,
                    name=t_name,
                    content=gap_msg,
                )
            )
            continue

        try:
            raw_out = await _execute_tool_with_context(t_name, t_args, ctx)
        except Exception as exc:
            raw_out = f"工具调用异常 ({t_name}): {exc}"
            recorded_gaps.append(f"[探针 {state['hypothesis'].id} 工具异常] {raw_out}")

        ev = build_evidence(tool_name=t_name, tool_args=t_args, raw_output=raw_out)
        signed_evidences.append(ev)
        remaining = max(0, remaining - len(ev.raw_snippet))

        tool_messages.append(
            ToolMessage(
                tool_call_id=t_id,
                name=t_name,
                content=f"[已签发真实证据 ID: {ev.id}]\n{ev.raw_snippet}",
            )
        )

    return {
        "worker_messages": tool_messages,
        "evidence": signed_evidences,
        "gaps": recorded_gaps,
        "remaining_chars": remaining,
    }


def _route_probe_worker(
    state: ProbeWorkerState,
) -> Literal["probe_tool_node", "__end__"]:
    msgs = state.get("worker_messages", [])
    if not msgs:
        return "__end__"
    last_msg = msgs[-1]
    if getattr(last_msg, "tool_calls", None) and state.get("remaining_chars", 15000) > 0:
        return "probe_tool_node"
    return "__end__"


def build_probe_subgraph():
    """构建并编译带 IncidentGraphContext 上下文 Schema 的单假设探针验证子图"""
    builder = StateGraph(state_schema=ProbeWorkerState, context_schema=IncidentGraphContext)
    builder.add_node("probe_agent_node", probe_agent_node)
    builder.add_node("probe_tool_node", probe_tool_node)
    # 连线是简单的ReAct
    builder.add_edge(START, "probe_agent_node")
    builder.add_conditional_edges(
        "probe_agent_node",
        _route_probe_worker,
        {
            "probe_tool_node": "probe_tool_node",
            "__end__": END,
        },
    )
    builder.add_edge("probe_tool_node", "probe_agent_node")
    return builder.compile()


probe_subgraph = build_probe_subgraph()
