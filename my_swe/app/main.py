"""FastAPI 后端主入口 (对应《5_API _DOC.md》)"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.incidents_router import router as incidents_router
from app.api.knowledge_router import router as knowledge_router
from app.api.range_router import router as range_router
from app.core.storage_backends import seed_databases_if_empty
from app.schemas.api_schemas import ApiResponse
from target_mall_app.app.api.order_router import router as target_mall_order_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动生命周期：自动初始化并预置平台库与被排查电商靶场库"""
    await seed_databases_if_empty()
    yield


app = FastAPI(
    title="基于 LangGraph 的企业级多源故障根因定位与代码自愈系统 (My-SWE)",
    description=(
        "支持 ES (BM25) + Qdrant (Vector) + RRF 倒数秩融合混合检索、Send API 并行多探针子图、"
        "valid_refs 防幻觉证据绑定校验、SHA-256 结论指纹去重、沙箱 pytest 自愈闭环与 interrupt() 人工审批。"
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册核心排障平台 API 路由
app.include_router(range_router)
app.include_router(incidents_router)
app.include_router(knowledge_router)

# 挂载内置被排查电商靶场路由（可直接访问触发真实异常）
app.include_router(target_mall_order_router, prefix="/target-mall")


@app.get("/health", response_model=ApiResponse, tags=["0. 健康检查"])
async def health_check() -> ApiResponse:
    return ApiResponse(
        code=200,
        message="ok",
        data={"service": "my-swe-rca-engine", "status": "healthy"},
    )
