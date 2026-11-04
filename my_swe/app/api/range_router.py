"""模块一：内置电商故障靶场路由控制器 (Fault Range API)
通过 Annotated 依赖注入 (RangeServiceDep) 获取装配好 RuntimeLogESRepository 的服务实例。
"""

from fastapi import APIRouter

from app.api.deps import RangeServiceDep
from app.schemas.api_schemas import ApiResponse, TriggerFaultRequest

router = APIRouter(prefix="/api/v1/range", tags=["1. 电商故障靶场 (Fault Range)"])


@router.post("/trigger-fault", response_model=ApiResponse)
async def trigger_fault_endpoint(
    req: TriggerFaultRequest,
    service: RangeServiceDep,
) -> ApiResponse:
    """一键触发三大预置电商故障剧本之一，向 ES 运行态日志索引注入真实异常堆栈与 trace_id"""
    result = await service.trigger_fault(req)
    return ApiResponse(
        code=200,
        message="故障场景已成功注入靶场",
        data=result,
    )
