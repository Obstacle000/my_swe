"""提示词加载与 Jinja2 模板渲染器 (复刻自 open-swe/openswe/prompts.py)"""

from collections.abc import Mapping
from functools import cache
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, Undefined

PROMPTS_DIR = Path(__file__).resolve().parent

_JINJA = Environment(
    loader=FileSystemLoader(str(PROMPTS_DIR)),
    undefined=Undefined,
    autoescape=False,
    keep_trailing_newline=True,
)


def _normalize_name(name: str) -> str:
    if name.endswith(".md.jinja"):
        return name[: -len(".md.jinja")]
    if name.endswith(".md"):
        return name[: -len(".md")]
    return name


def _is_template(base_name: str) -> bool:
    return (PROMPTS_DIR / f"{base_name}.md.jinja").is_file()


@cache
def _load_static_prompt(filename: str) -> str:
    return (PROMPTS_DIR / filename).read_text(encoding="utf-8").strip()


def prompt(name: str, values: Mapping[str, object] | None = None, /, **kwargs: object) -> str:
    """
    渲染 .md.jinja 动态提示词模板，或直接读取静态 .md 提示词文件。
    用法示例:
        prompt("incident_report")
        prompt("probe_worker", probe_type="code", hypothesis_title="...")
    """
    base_name = _normalize_name(name)
    substitutions = {**(values or {}), **kwargs}
    # 兼容别名映射
    if "clues_text" in substitutions and "clues_summary" not in substitutions:
        substitutions["clues_summary"] = substitutions["clues_text"]
    if "cause" in substitutions and "report_cause" not in substitutions:
        substitutions["report_cause"] = substitutions["cause"]
    if "next_steps" in substitutions and "report_next_steps" not in substitutions:
        substitutions["report_next_steps"] = substitutions["next_steps"]

    if _is_template(base_name):
        return _JINJA.get_template(f"{base_name}.md.jinja").render(substitutions).strip()
    return _load_static_prompt(f"{base_name}.md")


# 兼容别名
load_prompt = prompt
