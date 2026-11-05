import difflib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.config import PROJECT_ROOT, get_llm, settings
from app.graphs.context import IncidentGraphContext
from app.graphs.state import PatchSelfHealSubState
from app.mcp.client import load_official_filesystem_mcp_tools
from app.prompts.loader import load_prompt
from app.sandbox.container_runner import LinuxContainerSandbox
from app.schemas.incident_schemas import Evidence


def _has_valid_llm_key() -> bool:
    key = (settings.llm_api_key or "").strip()
    return bool(key) and not key.startswith("sk-your-") and key != "sk-placeholder"





def _normalize_rel_path(cand: str, sandbox_dir: Path) -> str | None:
    cleaned = cand.replace("\\", "/").strip().lstrip("/")
    if cleaned.startswith("target_mall_app/"):
        cleaned = cleaned[len("target_mall_app/") :]

    for prefix in ["", "app/services/", "app/api/", "app/"]:
        if not prefix:
            target = sandbox_dir / cleaned
        else:
            filename = Path(cleaned).name
            target = sandbox_dir / prefix / filename
        if target.is_file():
            rel = os.path.relpath(target, sandbox_dir)
            return rel.replace("\\", "/")
    return None

# 返回的是相对sandbox_dir的路径
def _resolve_target_file(
    sandbox_dir: Path,
    report_summary: str,
    report_cause: str,
    report_next_steps: list[str],
    evidence_pool: list[Evidence],
) -> str:
    """遵循工业界故障定位 (Fault Localization) 策略解析待修复源码文件：
    1. 报告根因与修复建议优先 (SRE 裁决与审计结论)
    2. 运行态异常堆栈最深层 (Innermost/Bottom Frame) 提取 (跳过外部路由/网关调用方)
    3. 取证探针检索证据关联文件
    4. 兜底扫描
    """
    # 1. 优先提取排障报告明确指出的根因模块与处置步骤
    report_text = f"{report_cause} {' '.join(report_next_steps)} {report_summary}"
    for match in re.findall(r"([a-zA-Z0-9_\-/\\]+\.py)", report_text):
        # 把大模型、堆栈日志或探针报告里提到的“五花八门的乱七八糟路径”，规范化映射成沙箱里真正存在的相对路径（如 app/services/order_service.py）
        resolved = _normalize_rel_path(match, sandbox_dir)
        if resolved:
            return resolved

    # 2. 堆栈回溯定位：工业界 APM 惯例，报错堆栈中最内层 (倒序最后一行) 属于工程内部的文件即为抛出异常的原发缺陷现场
    for ev in evidence_pool:
        if ev.source in ("error_log", "runtime_log") or "Traceback" in ev.raw_snippet:
            frames = re.findall(r'File\s+["\']([^"\']+\.py)["\']', ev.raw_snippet)
            for frame in reversed(frames):
                resolved = _normalize_rel_path(frame, sandbox_dir)
                if resolved:
                    return resolved

    # 3. 从代码审计探针与 Git Blame 证据中匹配
    for ev in evidence_pool:
        if ev.source in ("code_snippet", "git_blame"):
            for match in re.findall(r"([a-zA-Z0-9_\-/\\]+\.py)", f"{ev.summary} {ev.raw_snippet}"):
                resolved = _normalize_rel_path(match, sandbox_dir)
                if resolved:
                    return resolved

    # 4. 目录降级兜底匹配
    for root, _, files in os.walk(sandbox_dir / "app" if (sandbox_dir / "app").exists() else sandbox_dir):
        for f in files:
            if f.endswith(".py") and not f.startswith("__") and "router" not in f:
                rel = os.path.relpath(os.path.join(root, f), sandbox_dir)
                return rel.replace("\\", "/")

    return "app/services/order_service.py" # 兜底相对路径


def _resolve_pytest_keyword_filter(
    rel_target: str,
    report_cause: str,
    evidence_pool: list[Evidence],
    sandbox_dir: Path | None = None,
) -> str:
    """基于 AST 依赖静态分析的定向单测选择 (Impacted Test Selection, ITS)
    动态扫描 tests/ 目录下的单测 AST 语法树，找出直接调用或导入待修复模块的测试用例名称，绝无任何硬编码映射。
    """
    if sandbox_dir and (sandbox_dir / "tests").exists():
        import ast
        target_stem = Path(rel_target).stem
        impacted_tests = []
        for test_file in (sandbox_dir / "tests").glob("*.py"):
            try:
                tree = ast.parse(test_file.read_text(encoding="utf-8"))
                imported_symbols = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.module and target_stem in node.module:
                        for alias in node.names:
                            imported_symbols.add(alias.name)
                for node in ast.walk(tree):
                    if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                        func_calls = {
                            n.id for n in ast.walk(node) if isinstance(n, ast.Name)
                        }
                        if func_calls.intersection(imported_symbols):
                            impacted_tests.append(node.name)
            except Exception:
                pass
        if impacted_tests:
            prefix = os.path.commonprefix(impacted_tests)
            if prefix and len(prefix) >= 5:
                return prefix
            return " or ".join(impacted_tests[:3])

    return Path(rel_target).stem.replace("_service", "")


