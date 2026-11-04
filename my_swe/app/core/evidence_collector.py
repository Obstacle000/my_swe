"""证据采集与敏感信息自动脱敏模块 (复刻自 open-swe/openswe/incidents/collector.py)"""

import hashlib
import json
import re
from typing import Literal

from app.config import now_iso
from app.schemas.incident_schemas import Evidence, EvidenceSource

# 敏感信息脱敏正则规则集
_SECRET_PATTERNS = [
    (re.compile(r"(?i)(api[_-]?key|secret|password|passwd|token)\s*[:=]\s*['\"]?([A-Za-z0-9_\-\.]{6,})['\"]?"), r"\1=***REDACTED***"),
    (re.compile(r"\b1[3-9]\d{9}\b"), "138****REDACTED"),
    (re.compile(r"\bsk-[A-Za-z0-9]{12,}\b"), "sk-***REDACTED***"),
]

# 工具名到精细化证据来源与 ID 前缀的映射表
_TOOL_SOURCE_MAP: dict[str, tuple[EvidenceSource, str]] = {
    "search_error_logs": ("error_log", "ev_log"),
    "get_trace_logs": ("trace_log", "ev_log"),
    "ls_directory": ("code_search", "ev_code"),
    "grep_code": ("code_search", "ev_code"),
    "read_file_window": ("source_code", "ev_code"),
    "git_blame_diff": ("git_history", "ev_git"),
    "inspect_table_schema": ("db_schema", "ev_db"),
    "execute_readonly_sql": ("db_record", "ev_db"),
    "crag_runbook_search": ("crag_runbook", "ev_hist"),
}


def redact_text(text: str, max_chars: int = 3800) -> str:
    """对工具返回的原始输出执行敏感凭证脱敏与长度截断"""
    cleaned = text or ""
    for pattern, replacement in _SECRET_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars] + "\n...[TRUNCATED BY EVIDENCE COLLECTOR]..."
    return cleaned


def build_evidence(
    tool_name: str,
    tool_args: dict,
    raw_output: str,
    summary_override: str | None = None,
) -> Evidence:
    """将工具调用结果封装并签发为不可伪造的 Evidence 对象"""
    source, prefix = _TOOL_SOURCE_MAP.get(tool_name, ("codebase", "ev_code"))
    args_json = json.dumps(tool_args, ensure_ascii=False, sort_keys=True)
    # 基于工具名、参数和输出内容前 512 字节计算确定性短哈希 ID
    digest_seed = f"{tool_name}:{args_json}:{raw_output[:512]}"
    short_hash = hashlib.sha1(digest_seed.encode("utf-8")).hexdigest()[:6]
    evidence_id = f"{prefix}:{short_hash}"

    redacted_snippet = redact_text(raw_output, max_chars=3800)
    if summary_override:
        summary = summary_override[:480]
    else:
        first_line = next((line.strip() for line in redacted_snippet.splitlines() if line.strip()), "Tool Output")
        summary = f"[{tool_name}] 参数 {args_json} -> {first_line}"[:480]

    return Evidence(
        id=evidence_id,
        source=source,
        tool_name=tool_name,
        query_args=args_json,
        summary=summary,
        raw_snippet=redacted_snippet,
        retrieved_at=now_iso(),
    )
