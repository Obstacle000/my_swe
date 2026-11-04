"""沙箱自愈编码子图专用文件修改工具集 (SANDBOX_TOOLS)：严禁操作正式主工作区目录"""

import ast
from pathlib import Path
from langchain_core.tools import tool
from pydantic import BaseModel, Field


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


@tool("edit_file_in_sandbox", args_schema=EditFileInSandboxInput)
def edit_file_in_sandbox(
    sandbox_dir: str,
    rel_file_path: str,
    old_text: str,
    new_text: str,
) -> str:
    """在隔离沙箱目录中精确替换指定源码文件的缺陷代码片段，并自动执行 Python AST 语法校验。"""
    sandbox_root = Path(sandbox_dir).resolve()
    if not sandbox_root.exists():
        return f"错误：沙箱目录不存在 ({sandbox_dir})"

    cleaned_rel = rel_file_path.strip().lstrip("/\\")
    if cleaned_rel.startswith("target_mall_app/") or cleaned_rel.startswith("target_mall_app\\"):
        cleaned_rel = cleaned_rel[len("target_mall_app/") :]

    target_file = (sandbox_root / cleaned_rel).resolve()
    try:
        target_file.relative_to(sandbox_root)
    except ValueError:
        return f"安全拦截：尝试修改沙箱目录之外的文件 ({rel_file_path})。"

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


SANDBOX_TOOLS = [edit_file_in_sandbox]
