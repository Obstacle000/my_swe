"""SQLAlchemy 2.0 异步数据库引擎与会话管理 (支持排障平台库与电商靶场业务库双引擎)"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from app.config import settings


class Base(DeclarativeBase):
    """排障平台 SQLAlchemy ORM 基类"""


# 兼容别名
PlatformBase = Base


class MallBase(DeclarativeBase):
    """被排查电商靶场 SQLAlchemy ORM 基类"""


# 1. 排障平台数据库异步引擎 (rca_platform.db 或 MySQL)
platform_engine = create_async_engine(
    settings.platform_db_url,
    echo=False,
    future=True,
)
engine = platform_engine

PlatformSessionLocal = async_sessionmaker(
    bind=platform_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)
AsyncSessionLocal = PlatformSessionLocal

# 2. 被排查电商靶场业务库异步引擎 (mall_target.db 或 MySQL)
mall_engine = create_async_engine(
    settings.target_mall_db_url,
    echo=False,
    future=True,
)

MallSessionLocal = async_sessionmaker(
    bind=mall_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


@asynccontextmanager
async def get_db_session() -> AsyncIterator[AsyncSession]:
    """提供异步数据库会话上下文"""
    async with PlatformSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
