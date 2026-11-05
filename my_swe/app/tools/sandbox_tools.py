"""沙箱自愈编码子图专用 MCP 工具集 (SANDBOX_TOOLS)：严禁操作正式主工作区目录"""

import ast
import fnmatch
from pathlib import Path
from langchain_core.tools import tool
from pydantic import BaseModel, Field


class LocateSandboxFilesInput(BaseModel):
    sandbox_dir: str = Field(
        ...,
        description="沙箱隔离工作目录的绝对路径",
    )
    pattern: str = Field(
        default="*.py",
        description="文件名匹配模式，如 '*.py'、'*order*' 或 'app/services/*.py'",
    )


class ReadSandboxFileWindowInput(BaseModel):
    sandbox_dir: str = Field(
        ...,
        description="沙箱隔离工作目录的绝对路径",
    )
    rel_file_path: str = Field(
        ...,
        description="文件在沙箱内的相对路径，如 'app/services/order_service.py'",
    )
    start_line: int = Field(default=1, ge=1, description="起始行号 (从 1 开始)")
    end_line: int = Field(default=100, ge=1, description="结束行号")


class EditFileInSandboxInput(BaseModel):
    sandbox_dir: str = Field(
        ...,
        description="沙箱隔离工作目录的绝对路径（由 PatchSelfHealSubState 提供）",
    )
    rel_file_path: str = Field(
        ...,
        description="要修复的文件在沙箱内的相对路径，如 'app/services/order_service.py' 或 'app/services/pricing_service.py'",
    )
    old_text: str = Field(
        ...,
        description="原文件中需要被替换的精确代码片段（必须与原文件完全一致）",
    )
    new_text: str = Field(
        ...,
        description="修复后的新代码片段",
    )


def _safe_resolve_sandbox_path(sandbox_dir: str, rel_file_path: str) -> tuple[Path, str, str | None]:
    sandbox_root = Path(sandbox_dir).resolve()
    if not sandbox_root.exists():
        return sandbox_root, "", f"错误：沙箱目录不存在 ({sandbox_dir})"

    cleaned_rel = rel_file_path.strip().lstrip("/\\")
    if cleaned_rel.startswith("target_mall_app/") or cleaned_rel.startswith("target_mall_app\\"):
        cleaned_rel = cleaned_rel[len("target_mall_app/") :]

    target_file = (sandbox_root / cleaned_rel).resolve()
    try:
        target_file.relative_to(sandbox_root)
    except ValueError:
        return sandbox_root, cleaned_rel, f"安全拦截：尝试访问沙箱目录之外的文件 ({rel_file_path})。"

    return target_file, cleaned_rel, None


@tool("locate_sandbox_files", args_schema=LocateSandboxFilesInput)
def locate_sandbox_files(sandbox_dir: str, pattern: str = "*.py") -> str:
    """在沙箱隔离工作目录中检索匹配的文件相对路径列表（遵循 MCP 文件定位规范）。"""
    sandbox_root = Path(sandbox_dir).resolve()
    if not sandbox_root.exists():
        return f"错误：沙箱目录不存在 ({sandbox_dir})"

    matched_files: list[str] = []
    for p in sandbox_root.rglob("*"):
        if not p.is_file() or "__pycache__" in p.parts or ".pytest" in p.parts:
            continue
        rel = p.relative_to(sandbox_root).as_posix()
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(p.name, pattern):
            matched_files.append(rel)

    if not matched_files:
        return f"在沙箱中未找到匹配 '{pattern}' 的文件。"
    return "\n".join(matched_files)


@tool("read_sandbox_file_window", args_schema=ReadSandboxFileWindowInput)
def read_sandbox_file_window(
    sandbox_dir: str,
    rel_file_path: str,
    start_line: int = 1,
    end_line: int = 100,
) -> str:
    """按行号窗口读取沙箱内的源码文件，带行号前缀以辅助精确比对缩进与缺陷上下文。"""
    target_file, cleaned_rel, err = _safe_resolve_sandbox_path(sandbox_dir, rel_file_path)
    if err:
        return err
    if not target_file.exists():
        return f"错误：沙箱内不存在文件 {cleaned_rel}"

    lines = target_file.read_text(encoding="utf-8").splitlines()
    total_lines = len(lines)
    start_idx = max(1, start_line)
    end_idx = min(total_lines, end_line)

    if start_idx > total_lines:
        return f"文件 {cleaned_rel} 总行数为 {total_lines}，起始行号超出范围。"

    numbered_lines = [
        f"{idx:4d} | {lines[idx - 1]}"
        for idx in range(start_idx, end_idx + 1)
    ]
    header = f"=== File: {cleaned_rel} (Lines {start_idx}-{end_idx} of {total_lines}) ===\n"
    return header + "\n".join(numbered_lines)


@tool("edit_file_in_sandbox", args_schema=EditFileInSandboxInput)
def edit_file_in_sandbox(
    sandbox_dir: str,
    rel_file_path: str,
    old_text: str,
    new_text: str,
) -> str:
    """在隔离沙箱目录中精确替换指定源码文件的缺陷代码片段，并自动执行 Python AST 语法校验。"""
    target_file, cleaned_rel, err = _safe_resolve_sandbox_path(sandbox_dir, rel_file_path)
    if err:
        return err
    if not target_file.exists():
        return f"错误：沙箱内不存在文件 {cleaned_rel}"

    original_content = target_file.read_text(encoding="utf-8")
    if old_text not in original_content:
        return (
            f"替换失败：在 {cleaned_rel} 中未找到完全匹配的 old_text 代码片段。"
            "请核对缩进与上下文字符。"
        )

    updated_content = original_content.replace(old_text, new_text, 1)

    # 对 Python 源码文件执行 AST 语法预校验，防止写入语法损坏的代码
    if target_file.suffix == ".py":
        try:
            ast.parse(updated_content, filename=cleaned_rel)
        except SyntaxError as syn_err:
            return (
                f"语法校验未通过 (SyntaxError)，已拒绝写入 {cleaned_rel}：\n"
                f"Line {syn_err.lineno}: {syn_err.msg}\n{syn_err.text}"
            )

    target_file.write_text(updated_content, encoding="utf-8")
    return f"成功更新沙箱文件: {cleaned_rel}（已通过 Python AST 语法检查）。"


SANDBOX_TOOLS = [
    locate_sandbox_files,
    read_sandbox_file_window,
    edit_file_in_sandbox,
]

