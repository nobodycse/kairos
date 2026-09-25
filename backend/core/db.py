"""异步数据库会话（Phase 1 起 backend 真连 PostgreSQL，design.md §2）。"""
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.config import settings

# pool_pre_ping：postgres 重启等场景下剔除失效连接
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：请求级 session，用完自动归还连接池。"""
    async with SessionLocal() as session:
        yield session
