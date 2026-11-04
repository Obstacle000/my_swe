"""子图三：沙箱代码修复与单测自愈子图 (PatchSelfHealSubgraph)"""

import difflib
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
from app.prompts.loader import load_prompt
from app.tools.sandbox_tools import SANDBOX_TOOLS, edit_file_in_sandbox


def _has_valid_llm_key() -> bool:
    key = (settings.llm_api_key or "").strip()
    return bool(key) and not key.startswith("sk-your-") and key != "sk-placeholder"


async def init_worktree_node(
    state: PatchSelfHealSubState, runtime: Runtime[IncidentGraphContext]
) -> dict:
    """创建隔离沙箱目录并将被排查业务仓库完整复制进去，保护主工作区不被直接篡改"""
    inc_id = state.get("incident_id", "inc_default")
    sandbox_root = PROJECT_ROOT / ".sandboxes" / inc_id
    if sandbox_root.exists():
        shutil.rmtree(sandbox_root)
    sandbox_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(settings.default_target_repo_path, sandbox_root)

    return {
        "sandbox_dir": str(sandbox_root.resolve()),
        "retry_count": 0,
        "pytest_output": "",
        "test_passed": False,
    }


async def generate_patch_node(
    state: PatchSelfHealSubState, runtime: Runtime[IncidentGraphContext]
) -> dict:
    """根据 IncidentReport 根因与上一轮 pytest 失败报错，在隔离沙箱中修改源码并生成 Unified Diff"""
    sandbox_dir = state["sandbox_dir"]
    report = state["report"]
    retry_count = state.get("retry_count", 0)
    prev_error = state.get("pytest_output", "")

    cause_lower = f"{report.summary} {report.cause}".lower()
    is_pricing_bug = "pricing_service.py" in cause_lower or "vip_level" in cause_lower

    rel_target = (
        "app/services/pricing_service.py"
        if is_pricing_bug
        else "app/services/order_service.py"
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
                next_steps="\n".join(report.next_steps),
                retry_count=retry_count,
                test_error_log=prev_error,
                human_feedback=state.get("human_feedback", ""),
            )
            user_msg = (
                f"沙箱根目录 `sandbox_dir`: `{sandbox_dir}`\n"
                f"待修复文件相对路径 `rel_file_path`: `{rel_target}`\n"
                f"当前文件内容如下：\n```python\n{current_code}\n```\n"
                "请直接调用 `edit_file_in_sandbox` 工具完成修复。"
            )
            llm_coder = get_llm(temperature=0.0).bind_tools(SANDBOX_TOOLS)
            ai_msg = await llm_coder.ainvoke(
                [SystemMessage(content=sys_prompt), HumanMessage(content=user_msg)]
            )
            for tc in getattr(ai_msg, "tool_calls", None) or []:
                if tc["name"] == "edit_file_in_sandbox":
                    res = str(edit_file_in_sandbox.invoke(tc["args"]))
                    if "成功更新沙箱文件" in res:
                        applied_via_llm = True
        except Exception:
            applied_via_llm = False

    if not applied_via_llm:
        _apply_deterministic_sandbox_fix(sandbox_dir, rel_target)

    patch_diff = _compute_unified_diff(
        orig_root=settings.default_target_repo_path,
        sandbox_root=Path(sandbox_dir),
        rel_path=rel_target,
    )

    return {
        "target_files": [rel_target],
        "patch_diff": patch_diff,
        "retry_count": retry_count + 1,
    }


def _apply_deterministic_sandbox_fix(sandbox_dir: str, rel_target: str) -> None:
    """在沙箱中调用 edit_file_in_sandbox 工具执行精准补丁替换"""
    if rel_target == "app/services/pricing_service.py":
        edit_file_in_sandbox.invoke(
            {
                "sandbox_dir": sandbox_dir,
                "rel_file_path": "app/services/pricing_service.py",
                "old_text": "    if user.vip_level > 2:\n        return round(amount * 0.9, 2)",
                "new_text": "    if user.vip_level >= 2:\n        return round(amount * 0.9, 2)",
            }
        )
    else:
        old_snippet = (
            "    final_price = total_price - coupon.discount_amount\n"
            "    return round(max(final_price, 0.0), 2)"
        )
        new_snippet = (
            '    if coupon.coupon_type == "RATE" and coupon.discount_rate is not None:\n'
            "        final_price = total_price * float(coupon.discount_rate)\n"
            "    else:\n"
            "        discount_amt = float(coupon.discount_amount or 0.0)\n"
            "        final_price = total_price - discount_amt\n"
            "    return round(max(final_price, 0.0), 2)"
        )
        edit_file_in_sandbox.invoke(
            {
                "sandbox_dir": sandbox_dir,
                "rel_file_path": "app/services/order_service.py",
                "old_text": old_snippet,
                "new_text": new_snippet,
            }
        )


def _compute_unified_diff(orig_root: Path, sandbox_root: Path, rel_path: str) -> str:
    orig_file = orig_root / rel_path
    sand_file = sandbox_root / rel_path
    orig_lines = (
        orig_file.read_text(encoding="utf-8").splitlines(keepends=True)
        if orig_file.exists()
        else []
    )
    sand_lines = (
        sand_file.read_text(encoding="utf-8").splitlines(keepends=True)
        if sand_file.exists()
        else []
    )

    diff = difflib.unified_diff(
        orig_lines,
        sand_lines,
        fromfile=f"a/{rel_path}",
        tofile=f"b/{rel_path}",
        lineterm="\n",
    )
    return "".join(diff)


async def run_pytest_node(
    state: PatchSelfHealSubState, runtime: Runtime[IncidentGraphContext]
) -> dict:
    """在隔离沙箱目录中拉起子进程执行真实 pytest 单元测试验证"""
    sandbox_dir = state["sandbox_dir"]
    target_files = state.get("target_files", [])
    is_pricing = any("pricing_service" in f for f in target_files)

    k_expr = (
        "test_vip_level_2_gold_member_discount"
        if is_pricing
        else "test_checkout_with_rate_discount_coupon or test_checkout_with_full_reduce_coupon"
    )

    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                f"--rootdir={sandbox_dir}",
                "-c",
                str(Path(sandbox_dir) / "pytest.ini"),
                "tests/test_order_service.py",
                "-k",
                k_expr,
                "-v",
                "--tb=short",
            ],
            cwd=sandbox_dir,
            capture_output=True,
            text=True,
            timeout=20,
        )
        output = (proc.stdout + "\n" + proc.stderr).strip()
        passed = proc.returncode == 0
    except Exception as exc:
        output = f"pytest 执行异常: {exc}"
        passed = False

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
    """构建并编译带 IncidentGraphContext 上下文 Schema 的沙箱自愈编码子图"""
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
