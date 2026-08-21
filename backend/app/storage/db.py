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
    # 导入图谱 ORM，使 create_all 一并建表（GRAPH_RAG_SPEC §2.2）
    from ..knowledge.graph import models as _graph_models  # noqa: F401
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
        # 混合检索关键词路（AGENTIC_RAG_UPGRADE_SPEC §1.1/§3.2）：pg_trgm 扩展 + GIN trgm 索引
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_chunks_trgm "
                "ON knowledge_chunks USING gin (chunk_text gin_trgm_ops)"
            )
        )
        # 长期记忆（MEMORY_SPEC §5.3）：表已由 create_all 创建，这里补 HNSW 索引
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_memory_hnsw "
                "ON memory_entries USING hnsw (embedding vector_cosine_ops)"
            )
        )
        # KBX：知识库模式、文档三阶段状态与持久化任务。
        for ddl in (
            "ALTER TABLE knowledge_bases ADD COLUMN IF NOT EXISTS graph_status VARCHAR(16) NOT NULL DEFAULT 'none'",
            "ALTER TABLE knowledge_bases ADD COLUMN IF NOT EXISTS graph_meta JSONB NOT NULL DEFAULT '{}'::jsonb",
            "ALTER TABLE knowledge_bases ADD COLUMN IF NOT EXISTS index_mode VARCHAR(24) NOT NULL DEFAULT 'standard'",
            "ALTER TABLE knowledge_bases ADD COLUMN IF NOT EXISTS graph_build_policy VARCHAR(16) NOT NULL DEFAULT 'manual'",
            "ALTER TABLE knowledge_bases ADD COLUMN IF NOT EXISTS index_status VARCHAR(16) NOT NULL DEFAULT 'empty'",
            "ALTER TABLE knowledge_bases ADD COLUMN IF NOT EXISTS graph_version INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE knowledge_bases ADD COLUMN IF NOT EXISTS settings JSONB NOT NULL DEFAULT '{}'::jsonb",
            "ALTER TABLE document_meta ADD COLUMN IF NOT EXISTS graph_override VARCHAR(16) NOT NULL DEFAULT 'inherit'",
            "ALTER TABLE document_meta ADD COLUMN IF NOT EXISTS parse_method VARCHAR(16) NOT NULL DEFAULT 'text'",
            "ALTER TABLE document_meta ADD COLUMN IF NOT EXISTS vector_status VARCHAR(16) NOT NULL DEFAULT 'pending'",
            "ALTER TABLE document_meta ADD COLUMN IF NOT EXISTS graph_status VARCHAR(16) NOT NULL DEFAULT 'excluded'",
            "ALTER TABLE document_meta ADD COLUMN IF NOT EXISTS vector_error TEXT",
            "ALTER TABLE document_meta ADD COLUMN IF NOT EXISTS graph_error TEXT",
            "ALTER TABLE document_meta ADD COLUMN IF NOT EXISTS vector_indexed_at TIMESTAMPTZ",
            "ALTER TABLE document_meta ADD COLUMN IF NOT EXISTS graph_indexed_at TIMESTAMPTZ",
            "ALTER TABLE document_meta ADD COLUMN IF NOT EXISTS graph_version INTEGER",
            "ALTER TABLE document_meta ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64)",
            "CREATE INDEX IF NOT EXISTS ix_document_meta_content_hash ON document_meta (content_hash)",
            "CREATE INDEX IF NOT EXISTS ix_kb_jobs_tenant_kb ON knowledge_index_jobs (tenant_id, kb_id, created_at)",
            "CREATE INDEX IF NOT EXISTS ix_kb_jobs_doc_type ON knowledge_index_jobs (doc_id, job_type, status)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_jobs_active ON knowledge_index_jobs "
            "(tenant_id, kb_id, COALESCE(doc_id, 0), job_type) WHERE status IN ('queued', 'running')",
        ):
            await conn.execute(text(ddl))
        # 非破坏回填：已有图数据的库保留为图谱增强，其他库按标准模式处理。
        await conn.execute(
            text(
                "UPDATE knowledge_bases kb SET index_mode = 'graph_enhanced', "
                "graph_build_policy = 'manual' "
                "WHERE kb.graph_status NOT IN ('none', 'disabled') "
                "OR EXISTS (SELECT 1 FROM graph_entities ge WHERE ge.kb_id = kb.id)"
            )
        )
        await conn.execute(
            text(
                "UPDATE knowledge_bases SET graph_status = 'disabled' "
                "WHERE index_mode = 'standard'"
            )
        )
        await conn.execute(
            text(
                "UPDATE document_meta d SET vector_status = CASE "
                "WHEN d.parse_status = 'parsed' AND EXISTS "
                "(SELECT 1 FROM knowledge_chunks c WHERE c.doc_id = d.id) THEN 'ready' "
                "WHEN d.parse_status = 'failed' THEN 'failed' ELSE 'pending' END, "
                "vector_indexed_at = CASE WHEN d.parse_status = 'parsed' THEN d.updated_at ELSE NULL END"
            )
        )
        await conn.execute(
            text(
                "UPDATE document_meta d SET graph_status = CASE "
                "WHEN kb.index_mode = 'standard' THEN 'excluded' "
                "WHEN EXISTS (SELECT 1 FROM graph_entities ge WHERE ge.kb_id = d.kb_id "
                "AND (ge.doc_id = d.id OR EXISTS (SELECT 1 FROM unnest(COALESCE(ge.chunk_ids, ARRAY[]::bigint[])) cid "
                "JOIN knowledge_chunks c ON c.id = cid WHERE c.doc_id = d.id))) THEN 'ready' "
                "ELSE 'stale' END "
                "FROM knowledge_bases kb WHERE kb.id = d.kb_id"
            )
        )
        await conn.execute(
            text(
                "UPDATE knowledge_bases kb SET index_status = CASE "
                "WHEN NOT EXISTS (SELECT 1 FROM document_meta d WHERE d.kb_id = kb.id) THEN 'empty' "
                "WHEN EXISTS (SELECT 1 FROM document_meta d WHERE d.kb_id = kb.id "
                "AND d.vector_status IN ('pending', 'building')) THEN 'indexing' "
                "WHEN NOT EXISTS (SELECT 1 FROM document_meta d WHERE d.kb_id = kb.id "
                "AND d.vector_status <> 'ready') THEN 'ready' "
                "WHEN EXISTS (SELECT 1 FROM document_meta d WHERE d.kb_id = kb.id "
                "AND d.vector_status = 'ready') AND EXISTS (SELECT 1 FROM document_meta d "
                "WHERE d.kb_id = kb.id AND d.vector_status IN ('failed', 'stale')) THEN 'partial' "
                "WHEN NOT EXISTS (SELECT 1 FROM document_meta d WHERE d.kb_id = kb.id "
                "AND d.vector_status <> 'failed') THEN 'failed' "
                "WHEN EXISTS (SELECT 1 FROM document_meta d WHERE d.kb_id = kb.id "
                "AND d.vector_status = 'stale') THEN 'stale' ELSE 'failed' END"
            )
        )
        await conn.execute(
            text(
                "UPDATE knowledge_bases kb SET graph_status = CASE "
                "WHEN kb.index_mode = 'standard' THEN 'disabled' "
                "WHEN NOT EXISTS (SELECT 1 FROM document_meta d WHERE d.kb_id = kb.id "
                "AND d.graph_status <> 'excluded') THEN 'none' "
                "WHEN EXISTS (SELECT 1 FROM document_meta d WHERE d.kb_id = kb.id "
                "AND d.graph_status = 'building') THEN 'building' "
                "WHEN EXISTS (SELECT 1 FROM document_meta d WHERE d.kb_id = kb.id "
                "AND d.graph_status = 'pending') THEN 'pending' "
                "WHEN NOT EXISTS (SELECT 1 FROM document_meta d WHERE d.kb_id = kb.id "
                "AND d.graph_status NOT IN ('ready', 'excluded')) THEN 'ready' "
                "WHEN EXISTS (SELECT 1 FROM document_meta d WHERE d.kb_id = kb.id "
                "AND d.graph_status = 'ready') AND EXISTS (SELECT 1 FROM document_meta d "
                "WHERE d.kb_id = kb.id AND d.graph_status IN ('failed', 'stale')) THEN 'partial' "
                "WHEN NOT EXISTS (SELECT 1 FROM document_meta d WHERE d.kb_id = kb.id "
                "AND d.graph_status NOT IN ('failed', 'excluded')) THEN 'failed' "
                "WHEN EXISTS (SELECT 1 FROM document_meta d WHERE d.kb_id = kb.id "
                "AND d.graph_status = 'stale') THEN 'stale' ELSE 'failed' END"
            )
        )
        # graph_meta 是派生统计；旧数据可能只留有历史计数而实际图表已为空。
        # 启动时以当前租户、当前知识库的真实表数据校准，避免 UI 展示幽灵实体。
        await conn.execute(
            text(
                "UPDATE knowledge_bases kb SET graph_meta = "
                "COALESCE(kb.graph_meta, '{}'::jsonb) || jsonb_build_object("
                "'entities', (SELECT COUNT(*) FROM graph_entities ge "
                "WHERE ge.kb_id = kb.id AND ge.tenant_id = kb.tenant_id), "
                "'relations', (SELECT COUNT(*) FROM graph_relations gr "
                "WHERE gr.kb_id = kb.id AND gr.tenant_id = kb.tenant_id), "
                "'events', (SELECT COUNT(*) FROM graph_events gev "
                "WHERE gev.kb_id = kb.id AND gev.tenant_id = kb.tenant_id), "
                "'communities', (SELECT COUNT(*) FROM graph_communities gc "
                "WHERE gc.kb_id = kb.id AND gc.tenant_id = kb.tenant_id))"
            )
        )
        # create_all 不会为旧表补 CHECK；以命名约束幂等追加。
        for name, clause, table in (
            ("ck_kb_index_mode", "index_mode IN ('standard', 'graph_enhanced')", "knowledge_bases"),
            ("ck_kb_graph_build_policy", "graph_build_policy IN ('auto', 'manual')", "knowledge_bases"),
            ("ck_kb_standard_manual_policy", "index_mode = 'graph_enhanced' OR graph_build_policy = 'manual'", "knowledge_bases"),
            ("ck_doc_graph_override", "graph_override IN ('inherit', 'include', 'exclude')", "document_meta"),
            ("ck_doc_parse_method", "parse_method IN ('text', 'ocr')", "document_meta"),
        ):
            await conn.execute(
                text(
                    f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{name}') "
                    f"THEN ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({clause}); END IF; END $$"
                )
            )
        # Graph RAG（GRAPH_RAG_SPEC §2.2/§3.2 步骤 6）：实体向量 HNSW + name BM25（pg_trgm）
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_graph_entities_vec "
                "ON graph_entities USING hnsw (embedding vector_cosine_ops)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_graph_entities_name_trgm "
                "ON graph_entities USING gin (canonical_name gin_trgm_ops)"
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
        # Trace 事件回放：消息与一次请求/Trace 直接关联；历史数据允许为空。
        for col in (
            "trace_id VARCHAR(64)",
            "span_id VARCHAR(64)",
            "request_id VARCHAR(128)",
            "turn_index INTEGER",
            "step_index INTEGER",
        ):
            await conn.execute(text(f"ALTER TABLE messages ADD COLUMN IF NOT EXISTS {col}"))
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_messages_trace ON messages (trace_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_messages_request ON messages (request_id)")
        )
        # C-12：audit_logs 关联 Trace / Request（OBSERVABILITY_SPEC §8.2）
        for col in (
            "trace_id VARCHAR(64)",
            "request_id VARCHAR(128)",
            "resource_id VARCHAR(128)",
        ):
            await conn.execute(text(f"ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS {col}"))
