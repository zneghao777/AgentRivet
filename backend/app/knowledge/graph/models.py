"""图谱 ORM 模型：graph_entities / graph_relations / graph_communities / graph_events。

GRAPH_RAG_SPEC §2.2 物理模型（三张核心表 + 事件辅助表）。
全部挂在 KnowledgeBase 之下，全部带 tenant_id 隔离。
与现有 storage.models.Base 共用元数据，由 create_tables 建表。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...config import settings
from ...storage.models import Base

# 支持扩展；此处仅作类型白名单注释，DB 层不强约束（VARCHAR 已足够宽）
ENTITY_TYPES = (
    "person",
    "place",
    "organization",
    "item",
    "cultivation",
    "event",
    "concept",
)


def _now() -> datetime:
    return datetime.now(UTC)


class GraphEntity(Base):
    """图实体（GRAPH_RAG_SPEC §2.2 表 1）。"""

    __tablename__ = "graph_entities"
    __table_args__ = (
        Index("idx_graph_entities_kb", "kb_id", "tenant_id"),
        Index("idx_graph_entities_name", "canonical_name"),
        Index("idx_graph_entities_type", "entity_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kb_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("knowledge_bases.id"), nullable=False
    )
    doc_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("document_meta.id"), nullable=True
    )
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict | None] = mapped_column(JSON, default=dict)
    embedding: Mapped[list | None] = mapped_column(Vector(settings.EMBEDDING_DIM))
    # 关联的原文 KnowledgeChunk.id（BigInteger）倒排锚点（GRAPH_RAG_SPEC §2.3）
    chunk_ids: Mapped[list | None] = mapped_column(ARRAY(BigInteger), default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class GraphRelation(Base):
    """图关系（GRAPH_RAG_SPEC §2.2 表 2）。"""

    __tablename__ = "graph_relations"
    __table_args__ = (
        Index("idx_graph_rel_src", "source_entity_id", "tenant_id"),
        Index("idx_graph_rel_tgt", "target_entity_id", "tenant_id"),
        Index("idx_graph_rel_type", "kb_id", "relation_type"),
        Index("idx_graph_rel_src_tgt", "source_entity_id", "target_entity_id", "relation_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kb_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("knowledge_bases.id"), nullable=False
    )
    source_entity_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("graph_entities.id"), nullable=False
    )
    target_entity_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("graph_entities.id"), nullable=False
    )
    relation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    weight: Mapped[float] = mapped_column(Float, default=0.5)
    source_chunk_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now
    )


class GraphCommunity(Base):
    """图社区（摘要单元）（GRAPH_RAG_SPEC §2.2 表 3）。"""

    __tablename__ = "graph_communities"
    __table_args__ = (
        Index("idx_graph_comm_kb", "kb_id", "tenant_id"),
        Index("idx_graph_comm_level", "kb_id", "level"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kb_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("knowledge_bases.id"), nullable=False
    )
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary: Mapped[str | None] = mapped_column(Text)
    keywords: Mapped[list | None] = mapped_column(ARRAY(Text), default=list)
    member_entity_ids: Mapped[list | None] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), default=list
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now
    )


class GraphEvent(Base):
    """事件时间线（GRAPH_RAG_SPEC §2.2 表 4 辅助）。"""

    __tablename__ = "graph_events"
    __table_args__ = (
        Index("idx_graph_events_subj", "subject_entity_id", "tenant_id"),
        Index("idx_graph_events_kb", "kb_id", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kb_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("knowledge_bases.id"), nullable=False
    )
    doc_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("document_meta.id"), nullable=True
    )
    event_type: Mapped[str | None] = mapped_column(String(64))
    subject_entity_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("graph_entities.id"), nullable=False
    )
    predicate: Mapped[str | None] = mapped_column(String(64))
    object: Mapped[str | None] = mapped_column(Text)
    order_no: Mapped[int | None] = mapped_column(Integer)
    source_chunk_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now
    )