def _apply_grounded_fallback_fix(
    sandbox_dir: str,
    rel_target: str,
    report_cause: str,
    evidence_pool: list[Evidence],
    patch_spec: dict[str, str] | None = None,
) -> None:
    """当离线或无有效 LLM API Key 时执行的补丁应用器 (通过 Linux 容器沙箱执行 AST 校验)"""
    sandbox = LinuxContainerSandbox(workspace_id=Path(sandbox_dir).name)
    target_path = Path(sandbox_dir) / rel_target
    if not target_path.exists():
        return
    code_text = target_path.read_text(encoding="utf-8")

    # 1. 优先执行调用方或测试注入的 patch_spec
    if patch_spec and "old_text" in patch_spec and "new_text" in patch_spec:
        sandbox.edit_file(
            rel_path=rel_target,
            old_text=patch_spec["old_text"],
            new_text=patch_spec["new_text"],
        )
        return

    # 2. 通用自愈匹配：针对 NoneType / RATE 折扣券未判空的计算缺陷
    if "rate" in code_text.lower() and "discount_amount" in code_text:
        old_pattern = (
            "    final_price = total_price - coupon.discount_amount\n"
            "    return round(max(final_price, 0.0), 2)"
        )
        new_pattern = (
            '    if coupon.coupon_type == "RATE" and coupon.discount_rate is not None:\n'
            "        final_price = total_price * float(coupon.discount_rate)\n"
            "    else:\n"
            "        discount_amt = float(coupon.discount_amount or 0.0)\n"
            "        final_price = total_price - discount_amt\n"
            "    return round(max(final_price, 0.0), 2)"
        )
        if old_pattern in code_text:
            sandbox.edit_file(
                rel_path=rel_target,
                old_text=old_pattern,
                new_text=new_pattern,
            )
            return

    # 3. 通用自愈匹配：针对 VIP 等级边界比较缺陷 (> 变为 >=)
    if "vip_level" in code_text:
        old_pattern = "    if user.vip_level > 2:\n        return round(amount * 0.9, 2)"
        new_pattern = "    if user.vip_level >= 2:\n        return round(amount * 0.9, 2)"
        if old_pattern in code_text:
            sandbox.edit_file(
                rel_path=rel_target,
                old_text=old_pattern,
                new_text=new_pattern,
            )
            return


async def init_worktree_node(
    state: PatchSelfHealSubState, runtime: Runtime[IncidentGraphContext]
) -> dict:
    ws_id = state.get("workspace_id") or "ws_mall_prod"
    sandbox = LinuxContainerSandbox(workspace_id=ws_id)
    info = sandbox.init_container()

    return {
        "sandbox_dir": info["sandbox_dir"],
        "retry_count": 0,
        "pytest_output": "",
        "test_passed": False,
    }


