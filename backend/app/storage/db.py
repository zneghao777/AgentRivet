"""数据库连接：SQLAlchemy engine / session 工厂。"""

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..config import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：提供数据库会话。"""
    async with AsyncSessionLocal() as session:
        yield session


async def create_tables() -> None:
    """建表（开发用）。生产环境建议使用迁移工具。

    建表后额外创建 pgvector HNSW 索引（加速余弦相似度检索）。
    """
    from .models import Base

    async with engine.begin() as conn:
        # 确保 pgvector 扩展存在
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        # HNSW 余弦索引（幂等）
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_chunks_hnsw "
                "ON knowledge_chunks USING hnsw (embedding vector_cosine_ops)"
            )
        )
