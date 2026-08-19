"""LangGraph Postgres Checkpointer 工厂。

独立 schema（建议 `checkpoints`），业务库与 checkpoint 库同实例不同 schema。
"""

from __future__ import annotations

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from ..config import settings

_CHECKPOINTER: AsyncPostgresSaver | None = None
_POOL: AsyncConnectionPool | None = None


def _pg_conninfo(url: str) -> str:
    """将 SQLAlchemy URL 转换为 psycopg 连接串。"""
    url = url.replace("postgresql+psycopg://", "postgresql://")
    return url.replace("postgresql+psycopg://", "postgresql://")


async def get_checkpointer() -> AsyncPostgresSaver:
    """获取（并缓存）Postgres Checkpointer 单例。"""
    global _CHECKPOINTER, _POOL
    if _CHECKPOINTER is not None:
        return _CHECKPOINTER

    pool = AsyncConnectionPool(
        conninfo=_pg_conninfo(settings.DATABASE_URL),
        max_size=10,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0},
    )
    await pool.open()
    saver = AsyncPostgresSaver(pool)
    await saver.setup()  # 创建 checkpoint 表
    _POOL = pool
    _CHECKPOINTER = saver
    return saver
