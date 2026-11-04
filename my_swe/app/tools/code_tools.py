"""代码探针只读工具集 (CODE_TOOLS)：复刻自 open-swe/openswe/tools/filesystem.py 并带路径穿越防护"""

from pathlib import Path
import subprocess
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.config import settings


def _safe_resolve_in_repo(rel_path: str, repo_root: Path | None = None) -> Path:
    """将相对路径解析为目标仓库内的绝对路径，严格拦截 '../' 路径穿越越权访问"""
    base = (repo_root or settings.default_target_repo_path).resolve()
    cleaned = rel_path.strip().lstrip("/\\")
    # 若传入了前缀 target_mall_app/，自动剥离以兼容两种写法
    if cleaned.startswith("target_mall_app/") or cleaned.startswith("target_mall_app\\"):
        cleaned = cleaned[len("target_mall_app/") :]
    target = (base / cleaned).resolve()
    target.relative_to(base)  # 若越界会抛出 ValueError
    return target


class LsDirectoryInput(BaseModel):
    rel_path: str = Field(
        default=".",
        description="相对于被排查项目根目录的子目录路径，如 '.' 或 'app/services'",
    )


class GrepCodeInput(BaseModel):
    pattern: str = Field(
        ...,
        description="要在源码中全局搜索的函数名、类名或关键字符串，如 'checkout_order' 或 'vip_level'",
    )
    sub_dir: str = Field(
        default=".",
        description="限定搜索的子目录路径，默认为 '.' 搜索整个被排查仓库",
    )


class ReadFileWindowInput(BaseModel):
    file_path: str = Field(
        ...,
        description="源码文件相对路径，如 'app/services/order_service.py' 或 'config/settings.yaml'",
    )
    start_line: int = Field(default=1, ge=1, description="起始行号 (从 1 开始)")
    end_line: int = Field(default=100, ge=1, description="结束行号 (单次最多读取 120 行)")


class GitBlameDiffInput(BaseModel):
    file_path: str = Field(
        ...,
        description="要查询最近 Git 变更记录的文件相对路径，如 'app/services/order_service.py'",
    )


