"""全局配置与大模型 (LLM) 实例工厂模块"""

import os
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from pydantic_settings import BaseSettings

# 加载项目根目录下的 .env 文件
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def now_iso() -> str:
    """生成当前时间的 ISO-8601 字符串"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Settings(BaseSettings):
    """系统环境配置项"""

    # 1. LLM 模型配置 (兼容 DeepSeek / Qwen / OpenAI)
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
    llm_model_name: str = os.getenv("LLM_MODEL_NAME", "deepseek-chat")
    llm_temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.0"))

    # 2. Embedding 配置
    embedding_api_key: str = os.getenv("EMBEDDING_API_KEY", "")
    embedding_base_url: str = os.getenv(
        "EMBEDDING_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    embedding_model_name: str = os.getenv("EMBEDDING_MODEL_NAME", "text-embedding-v3")

    # 3. 外部存储切换开关与连接串
    use_external_stores: bool = os.getenv("USE_EXTERNAL_STORES", "false").lower() == "true"
    platform_db_url: str = os.getenv(
        "PLATFORM_DB_URL", f"sqlite+aiosqlite:///{(PROJECT_ROOT / 'rca_platform.db').as_posix()}"
    )
    target_mall_db_url: str = os.getenv(
        "TARGET_MALL_DB_URL", f"sqlite+aiosqlite:///{(PROJECT_ROOT / 'mall_target.db').as_posix()}"
    )
    es_url: str = os.getenv("ES_URL", "http://localhost:9200")
    es_runtime_log_index: str = os.getenv("ES_RUNTIME_LOG_INDEX", "mall-runtime-logs")
    es_history_bm25_index: str = os.getenv("ES_HISTORY_BM25_INDEX", "historical-incidents-bm25")
    qdrant_url: str = os.getenv("QDRANT_URL", "http://localhost:6333")
    qdrant_collection: str = os.getenv("QDRANT_COLLECTION", "historical_incidents_vec")
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # 4. 默认靶场本地源码路径
    default_target_repo_path: Path = PROJECT_ROOT / "target_mall_app"


settings = Settings()


def get_llm(temperature: float | None = None) -> ChatOpenAI:
    """创建并返回配置好的 ChatOpenAI 大模型实例"""
    return ChatOpenAI(
        api_key=settings.llm_api_key or "sk-placeholder",
        base_url=settings.llm_base_url,
        model=settings.llm_model_name,
        temperature=settings.llm_temperature if temperature is None else temperature,
    )
