"""LangGraph 全局状态机与三大子图隔离状态定义 (对应《02_STATE_SCHEMA_MODEL.md》第一部分)"""

import operator
from typing import Annotated, Literal, NotRequired, TypedDict
from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from app.schemas.incident_schemas import (
    Evidence,
    Hypothesis,
    IncidentReport,
    ReportDraft,
    TriageClues,
)


class IncidentMainState(TypedDict):
    """主图状态机 (贯穿排障会话生命周期，对应 thread_id = incident_id)"""

    # 1. 基础输入与提取出的线索
    incident_id: str
    workspace_id: str
    user_query: NotRequired[str]                                   # 用户的原始提问
    extracted_clues: NotRequired[TriageClues]                      # 提炼出的结构化线索 (TriageClues Schema 对象)
    messages: Annotated[list[AnyMessage], add_messages]            # 主图消息历史

    # 2. CRAG 历史故障检索子图回传的结果
    historical_context: NotRequired[str]                           # 只取检索分数最高的一次记录
    crag_hit: NotRequired[bool]                                    # 表示 CRAG 历史故障检索子图有没有真正命中高度相关的历史同类故障。

    # 3. 规划与并行探针子图协同字段 (使用 operator.add 规避并行写冲突)
    hypotheses: NotRequired[list[Hypothesis]]
    # 各探针子图汇总上来的真实证据池,后面需要用这个对每一个假说里的证据进行验证,防止模型幻觉
    evidence: Annotated[list[Evidence], operator.add]
    gaps: Annotated[list[str], operator.add]                       # 各探针与验钞节点汇总上来的排查盲区
    evidence_sufficient: NotRequired[bool]                         # 证据评估节点判定当前证据是否足以锁定根因
    evaluate_retry_reason: NotRequired[str]                        # 证据审查节点(evidence_evaluate_node)在判定证据不足打回重规划时写给 planner 的打回批注与待补线索
    # 主图“提假设 → 派探针取证 → 评估证据是否充足”这个外层大循环的轮数计数器
    # 如果第一轮探针证据不足，且循环次数 < 2，允许打回 hypothesis_planner_node 带着缺失线索重新提假设再查一轮
    loop_count: int

    # 4. 报告生成与防幻觉校验字段
    report_draft: NotRequired[ReportDraft | None]
    final_report: NotRequired[IncidentReport | None]
    # 防幻觉验钞节点（verify_report_node）在拦截大模型伪造证据时，写给报告生成节点（generate_report_node）的“打回重写批注”。
    verification_error: NotRequired[str]
    verify_retry_count: int

    # 5. 沙箱自愈编码子图与 HITL 人工审批字段
    patch_diff: NotRequired[str]
    test_passed: bool
    human_approval: Literal["pending", "approved", "rejected"]
    human_feedback: NotRequired[str]


class CRAGSubState(TypedDict):
    """子图一：CRAG 纠错检索子图私有状态"""

    original_query: str
    current_query: str                                              # 子图内部重写后的搜索词
    retrieved_docs: NotRequired[list[dict]]
    relevant_docs: NotRequired[list[dict]]
    retry_count: int
    # 回传给主图的输出字段
    historical_context: NotRequired[str]
    crag_hit: bool                                                  # 表示 CRAG 历史故障检索子图有没有真正命中高度相关的历史同类故障。
    evidence: Annotated[list[Evidence], operator.add]


class ProbeWorkerState(TypedDict):
    """子图二：单假设探针验证子图私有状态 (由 Send API 并行派生)"""

    hypothesis: Hypothesis
    clues: TriageClues
    known_facts: NotRequired[list[str]]                            # 主图已查明的现场确凿事实/前序证据摘要（只读情报下发）
    worker_messages: Annotated[list[AnyMessage], add_messages]     # 子图私有工具调用对话过程，不污染主图
    remaining_chars: int                                           # 当前探针的字符预算上限 (15000)
    # 跑完后自动通过 operator.add 合并回主图 IncidentMainState 的字段：
    evidence: Annotated[list[Evidence], operator.add]
    gaps: Annotated[list[str], operator.add]


class PatchSelfHealSubState(TypedDict):
    """子图三：沙箱代码修复与单测自愈子图私有状态"""

    incident_id: str
    workspace_id: NotRequired[str]                                 # 对应所属靶场工作区 (用于沙箱目录复用与增量基线同步)
    report: IncidentReport
    evidence: list[Evidence]
    sandbox_dir: NotRequired[str]
    target_files: NotRequired[list[str]]
    patch_diff: NotRequired[str]
    pytest_output: NotRequired[str]
    test_passed: bool
    retry_count: int
    human_feedback: NotRequired[str]
