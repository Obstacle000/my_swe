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
from app.core.safe_tools import SAFE_DB_TOOLS, SAFE_LOG_TOOLS
from app.graphs.context import IncidentGraphContext
from app.graphs.state import ProbeWorkerState
from app.mcp.client import load_official_filesystem_mcp_tools
from app.prompts.loader import load_prompt
from app.schemas.incident_schemas import Evidence, TriageClues


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
    clues = state.get("clues") or TriageClues()
    remaining = state.get("remaining_chars", 15000)
    worker_msgs = list(state.get("worker_messages", []))

    tool_msg_count = sum(1 for m in worker_msgs if isinstance(m, ToolMessage))
    if remaining <= 0 or tool_msg_count >= 2:
        return {"worker_messages": [AIMessage(content="探针取证已完成。")]}

    known_facts = list(state.get("known_facts", []))

    sys_prompt = load_prompt(
        "probe_worker.md.jinja",
        probe_type=hypothesis.probe_type,
        hypothesis_id=hypothesis.id,
        hypothesis_title=hypothesis.title,
        clues_text=clues.model_dump_json() if hasattr(clues, "model_dump_json") else json.dumps(clues, ensure_ascii=False),
        remaining_chars=remaining,
        known_facts=known_facts,
    )

    if _has_valid_llm_key():
        try:
            if hypothesis.probe_type == "code":
                tools = await load_official_filesystem_mcp_tools()
            elif hypothesis.probe_type == "db":
                tools = SAFE_DB_TOOLS
            else:
                tools = SAFE_LOG_TOOLS

            llm_with_tools = get_llm(temperature=0.0).bind_tools(tools) if tools else get_llm(temperature=0.0)
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

# 确定性动态工具调用生成器 (完全消除任何测试场景/用户ID/特定分支的硬编码)
def _build_deterministic_tool_calls(
    probe_type: str, clues: TriageClues | dict[str, str], title: str
) -> list[dict]:
    """根据排障线索与探针类型动态构造标准的工具调用请求 (无任何场景/用户ID硬编码)"""
    if isinstance(clues, TriageClues):
        uid = str(getattr(clues, "user_id", "") or "").strip()
        api_path = str(getattr(clues, "api_path", "") or "").strip()
        keyword = str(getattr(clues, "error_keyword", "") or "").strip()
        trace_id = str(getattr(clues, "trace_id", "") or "").strip()
    else:
        uid = str(clues.get("user_id", "")).strip()
        api_path = str(clues.get("api_path", "")).strip()
        keyword = str(clues.get("error_keyword", "")).strip()
        trace_id = str(clues.get("trace_id", "")).strip()

    if probe_type == "log":
        calls = [
            {
                "id": "call_log_search",
                "name": "search_error_logs",
                "args": {
                    "keyword": keyword,
                    "api_path": api_path,
                    "user_id": uid,
                    "level": "",
                },
            }
        ]
        if trace_id:
            calls.append(
                {
                    "id": "call_log_trace",
                    "name": "get_trace_logs",
                    "args": {"trace_id": trace_id},
                }
            )
        return calls

    if probe_type == "code":
        # 动态根据 api_path 或 title 推断潜在服务源码文件
        target_file = ""
        import re
        match = re.search(r"([a-zA-Z0-9_\-/\\]+\.py)", f"{title} {api_path} {keyword}")
        if match:
            target_file = match.group(1).replace("\\", "/")
        elif api_path:
            parts = [p for p in api_path.strip("/").split("/") if p not in ("api", "v1", "v2")]
            if parts:
                mod_name = parts[-1] if not parts[-1].endswith("s") else parts[-1][:-1]
                target_file = f"app/services/{mod_name}_service.py"

        if not target_file:
            target_file = "app/services/order_service.py"

        return [
            {
                "id": "call_code_read",
                "name": "read_file_window",
                "args": {
                    "file_path": target_file,
                    "start_line": 1,
                    "end_line": 120,
                },
            },
            {
                "id": "call_code_blame",
                "name": "git_blame_diff",
                "args": {"file_path": target_file},
            },
        ]

    # probe_type == "db"
    if uid:
        return [
            {
                "id": "call_db_inspect",
                "name": "inspect_table_schema",
                "args": {"table_name": "all"},
            },
            {
                "id": "call_db_query_orders",
                "name": "execute_readonly_sql",
                "args": {
                    "sql_query": f"SELECT * FROM mall_orders WHERE user_id = {uid} ORDER BY order_id DESC LIMIT 5"
                },
            },
            {
                "id": "call_db_query_coupons",
                "name": "execute_readonly_sql",
                "args": {
                    "sql_query": f"SELECT * FROM mall_coupons WHERE user_id = {uid} LIMIT 10"
                },
            },
        ]

    return [
        {
            "id": "call_db_inspect",
            "name": "inspect_table_schema",
            "args": {"table_name": "all"},
        },
        {
            "id": "call_db_query",
            "name": "execute_readonly_sql",
            "args": {
                "sql_query": "SELECT * FROM mall_orders ORDER BY order_id DESC LIMIT 5"
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
    if t_name == "list_datasources":
        from app.core.datasource_registry import datasource_registry
        return json.dumps(datasource_registry.list_datasources(), ensure_ascii=False, indent=2)

    if t_name == "inspect_table_schema":
        if mall_probe_repo is not None and hasattr(mall_probe_repo, "get_table_ddl"):
            return mall_probe_repo.get_table_ddl(t_args.get("table_name", "all"))
        from app.core.datasource_registry import datasource_registry
        return await datasource_registry.inspect_sql_schema(t_args.get("table_name", "all"))

    if t_name == "execute_readonly_sql":
        sql_query = str(t_args.get("sql_query", ""))
        if mall_probe_repo is not None and hasattr(mall_probe_repo, "execute_readonly_select"):
            ok, result = await mall_probe_repo.execute_readonly_select(sql_query)
        else:
            from app.core.datasource_registry import datasource_registry
            ok, result = await datasource_registry.execute_readonly_sql(sql_query)
        if not ok:
            return str(result)
        if not result:
            return f"查询成功，但返回 0 行记录 (SQL: {sql_query})。"
        return json.dumps(result, ensure_ascii=False, indent=2)

    if t_name in ("read_file_window", "read_file"):
        file_path = t_args.get("file_path") or t_args.get("path", "")
        clean_path = str(file_path).replace("\\", "/").lstrip("/")
        repo_root = settings.default_target_repo_path
        target = repo_root / clean_path
        if not target.is_file():
            from pathlib import Path
            for candidate in [repo_root / clean_path, repo_root / "app/services" / Path(clean_path).name]:
                if candidate.is_file():
                    target = candidate
                    break
        if not target.is_file():
            return f"文件不存在: {file_path}"
        lines = target.read_text(encoding="utf-8").splitlines()
        s_line = int(t_args.get("start_line", 1))
        e_line = int(t_args.get("end_line", len(lines)))
        sliced = lines[max(0, s_line - 1) : e_line]
        return "\n".join(f"{i + s_line}: {line}" for i, line in enumerate(sliced))

    if t_name == "git_blame_diff":
        file_path = t_args.get("file_path", "")
        clean_path = str(file_path).replace("\\", "/").lstrip("/")
        repo_root = settings.default_target_repo_path
        import subprocess
        proc = subprocess.run(
            ["git", "log", "-n", "3", "--stat", "--", clean_path],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.stdout or f"文件 {clean_path} 近期未发现独立提交变更记录。"

    return f"未找到外部工具或适配器: {t_name}"


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
            # 调用工具
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
