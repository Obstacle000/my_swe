"""核心排障与大模型交互契约 Schema 定义 (对应《02_STATE_SCHEMA_MODEL.md》)"""

from typing import Literal
from pydantic import BaseModel, Field


class TriageClues(BaseModel):
    """分诊节点 (triage_node) 从用户提问中提取的结构化线索"""

    user_id: str = Field(default="", description="涉及的用户ID，如 10086")
    api_path: str = Field(default="", description="涉及的接口路由路径，如 /api/orders/checkout")
    error_keyword: str = Field(default="", description="报错关键词或异常类名，如 TypeError, 500")
    module_hint: str = Field(default="", description="推测相关的业务模块名，如 order, pricing, sms")
    title: str = Field(default="故障排查会话", description="15字以内的排障会话简短标题")


class Evidence(BaseModel):
    """由工具包装器自动签发的真实证据记录 (大模型不可篡改或伪造)"""

    id: str = Field(..., description="唯一证据编号，如 ev_log:a1b2, ev_code:c3d4, ev_db:e5f6")
    source: Literal["es_log", "codebase", "mysql_db", "git_history", "runbook"]
    tool_name: str = Field(..., description="产生该证据的工具名称")
    query_args: str = Field(..., description="调用工具时的参数 JSON 字符串")
    summary: str = Field(..., max_length=500, description="证据简述")
    raw_snippet: str = Field(..., max_length=4000, description="经 redact_text() 脱敏与限长后的真实内容片段")
    retrieved_at: str = Field(..., description="证据采集时间戳")


class Claim(BaseModel):
    """报告草稿中的单句断言，强制要求绑定真实存在的 evidence_ids"""

    text: str = Field(..., max_length=1200, description="单句排障结论陈述文本")
    evidence_ids: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="支撑该结论的真实证据ID列表，如 ['ev_log:8f3a1c', 'ev_code:9b2d01']",
    )


class Hypothesis(BaseModel):
    """排障假设对象及其验证状态"""

    id: str = Field(..., description="假设编号，如 h1, h2, h3")
    title: str = Field(..., max_length=500, description="派发给探针子图查证的具体假设内容")
    probe_type: Literal["log", "code", "db"] = Field(
        ..., description="决定探针子图挂载哪套工具的路由字段: log(ES日志) / code(本地代码) / db(MySQL数据)"
    )
    assessment: Literal["supported", "plausible", "rejected"] = Field(
        default="plausible",
        description="验证状态: plausible(待查证) / supported(已证实) / rejected(已排除)",
    )
    evidence_ids: list[str] = Field(default_factory=list, description="证实或排除该假设的证据ID列表")


class HypothesisPlanList(BaseModel):
    """规划节点 (hypothesis_planner_node) 的结构化输出包装器"""

    hypotheses: list[Hypothesis] = Field(
        ..., max_length=3, description="每次生成的 1~3 个并行待验证排障假设"
    )


class ReportDraft(BaseModel):
    """给大模型填写的待审报告草稿 Schema (对应 open-swe 的 ReportDraft)"""

    summary: list[Claim] = Field(default_factory=list, max_length=4, description="一句话总结核心结论")
    problem: list[Claim] = Field(default_factory=list, max_length=4, description="明确报错接口与异常现象")
    previous_occurrence: list[Claim] = Field(
        default_factory=list, max_length=3, description="历史故障库复发命中情况"
    )
    impact: list[Claim] = Field(default_factory=list, max_length=4, description="故障影响的业务范围")
    cause: list[Claim] = Field(
        default_factory=list, max_length=4, description="经证实的确凿根因(必须绑定真实证据ID)"
    )
    next_steps: list[Claim] = Field(
        default_factory=list, max_length=3, description="具体修复步骤或运维建议"
    )
    hypotheses: list[Hypothesis] = Field(
        default_factory=list, max_length=6, description="更新验证状态后的假设列表"
    )
    gaps: list[str] = Field(
        default_factory=list, max_length=10, description="排查盲区(无需绑定证据ID)"
    )
    questions: list[str] = Field(
        default_factory=list, max_length=5, description="线索不足时向用户反问的问题(无需绑定证据ID)"
    )
    need_code_patch: bool = Field(
        default=False,
        description="若根因属于本地源码缺陷需生成修复补丁则设为 True；若属于外部限流/配置开关则设为 False",
    )


class IncidentReport(BaseModel):
    """经过 valid_refs 防幻觉校验后生成的正式排障报告 (对应 open-swe 的 IncidentReport)"""

    incident_id: str
    summary: str
    problem: str
    previous_occurrence: str
    impact: str
    cause: str
    next_steps: list[str]
    outcome: Literal["inconclusive", "findings"]
    need_code_patch: bool
    hypotheses: list[Hypothesis]
    evidence: list[Evidence]
    gaps: list[str]
    questions: list[str]
    digest: str = Field(..., description="核心结论的 64 位 SHA-256 指纹 (fingerprint)")
    created_at: str


class RetrievalGrade(BaseModel):
    """CRAG 子图中 Grader 相关性裁判节点的结构化输出"""

    relevance_score: Literal["yes", "no"] = Field(
        ..., description="检索到的历史故障文档是否与当前故障高度相关"
    )
    reason: str = Field(..., max_length=300, description="判定相关或不相关的简短理由")
