"""模块二与模块三：排障会话生命周期管理、SSE 流式推理与 HITL 人工审批路由控制器
参考 my_nl2sql：每个接口均通过 Annotated 依赖注入 (IncidentServiceDep) 获取装配好 Repository 的服务实例。
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.api.deps import IncidentServiceDep
from app.schemas.api_schemas import (
    ApiResponse,
    ApprovalRequest,
    ChatStreamRequest,
    CreateIncidentRequest,
)

router = APIRouter(prefix="/api/v1/incidents", tags=["2 & 3. 排障会话、SSE 流式追踪与 HITL 审批"])


@router.post("", response_model=ApiResponse)
async def create_incident_endpoint(
    req: CreateIncidentRequest,
    service: IncidentServiceDep,
) -> ApiResponse:
    """新建排障会话 (生成的 incident_id 兼任 LangGraph Checkpointer 的 thread_id)"""
    data = await service.create_incident(req)
    return ApiResponse(code=200, message="success", data=data)


@router.get("", response_model=ApiResponse)
async def list_incidents_endpoint(
    service: IncidentServiceDep,
    workspace_id: str = "ws_mall_prod",
) -> ApiResponse:
    """查询指定工作空间下的排障会话列表"""
    data = await service.list_incidents(workspace_id=workspace_id)
    return ApiResponse(code=200, message="success", data=data)


@router.get("/{incident_id}", response_model=ApiResponse)
async def get_incident_detail_endpoint(
    incident_id: str,
    service: IncidentServiceDep,
) -> ApiResponse:
    """获取单次排障会话的完整详情（含正式排障报告、证据链、沙箱补丁 Diff 与审批状态）"""
    data = await service.get_incident_detail(incident_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"排障会话 {incident_id} 不存在")
    return ApiResponse(code=200, message="success", data=data)


@router.post("/{incident_id}/chat-stream")
async def chat_stream_endpoint(
    incident_id: str,
    req: ChatStreamRequest,
    service: IncidentServiceDep,
) -> StreamingResponse:
    """核心排障对话与 LangGraph 节点级 SSE 实时流式推送接口（内部将注入的 Repo 放入 context 传给 astream）"""
    sse_generator = service.stream_incident_diagnosis(incident_id=incident_id, req=req)
    return StreamingResponse(sse_generator, media_type="text/event-stream")


@router.post("/{incident_id}/approval", response_model=ApiResponse)
async def approval_endpoint(
    incident_id: str,
    req: ApprovalRequest,
    service: IncidentServiceDep,
) -> ApiResponse:
    """人工审批唤醒接口：通过 Command(resume=...) 恢复挂起的 LangGraph 图实例并执行经验自进化归档"""
    result = await service.process_human_approval(incident_id=incident_id, req=req)
    if result is None:
        raise HTTPException(status_code=404, detail=f"未找到会话 {incident_id} 的挂起图状态")

    msg = (
        "已批准合并并完成故障经验自进化归档"
        if req.decision == "approved"
        else "已驳回补丁并重新进入沙箱修复"
    )
    return ApiResponse(code=200, message=msg, data=result)
