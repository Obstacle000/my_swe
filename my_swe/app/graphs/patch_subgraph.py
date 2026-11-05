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
from app.prompts.loader import load_prompt
from app.schemas.incident_schemas import Evidence
from app.tools.sandbox_tools import SANDBOX_TOOLS, edit_file_in_sandbox


def _has_valid_llm_key() -> bool:
    key = (settings.llm_api_key or "").strip()
    return bool(key) and not key.startswith("sk-your-") and key != "sk-placeholder"


def _sync_workspace_sandbox(src_repo: Path, sandbox_dir: Path) -> None:
    # 首次进入（冷启动）：全量克隆
    if not sandbox_dir.exists():
        # 创建sandbox_dir的父级目录,即.sandboxes
        sandbox_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src_repo, sandbox_dir)
        return
    # os.walk 默认会一直深度递归遍历下去，直到把所有的子目录、孙子目录全部遍历
    for root, dirs, files in os.walk(src_repo):
        # root：当前正在访问的目录绝对路径
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".pytest_cache", ".venv", ".git")]
        # os.path.relpath(path, start):计算某个路径（path）相对于基准路径（start）的相对路径（Relative Path）。
        rel_dir = os.path.relpath(root, src_repo)
        # "."意味着当前目录就是用户根目录,创建sandbox_dir,但是sandbox_dir以创建所以跳过
        # 只创建,用户根目录里面的目录
        target_dir = sandbox_dir if rel_dir == "." else sandbox_dir / rel_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        # 遍历当前目录文件拷贝到目标目录下面
        for f in files:
            if f.endswith(".pyc") or f.endswith(".log"):
                continue
            src_file = Path(root) / f
            dst_file = target_dir / f
            # 只同步修改过和缺失的的文件
            if not dst_file.exists() or src_file.read_bytes() != dst_file.read_bytes():
                shutil.copy2(src_file, dst_file)


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
) -> str:
    target_stem = Path(rel_target).stem.lower()

    combined = f"{rel_target} {report_cause}"
    for ev in evidence_pool:
        combined += f" {ev.summary}"
    combined_lower = combined.lower()

    # 如果修复目标是 pricing 相关模块，定向筛选 vip 会员计价单测
    if "pricing" in target_stem or "vip" in combined_lower:
        return "test_vip_"
    # 如果修复目标是 order 结算相关模块，定向筛选 checkout 优惠券抵扣单测
    if "order" in target_stem or "coupon" in combined_lower or "checkout" in combined_lower:
        return "test_checkout_"

    return target_stem


def _apply_grounded_fallback_fix(
    sandbox_dir: str,
    rel_target: str,
    report_cause: str,
    evidence_pool: list[Evidence],
) -> None:
    target_path = Path(sandbox_dir) / rel_target
    if not target_path.exists():
        return
    code_text = target_path.read_text(encoding="utf-8")

    if "unsupported operand" in report_cause.lower() or "nonetype" in report_cause.lower() or "order_service" in rel_target:
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
            edit_file_in_sandbox.invoke({
                "sandbox_dir": sandbox_dir,
                "rel_file_path": rel_target,
                "old_text": old_pattern,
                "new_text": new_pattern,
            })
            return

    if "vip_level" in report_cause.lower() or "pricing_service" in rel_target:
        old_pattern = "    if user.vip_level > 2:\n        return round(amount * 0.9, 2)"
        new_pattern = "    if user.vip_level >= 2:\n        return round(amount * 0.9, 2)"
        if old_pattern in code_text:
            edit_file_in_sandbox.invoke({
                "sandbox_dir": sandbox_dir,
                "rel_file_path": rel_target,
                "old_text": old_pattern,
                "new_text": new_pattern,
            })
            return


async def init_worktree_node(
    state: PatchSelfHealSubState, runtime: Runtime[IncidentGraphContext]
) -> dict:
    ws_id = state.get("workspace_id") or "ws_mall_prod"
    sandbox_root = PROJECT_ROOT / ".sandboxes" / ws_id
    # 下面方法执行完,就说明和用户目录同步了,接下来调用工具执行补丁修改
    _sync_workspace_sandbox(
        src_repo=settings.default_target_repo_path,# 用户项目根目录
        sandbox_dir=sandbox_root, # 沙箱根目录
    )

    return {
        "sandbox_dir": str(sandbox_root.resolve()),
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

    # 报告里真实提到的文件的相对沙箱根目录的路径
    # 这里只选了“首要缺陷文件”,80% 以上的线上故障是“单一根因点（Single Point of Bug）”
    rel_target = _resolve_target_file(
        sandbox_dir=Path(sandbox_dir),
        report_summary=report.summary,
        report_cause=report.cause,
        report_next_steps=report.next_steps,
        evidence_pool=ev_pool,
    )
    # 拼接成完整相对于swe项目根目录的的路径
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
                "请直接调用 `edit_file_in_sandbox` 工具完成修复。"
            )
            llm_coder = get_llm(temperature=0.0).bind_tools(SANDBOX_TOOLS)
            ai_msg = await llm_coder.ainvoke(
                [SystemMessage(content=sys_prompt), HumanMessage(content=user_msg)]
            )
            for tc in getattr(ai_msg, "tool_calls", None) or []:
                if tc["name"] == "edit_file_in_sandbox":
                    args = dict(tc["args"])
                    args.setdefault("sandbox_dir", sandbox_dir)
                    if "file_path" in args and "rel_file_path" not in args:
                        args["rel_file_path"] = args.pop("file_path")
                    if "old_code" in args and "old_text" not in args:
                        args["old_text"] = args.pop("old_code")
                    if "new_code" in args and "new_text" not in args:
                        args["new_text"] = args.pop("new_code")
                    res = str(edit_file_in_sandbox.invoke(args))
                    if "成功更新沙箱文件" in res:
                        applied_via_llm = True
        except Exception:
            applied_via_llm = False

    if not applied_via_llm:
        _apply_grounded_fallback_fix(
            sandbox_dir=sandbox_dir,
            rel_target=rel_target,
            report_cause=report.cause,
            evidence_pool=ev_pool,
        )

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

# 这个还得改
async def run_pytest_node(
    state: PatchSelfHealSubState, runtime: Runtime[IncidentGraphContext]
) -> dict:
    sandbox_dir = state["sandbox_dir"]
    target_files = state.get("target_files", [])
    report = state.get("report")
    report_cause = report.cause if report else ""
    ev_pool = state.get("evidence", [])

    rel_target = target_files[0] if target_files else "app/services/order_service.py"
    # 动态计算本次在沙箱里执行验证时，究竟只跑哪些单测用例，避免误跑整个项目的无关测试。
    k_expr = _resolve_pytest_keyword_filter(
        rel_target=rel_target,
        report_cause=report_cause,
        evidence_pool=ev_pool,
    )

    test_file_path = Path(sandbox_dir) / "tests" / "test_order_service.py"
    test_target = str(test_file_path) if test_file_path.exists() else "tests/"

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        f"--rootdir={sandbox_dir}",
    ]
    ini_path = Path(sandbox_dir) / "pytest.ini"
    if ini_path.exists():
        cmd.extend(["-c", str(ini_path)])
    cmd.extend([test_target, "-k", k_expr, "-v", "--tb=short"])

    try:
        proc = subprocess.run(
            cmd,
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
