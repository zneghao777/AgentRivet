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
    对旧表（knowledge_chunks）做幂等 ALTER 补齐新列，支持开发期平滑升级（SPEC §8）。
    """
    from .models import Base

    async with engine.begin() as conn:
        # 确保 pgvector 扩展存在
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        # 向量维度迁移：旧库 embedding 为 1536 维（OpenAI），BGE-M3 为 1024 维。
        # 维度不匹配时 drop 重加（开发底座可接受，生产用迁移脚本）。
        dim = settings.EMBEDDING_DIM
        col_type = (
            await conn.execute(
                text(
                    "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
                    "WHERE a.attrelid = 'knowledge_chunks'::regclass "
                    "AND a.attname = 'embedding'"
                )
            )
        ).scalar_one_or_none()
        if col_type and col_type != f"vector({dim})":
            await conn.execute(text("DROP INDEX IF EXISTS ix_chunks_hnsw"))
            await conn.execute(
                text("ALTER TABLE knowledge_chunks DROP COLUMN IF EXISTS embedding")
            )
            await conn.execute(
                text(f"ALTER TABLE knowledge_chunks ADD COLUMN embedding vector({dim})")
            )
        # 旧表迁移：knowledge_chunks 补齐 doc_id / enabled / updated_at
        for ddl in (
            "ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS doc_id BIGINT REFERENCES document_meta(id)",
            "ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE",
            "ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ",
            "CREATE INDEX IF NOT EXISTS ix_chunks_kb_doc ON knowledge_chunks (kb_id, doc_id)",
            "CREATE INDEX IF NOT EXISTS ix_chunks_kb_enabled ON knowledge_chunks (kb_id, enabled)",
        ):
            await conn.execute(text(ddl))
        # HNSW 余弦索引（幂等）
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_chunks_hnsw "
                "ON knowledge_chunks USING hnsw (embedding vector_cosine_ops)"
            )
        )
        # 长期记忆（MEMORY_SPEC §5.3）：表已由 create_all 创建，这里补 HNSW 索引
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_memory_hnsw "
                "ON memory_entries USING hnsw (embedding vector_cosine_ops)"
            )
        )
        # 可观测性 v2 迁移：agent_spans 新增列（OBSERVABILITY_SPEC §9.2）
        _span_cols = [
            "run_id VARCHAR(64)",
            "parent_run_id VARCHAR(64)",
            "links JSONB",
            "status_code VARCHAR(32)",
            "status_message TEXT",
            "model_provider VARCHAR(64)",
            "model_name_requested VARCHAR(128)",
            "model_name_resolved VARCHAR(128)",
            "model_parameters JSONB",
            "finish_reason VARCHAR(64)",
            "provider_request_id VARCHAR(128)",
            "prompt_name VARCHAR(128)",
            "prompt_version VARCHAR(64)",
            "prompt_hash VARCHAR(64)",
            "ttft_ms INTEGER",
            "retry_count INTEGER NOT NULL DEFAULT 0",
            "cost_input NUMERIC(18,8)",
            "cost_output NUMERIC(18,8)",
            "cost_total NUMERIC(18,8)",
            "currency VARCHAR(8)",
            "events JSONB",
            "content_capture_mode VARCHAR(32)",
            "redaction_version VARCHAR(32)",
            "request_id VARCHAR(128)",
            "thread_id VARCHAR(64)",
        ]
        for col in _span_cols:
            await conn.execute(
                text(f"ALTER TABLE agent_spans ADD COLUMN IF NOT EXISTS {col}")
            )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_spans_parent ON agent_spans (parent_span_id)")
        )
        # C-12：audit_logs 关联 Trace / Request（OBSERVABILITY_SPEC §8.2）
        for col in (
            "trace_id VARCHAR(64)",
            "request_id VARCHAR(128)",
            "resource_id VARCHAR(128)",
        ):
            await conn.execute(text(f"ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS {col}"))
