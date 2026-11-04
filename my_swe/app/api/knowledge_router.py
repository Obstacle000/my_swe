"""模块四：知识库与双路 RRF 混合检索调试路由控制器 (Knowledge & RRF Debug API)
通过 Annotated 依赖注入 (KnowledgeServiceDep) 获取装配好 MySQL / ES / Qdrant 仓储的服务实例。
"""

from fastapi import APIRouter

from app.api.deps import KnowledgeServiceDep
from app.schemas.api_schemas import ApiResponse, HybridSearchDebugRequest

router = APIRouter(prefix="/api/v1/knowledge", tags=["4. 知识库与双路 RRF 混合检索 (Knowledge Base)"])


@router.post("/search-debug", response_model=ApiResponse)
async def search_debug_endpoint(
    req: HybridSearchDebugRequest,
    service: KnowledgeServiceDep,
) -> ApiResponse:
    """独立调试 ES (BM25) + Qdrant (Vector) 双路检索与 RRF (k=60) 倒数秩融合重排明细"""
    data = await service.debug_hybrid_search(req)
    return ApiResponse(code=200, message="success", data=data)


@router.get("/runbooks", response_model=ApiResponse)
async def list_runbooks_endpoint(
    service: KnowledgeServiceDep,
) -> ApiResponse:
    """查询历史故障知识库中的所有 Runbook 复盘记录（含结案后自动自进化写入的新记录）"""
    data = await service.list_runbooks()
    return ApiResponse(code=200, message="success", data=data)
