"""FastAPI 前后端交互接口请求/响应 DTO Schema (对应《5_API _DOC.md》)"""

from typing import Any, Literal
from pydantic import BaseModel, Field


class ApiResponse(BaseModel):
    """统一 RESTful JSON 响应信封"""

    code: int = Field(default=200, description="业务状态码，200表示成功")
    message: str = Field(default="success", description="响应消息提示")
    data: Any = Field(default=None, description="响应载荷数据")


class TriggerFaultRequest(BaseModel):
    """靶场故障一键注入请求体 (用于演示与评测)"""

    scenario_id: Literal[
        "case_1_coupon_null_500",
        "case_2_vip_discount_bug",
        "case_3_sms_timeout_recurrence",
    ] = Field(default="case_1_coupon_null_500", description="预设的 3 大典型故障剧本编码")
    custom_params: dict[str, Any] = Field(default_factory=dict, description="可选附加参数")


class CreateIncidentRequest(BaseModel):
    """新建排障会话请求体"""

    workspace_id: str = Field(default="ws_mall_prod", description="所属工作空间ID")
    trigger_source: Literal["user_chat", "alert_webhook", "range_inject"] = Field(
        default="user_chat", description="排障触发来源"
    )
    initial_query: str = Field(..., min_length=2, description="用户首次输入的故障现象描述")


class ChatStreamRequest(BaseModel):
    """在排障会话中发起 SSE 流式诊断的请求体"""

    message: str = Field(..., min_length=1, description="用户输入的故障描述或补充线索")


class ApprovalRequest(BaseModel):
    """人工审批代码补丁请求体 (用于通过 Command(resume=...) 唤醒 LangGraph interrupt 断点)"""

    decision: Literal["approved", "rejected"] = Field(..., description="人工审批动作: approved / rejected")
    feedback: str = Field(default="", description="审批备注或驳回重写要求")


class HybridSearchDebugRequest(BaseModel):
    """CRAG 双路 RRF 混合检索调试台请求体"""

    query: str = Field(..., min_length=2, description="检索测试查询词")
    top_k: int = Field(default=3, ge=1, le=10)
