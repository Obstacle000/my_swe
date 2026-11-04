"""模块四：知识库与双路 RRF 混合检索调试接口 (Knowledge & RRF Debug API)"""

from fastapi import APIRouter
from app.core.storage_backends import storage_manager
from app.schemas.api_schemas import ApiResponse, HybridSearchDebugRequest

router = APIRouter(prefix="/api/v1/knowledge", tags=["4. 知识库与双路 RRF 混合检索 (Knowledge Base)"])


@router.post("/search-debug", response_model=ApiResponse)
async def search_debug_endpoint(req: HybridSearchDebugRequest) -> ApiResponse:
    """独立调试 ES (BM25) + Qdrant (Vector) 双路检索与 RRF (k=60) 倒数秩融合重排明细"""
    fused = storage_manager.hybrid_search_rrf(query=req.query, top_k=req.top_k, k_constant=60)
    return ApiResponse(
        code=200,
        message="success",
        data={
            "query": req.query,
            "rrf_k_constant": 60,
            "rrf_formula": "RRF_Score(d) = 1 / (60 + bm25_rank) + 1 / (60 + vector_rank)",
            "results": fused,
        },
    )


@router.get("/runbooks", response_model=ApiResponse)
async def list_runbooks_endpoint() -> ApiResponse:
    """查询历史故障知识库中的所有 Runbook 复盘记录（含结案后自动自进化写入的新记录）"""
    return ApiResponse(
        code=200,
        message="success",
        data={
            "total": len(storage_manager.runbooks),
            "items": storage_manager.runbooks,
        },
    )