@tool("ls_directory", args_schema=LsDirectoryInput)
def ls_directory(rel_path: str = ".") -> str:
    """列出被排查业务项目指定目录下的文件与子目录清单。"""
    try:
        target_dir = _safe_resolve_in_repo(rel_path)
    except ValueError:
        return f"安全拦截：路径 '{rel_path}' 超出被排查项目根目录范围。"

    if not target_dir.exists() or not target_dir.is_dir():
        return f"目录不存在: {rel_path}"

    entries: list[str] = []
    for item in sorted(target_dir.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
        if item.name.startswith("__pycache__") or item.name.startswith(".pytest"):
            continue
        kind = "[DIR] " if item.is_dir() else "[FILE]"
        rel_str = item.relative_to(settings.default_target_repo_path.resolve()).as_posix()
        entries.append(f"{kind} {rel_str}")
    return "\n".join(entries) if entries else "(空目录)"


@tool("grep_code", args_schema=GrepCodeInput)
def grep_code(pattern: str, sub_dir: str = ".") -> str:
    """在被排查业务代码库中全局搜索指定关键字或符号，返回匹配的文件路径、行号与代码行内容。"""
    try:
        search_root = _safe_resolve_in_repo(sub_dir)
    except ValueError:
        return f"安全拦截：路径 '{sub_dir}' 超出被排查项目根目录范围。"

    if not search_root.exists():
        return f"路径不存在: {sub_dir}"

    matches: list[str] = []
    repo_base = settings.default_target_repo_path.resolve()
    files = [search_root] if search_root.is_file() else sorted(search_root.rglob("*"))

    for fp in files:
        if not fp.is_file() or "__pycache__" in fp.parts or fp.suffix not in {".py", ".yaml", ".yml", ".md", ".sql"}:
            continue
        try:
            content = fp.read_text(encoding="utf-8")
        except Exception:
            continue
        rel_fp = fp.relative_to(repo_base).as_posix()
        for line_no, line in enumerate(content.splitlines(), start=1):
            if pattern.lower() in line.lower():
                matches.append(f"{rel_fp}:{line_no}: {line.strip()}")
                if len(matches) >= 30:
                    return "\n".join(matches) + "\n...(匹配过多，已截断前 30 条)"

    if not matches:
        return f"未在代码库中找到包含 '{pattern}' 的代码行。"
    return "\n".join(matches)


@tool("read_file_window", args_schema=ReadFileWindowInput)
def read_file_window(file_path: str, start_line: int = 1, end_line: int = 100) -> str:
    """按行号窗口读取指定源码文件的内容（自动附带真实行号，供报告引用具体代码行）。"""
    try:
        target_file = _safe_resolve_in_repo(file_path)
    except ValueError:
        return f"安全拦截：文件路径 '{file_path}' 越界。"

    if not target_file.exists() or not target_file.is_file():
        return f"文件不存在: {file_path}"

    lines = target_file.read_text(encoding="utf-8").splitlines()
    start_idx = max(1, start_line)
    # 限制单次窗口最大 120 行，防止撑爆上下文
    end_idx = min(len(lines), max(start_idx, min(end_line, start_idx + 119)))

    numbered_lines = [
        f"{lineno:4d} | {lines[lineno - 1]}"
        for lineno in range(start_idx, end_idx + 1)
    ]
    rel_name = target_file.relative_to(settings.default_target_repo_path.resolve()).as_posix()
    header = f"=== File: {rel_name} (Lines {start_idx}-{end_idx} of {len(lines)}) ==="
    return header + "\n" + "\n".join(numbered_lines)


@tool("git_blame_diff", args_schema=GitBlameDiffInput)
def git_blame_diff(file_path: str) -> str:
    """查看指定源码文件最近的 Git 提交变更历史与变更说明，排查是否由近期上线变更引入。"""
    try:
        target_file = _safe_resolve_in_repo(file_path)
    except ValueError:
        return f"安全拦截：文件路径 '{file_path}' 越界。"

    if not target_file.exists():
        return f"文件不存在: {file_path}"

    rel_name = target_file.relative_to(settings.default_target_repo_path.resolve()).as_posix()
    # 尝试调用本地 git log，若靶场目录未初始化独立 git 仓库则返回预置上线变更审计记录
    try:
        proc = subprocess.run(
            ["git", "log", "-n", "2", "-p", "--", str(target_file)],
            cwd=str(settings.default_target_repo_path),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()[:2500]
    except Exception:
        pass

    # 内置靶场近期 Git 变更审计记录
    simulated_commits = {
        "app/services/order_service.py": (
            "commit 9f81b3e (Author: dev_bob, Date: 2026-11-03 22:10:00 +0800)\n"
            "Message: feat(order): 新增结算接口优惠券抵扣逻辑\n"
            "Diff summary: 在 checkout_order() 第 60-65 行新增了 `final_price = round(base_price - coupon['discount_amount'], 2)`，"
            "当时仅测试了 CASH 满减券，未判断 RATE 折扣券的 discount_amount 为 None 的情况。"
        ),
        "app/services/pricing_service.py": (
            "commit 4c21a09 (Author: dev_alice, Date: 2026-11-02 18:40:00 +0800)\n"
            "Message: refactor(pricing): 重构会员等级阶梯折扣计算函数\n"
            "Diff summary: 在 calculate_vip_discount() 第 42 行将黄金会员判断条件写成了 `elif vip_level > 2:`，导致 vip_level == 2 落入默认不打折分支。"
        ),
        "config/settings.yaml": (
            "commit 1d77e42 (Author: ops_charlie, Date: 2026-10-28 11:00:00 +0800)\n"
            "Message: chore(config): 配置阿里云短信主网关超时时间为 2.0s，暂未开启自动降级开关"
        ),
    }
    return simulated_commits.get(rel_name, f"文件 {rel_name} 近期无高危代码变更记录。")


CODE_TOOLS = [ls_directory, grep_code, read_file_window, git_blame_diff]
