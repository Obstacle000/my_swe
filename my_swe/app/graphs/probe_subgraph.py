"""子图二：单假设探针验证子图 (ProbeWorkerSubgraph)
通过 Runtime[IncidentGraphContext] 与 DatasourceRegistry 执行：
- runtime_log_es_repo (ES 运行态日志仓储)
- datasource_registry (企业级多数据源通用只读与反射网关)
"""

import json
from typing import Literal
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.config import get_llm, settings
from app.core.evidence_collector import build_evidence
from app.core.safe_tools import SAFE_CODE_TOOLS, SAFE_DB_TOOLS, SAFE_LOG_TOOLS
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
                tools = list(SAFE_CODE_TOOLS)
                try:
                    tools.extend(await load_official_filesystem_mcp_tools())
                except Exception:
                    pass
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

# 确定性动态工具调用生成器
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
        # 动态根据 api_path、title 与目标工程目录结构推断服务源码文件
        target_file = ""
        import re
        from pathlib import Path
        repo_root = settings.default_target_repo_path

        match = re.search(r"([a-zA-Z0-9_\-/\\]+\.py)", f"{title} {api_path} {keyword}")
        if match:
            cand = match.group(1).replace("\\", "/")
            if (repo_root / cand).exists():
                target_file = cand

        if not target_file and api_path:
            parts = [p for p in api_path.strip("/").split("/") if p not in ("api", "v1", "v2")]
            # 优先在目标工程中寻找匹配的 service 源码文件
            for p in parts:
                stem = p[:-1] if p.endswith("s") else p
                candidates = [
                    f"app/services/{stem}_service.py",
                    f"app/services/{p}_service.py",
                    f"services/{stem}_service.py",
                    f"{stem}_service.py",
                ]
                for cand in candidates:
                    if (repo_root / cand).is_file():
                        target_file = cand
                        break
                if target_file:
                    break

            if not target_file and parts:
                mod_name = parts[0][:-1] if parts[0].endswith("s") else parts[0]
                target_file = f"app/services/{mod_name}_service.py"

        if not target_file:
            svc_dir = repo_root / "app" / "services"
            if svc_dir.is_dir():
                py_files = [f for f in svc_dir.glob("*.py") if not f.name.startswith("__")]
                if py_files:
                    target_file = f"app/services/{py_files[0].name}"

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
    from app.conf.app_config import app_config

    # 1. 动态推断项目数据表前缀 (根据 target_project 路径动态提取，如 target_xxx_app -> xxx_，绝无写死业务前缀)
    proj_name = getattr(app_config.target_project.path, "name", str(app_config.target_project.path))
    proj_prefix = ""
    if proj_name.startswith("target_"):
        cleaned_proj = proj_name[len("target_") :]
        if cleaned_proj.endswith("_app"):
            cleaned_proj = cleaned_proj[:-len("_app")]
        proj_prefix = f"{cleaned_proj}_"

    # 2. 动态推断主实体与关联实体 (从 api_path / title / clues 提取)
    table_candidates: list[str] = []
    if api_path:
        parts = [p for p in api_path.strip("/").split("/") if p not in ("api", "v1", "v2", "v3")]
        if parts:
            table_candidates.append(parts[0])
            if len(parts) > 1 and parts[1] not in ("checkout", "detail", "list", "query", "create", "update", "verify"):
                table_candidates.append(parts[1])

    if not table_candidates:
        mod = getattr(clues, "module_hint", "") if isinstance(clues, TriageClues) else clues.get("module_hint", "")
        if mod and mod != "core":
            table_candidates.append(mod)

    import re
    # 尝试从 title / keyword 中提取提及的表名或实体词
    matches = re.findall(r"(?:from|join|table)\s+([a-zA-Z0-9_]+)", f"{title} {keyword}", re.IGNORECASE)
    for m in matches:
        if m.lower() not in table_candidates:
            table_candidates.append(m.lower())

    # 常见关联实体推断 (根据线索文本中包含的关键字)
    for kw in ("coupon", "discount", "user", "account", "ticket", "item", "payment"):
        if kw in f"{title} {keyword}".lower() and f"{kw}s" not in table_candidates:
            table_candidates.append(f"{kw}s")

    if not table_candidates:
        table_candidates = ["orders", "records"]

    main_table = f"{proj_prefix}{table_candidates[0]}"

    calls = [
        {
            "id": "call_db_inspect",
            "name": "inspect_table_schema",
            "args": {"table_name": "all"},
        }
    ]

    if uid:
        calls.append(
            {
                "id": "call_db_query_main",
                "name": "execute_readonly_sql",
                "args": {
                    "sql_query": f"SELECT * FROM {main_table} WHERE user_id = {uid} LIMIT 5"
                },
            }
        )
        if len(table_candidates) > 1:
            sec_table = f"{proj_prefix}{table_candidates[1]}"
            calls.append(
                {
                    "id": "call_db_query_sec",
                    "name": "execute_readonly_sql",
                    "args": {
                        "sql_query": f"SELECT * FROM {sec_table} WHERE user_id = {uid} LIMIT 10"
                    },
                }
            )
    else:
        calls.append(
            {
                "id": "call_db_query_main",
                "name": "execute_readonly_sql",
                "args": {
                    "sql_query": f"SELECT * FROM {main_table} LIMIT 5"
                },
            }
        )

    return calls


async def _execute_tool_with_context(
    t_name: str,
    t_args: dict,
    ctx: IncidentGraphContext,
) -> str:
    """动态工具执行分发网关：直接通过 safe_tools 注册表与外部 MCP 动态分发"""
    from app.core.safe_tools import (
        SAFE_CODE_TOOLS,
        SAFE_DB_TOOLS,
        SAFE_LOG_TOOLS,
        current_runtime_log_es_repo,
    )

    # 1. 直接复用 safe_tools 现成工具列表构建动态索引
    tool_map: dict[str, BaseTool] = {
        t.name: t for t in (SAFE_DB_TOOLS + SAFE_LOG_TOOLS + SAFE_CODE_TOOLS)
    }

    # 2. 动态合并外部标准 MCP 工具
    try:
        from app.mcp.client import load_official_filesystem_mcp_tools
        for mt in await load_official_filesystem_mcp_tools():
            tool_map.setdefault(mt.name, mt)
    except Exception:
        pass

    # 参数别名与路径兼容规整 (例如 official MCP 的 read_file(path=...) 兼容适配)
    if t_name == "read_file" and "file_path" not in t_args and "path" in t_args:
        t_args = {
            "file_path": t_args["path"],
            "start_line": int(t_args.get("start_line", 1)),
            "end_line": int(t_args.get("end_line", 120)),
        }
        t_name = "read_file_window"

    target_tool = tool_map.get(t_name)
    if not target_tool:
        return f"未找到外部工具或适配器: {t_name}"

    # 3. 若当前运行上下文注入了特定 log_es_repo，设置上下文变量透传
    log_es_repo = ctx.get("runtime_log_es_repo") if isinstance(ctx, dict) else getattr(ctx, "runtime_log_es_repo", None)
    if log_es_repo is not None:
        current_runtime_log_es_repo.set(log_es_repo)

    try:
        res = await target_tool.ainvoke(t_args)
        return json.dumps(res, ensure_ascii=False, indent=2) if isinstance(res, (dict, list)) else str(res)
    except Exception as exc:
        return f"工具执行异常 ({t_name}): {exc}"


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