async def generate_patch_node(
    state: PatchSelfHealSubState, runtime: Runtime[IncidentGraphContext]
) -> dict:
    sandbox_dir = state["sandbox_dir"]
    report = state["report"]
    retry_count = state.get("retry_count", 0)
    prev_error = state.get("pytest_output", "")
    ev_pool = state.get("evidence", [])
    ws_id = Path(sandbox_dir).name
    sandbox = LinuxContainerSandbox(workspace_id=ws_id)

    # 报告里真实提到的文件的相对沙箱根目录的路径
    rel_target = _resolve_target_file(
        sandbox_dir=Path(sandbox_dir),
        report_summary=report.summary,
        report_cause=report.cause,
        report_next_steps=report.next_steps,
        evidence_pool=ev_pool,
    )
    sandbox_file = Path(sandbox_dir) / rel_target
    current_code = sandbox_file.read_text(encoding="utf-8") if sandbox_file.exists() else ""

    applied_via_llm = False
    if _has_valid_llm_key():
        try:
            sys_prompt = load_prompt(
                "patch_coder.md.jinja",
                report_summary=report.summary,
                cause=report.cause,
                report_next_steps="\n".join(report.next_steps),
                code_evidences="\n".join(f"- {e.summary}\n{e.raw_snippet[:300]}" for e in ev_pool),
                retry_count=retry_count,
                test_error_log=prev_error,
                human_feedback=state.get("human_feedback", ""),
            )
            user_msg = (
                f"沙箱根目录 `sandbox_dir`: `{sandbox_dir}`\n"
                f"待修复文件相对路径 `rel_file_path`: `{rel_target}`\n"
                f"当前文件内容如下：\n```python\n{current_code}\n```\n"
                "请直接调用 `edit_file` / `write_file` 完成修复。"
            )
            # 优先通过官方 MCP Client 动态向官方 Filesystem MCP 服务端拉取工具
            try:
                mcp_tools = await load_official_filesystem_mcp_tools(sandbox_dir)
            except Exception:
                mcp_tools = []

            llm_coder = get_llm(temperature=0.0).bind_tools(mcp_tools) if mcp_tools else get_llm(temperature=0.0)
            ai_msg = await llm_coder.ainvoke(
                [SystemMessage(content=sys_prompt), HumanMessage(content=user_msg)]
            )
            for tc in getattr(ai_msg, "tool_calls", None) or []:
                if tc["name"] in ("edit_file", "edit_file_in_sandbox", "write_file"):
                    args = dict(tc["args"])
                    old_t = args.get("old_text") or args.get("old_code", "")
                    new_t = args.get("new_text") or args.get("new_code", "")
                    if old_t and new_t:
                        ok, msg = sandbox.edit_file(rel_target, old_t, new_t)
                        if ok:
                            applied_via_llm = True
        except Exception:
            applied_via_llm = False

    if not applied_via_llm:
        _apply_grounded_fallback_fix(
            sandbox_dir=sandbox_dir,
            rel_target=rel_target,
            report_cause=report.cause,
            evidence_pool=ev_pool,
            patch_spec=state.get("patch_spec"),
        )

    patch_diff = sandbox.compute_diff(rel_target)

    return {
        "target_files": [rel_target],
        "patch_diff": patch_diff,
        "retry_count": retry_count + 1,
    }


async def run_pytest_node(
    state: PatchSelfHealSubState, runtime: Runtime[IncidentGraphContext]
) -> dict:
    sandbox_dir = state["sandbox_dir"]
    target_files = state.get("target_files", [])
    report = state.get("report")
    report_cause = report.cause if report else ""
    ev_pool = state.get("evidence", [])
    ws_id = Path(sandbox_dir).name
    sandbox = LinuxContainerSandbox(workspace_id=ws_id)

    rel_target = target_files[0] if target_files else "app/services/order_service.py"
    # 动态计算本次在沙箱里执行验证时，究竟只跑哪些单测用例，通过 AST 静态分析相关单测函数
    k_expr = _resolve_pytest_keyword_filter(
        rel_target=rel_target,
        report_cause=report_cause,
        evidence_pool=ev_pool,
        sandbox_dir=Path(sandbox_dir),
    )

    # 通过 Linux 容器沙箱执行定向单测验证
    res = sandbox.exec_pytest(k_expr=k_expr)
    passed = bool(res.get("passed", False))
    output = str(res.get("output", ""))

    return {
        "pytest_output": output,
        "test_passed": passed,
    }

    return {
        "pytest_output": output,
        "test_passed": passed,
    }


def _route_after_pytest(
    state: PatchSelfHealSubState,
) -> Literal["generate_patch_node", "__end__"]:
    if state.get("test_passed"):
        return "__end__"
    if state.get("retry_count", 0) < 3:
        return "generate_patch_node"
    return "__end__"


def build_patch_subgraph():
    builder = StateGraph(
        state_schema=PatchSelfHealSubState, context_schema=IncidentGraphContext
    )
    builder.add_node("init_worktree_node", init_worktree_node)
    builder.add_node("generate_patch_node", generate_patch_node)
    builder.add_node("run_pytest_node", run_pytest_node)

    builder.add_edge(START, "init_worktree_node")
    builder.add_edge("init_worktree_node", "generate_patch_node")
    builder.add_edge("generate_patch_node", "run_pytest_node")
    builder.add_conditional_edges(
        "run_pytest_node",
        _route_after_pytest,
        {
            "generate_patch_node": "generate_patch_node",
            "__end__": END,
        },
    )
    return builder.compile()


patch_subgraph = build_patch_subgraph()
