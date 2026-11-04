"""LangGraph 全局状态机与三大子图隔离状态定义 (对应《02_STATE_SCHEMA_MODEL.md》第一部分)"""

import operator
from typing import Annotated, Literal, TypedDict
from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from app.schemas.incident_schemas import (
    Evidence,
    Hypothesis,
    IncidentReport,
    ReportDraft,
)


class IncidentMainState(TypedDict):
    """主图状态机 (貫穿排障会话生命周期，对应 thread_id = incident_id)"""

    # 1. 基础会话与消息上下文
    incident_id: str
    workspace_id: str
    messages: Annotated[list[AnyMessage], add_messages]

    # 2. 分诊节点 (triage_node) 提取的初步线索字典
    extracted_clues: dict[str, str]

    # 3. CRAG 历史故障检索子图回传的结果
    historical_context: str
    crag_hit: bool

    # 4. 规划与并行探针子图协同字段 (使用 operator.add 规避并行写冲突)
    hypotheses: list[Hypothesis]
    evidence: Annotated[list[Evidence], operator.add]
    loop_count: int

    # 5. 报告生成与防幻觉校验字段
    report_draft: ReportDraft | None
    final_report: IncidentReport | None
    verification_error: str
    verify_retry_count: int

    # 6. 沙箱自愈编码子图与 HITL 人工审批字段
    patch_diff: str
    test_passed: bool
    human_approval: Literal["pending", "approved", "rejected"]
    human_feedback: str


class CRAGSubState(TypedDict):
    """子图一：CRAG 纠错检索子图私有状态"""

    original_query: str
    current_query: str
    retrieved_docs: list[dict]
    relevant_docs: list[dict]
    retry_count: int
    # 回传给主图的输出字段
    historical_context: str
    crag_hit: bool
    evidence: Annotated[list[Evidence], operator.add]


class ProbeWorkerState(TypedDict):
    """子图二：单假设探针验证子图私有状态 (由 Send API 并行派生)"""

    hypothesis: Hypothesis
    clues: dict[str, str]
    worker_messages: Annotated[list[AnyMessage], add_messages]
    remaining_chars: int
    # 唯一合并回主图的增量证据列表
    evidence: Annotated[list[Evidence], operator.add]


class PatchSelfHealSubState(TypedDict):
    """子图三：沙箱代码修复与单测自愈子图私有状态"""

    incident_id: str
    report: IncidentReport
    evidence: list[Evidence]
    sandbox_dir: str
    target_files: list[str]
    patch_diff: str
    pytest_output: str
    test_passed: bool
    retry_count: int
