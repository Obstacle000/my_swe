"""模块一：内置电商故障靶场接口 (Fault Range API)"""

from fastapi import APIRouter
from app.core.storage_backends import storage_manager
from app.schemas.api_schemas import ApiResponse, TriggerFaultRequest

router = APIRouter(prefix="/api/v1/range", tags=["1. 电商故障靶场 (Fault Range)"])


@router.post("/trigger-fault", response_model=ApiResponse)
async def trigger_fault_endpoint(req: TriggerFaultRequest) -> ApiResponse:
    """一键触发三大预置电商故障剧本之一，向运行态日志流注入真实异常堆栈与 trace_id"""
    result = storage_manager.inject_scenario_fault(req.scenario_id)
    return ApiResponse(
        code=200,
        message="故障场景已成功注入靶场",
        data=result,
    )
