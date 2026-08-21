"""图谱表 CRUD + 递归 CTE 查询（GRAPH_RAG_SPEC §2.3 / §4.2）。

将"图"物化为 Postgres 关系表，多跳邻居遍历用递归 CTE 表达（性能可控，max_hop 限幅）。
所有查询强制 tenant_id + kb_id 过滤（安全红线，防越权）。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ...storage.models import KnowledgeChunk
from ..embedder import EmbeddingError, embed_texts
from .models import (
    GraphCommunity,
    GraphEntity,
    GraphEvent,
    GraphRelation,
)

logger = logging.getLogger(__name__)


class GraphRepositoryError(Exception):
    """图谱仓储异常。"""


@dataclass
class NeighborNode:
    """扩散路径上的一个实体节点（含入边证据）。"""

    entity_id: str
    name: str
    canonical_name: str
    entity_type: str
    description: str | None = None
    relation_type: str | None = None  # 从父实体到此实体的关系
    source_chunk_id: int | None = None
    hop: int = 0


@dataclass
class RelationEdge:
    """一条关系边（带实体名称便于前端渲染）。"""

    relation_id: str
    source_entity_id: str
    source_name: str
    target_entity_id: str
    target_name: str
    relation_type: str
    description: str | None = None
    weight: float = 0.5
    source_chunk_id: int | None = None


class GraphRepository:
    """图谱表数据访问层。"""

    # ---------- 实体 CRUD ----------

    async def upsert_entity(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        doc_id: int | None,
        entity_type: str,
        name: str,
        canonical_name: str,
        description: str | None,
        meta: dict | None,
        embedding: list[float] | None,
        chunk_id: int | None,
    ) -> GraphEntity:
        """按 (tenant_id, kb_id, canonical_name) 幂等 upsert 实体。

        - 已存在：合并描述、追加 chunk_ids、刷新 embedding。
        - 不存在：新建。
        返回持久化后的实体。
        """
        result = await db.execute(
            select(GraphEntity).where(
                GraphEntity.tenant_id == tenant_id,
                GraphEntity.kb_id == kb_id,
                GraphEntity.canonical_name == canonical_name,
            )
        )
        existing = result.scalar_one_or_none()
        now = _utcnow()
        if existing is not None:
            if description and not existing.description:
                existing.description = description
            existing.meta = {**(existing.meta or {}), **(meta or {})}
            if embedding:
                existing.embedding = embedding
            existing.canonical_name = canonical_name
            existing.entity_type = existing.entity_type or entity_type
            if chunk_id is not None:
                chunks = list(existing.chunk_ids or [])
                if int(chunk_id) not in chunks:
                    chunks.append(int(chunk_id))
                    existing.chunk_ids = chunks
            existing.updated_at = now
            await db.flush()
            return existing
        entity = GraphEntity(
            tenant_id=tenant_id,
            kb_id=kb_id,
            doc_id=doc_id,
            entity_type=entity_type,
            name=name,
            canonical_name=canonical_name,
            description=description,
            meta=meta or {},
            embedding=embedding,
            chunk_ids=([int(chunk_id)] if chunk_id is not None else []),
            created_at=now,
            updated_at=now,
        )
        db.add(entity)
        await db.flush()
        return entity

    async def get_entity(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        entity_id: uuid.UUID,
    ) -> GraphEntity | None:
        result = await db.execute(
            select(GraphEntity).where(
                GraphEntity.id == entity_id,
                GraphEntity.kb_id == kb_id,
                GraphEntity.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_entity_by_name(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        name: str,
    ) -> GraphEntity | None:
        """按规范名或原名精确召回实体。"""
        result = await db.execute(
            select(GraphEntity).where(
                GraphEntity.kb_id == kb_id,
                GraphEntity.tenant_id == tenant_id,
                (GraphEntity.canonical_name == name)
                | (GraphEntity.name == name),
            )
        )
        return result.scalar_one_or_none()

    async def search_entities(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        query: str,
        top_k: int = 10,
    ) -> list[GraphEntity]:
        """实体精确 + 向量双召回（GRAPH_RAG_SPEC §4.2）。

        先按 canonical_name/name 精确或子串召回；再用描述向量语义召回；合并去重。
        """
        q = f"%{query}%"
        result = await db.execute(
            select(GraphEntity)
            .where(
                GraphEntity.kb_id == kb_id,
                GraphEntity.tenant_id == tenant_id,
                (GraphEntity.canonical_name.ilike(q))
                | (GraphEntity.name.ilike(q)),
            )
            .limit(top_k)
        )
        exact = list(result.scalars().all())

        vector_hits: list[GraphEntity] = []
        try:
            qvec = await embed_texts([query])
        except EmbeddingError:
            qvec = []
        if qvec:
            qvec_literal = "[" + ",".join(str(v) for v in qvec[0]) + "]"
            from ...config import settings

            sql = """
                SELECT ge.id FROM graph_entities ge
                WHERE ge.kb_id = :kb AND ge.tenant_id = :tenant
                  AND ge.embedding IS NOT NULL
                  AND 1 - (ge.embedding <=> CAST(:qvec AS vector)) >= :min_sim
                ORDER BY ge.embedding <=> CAST(:qvec AS vector)
                LIMIT :top_k
            """
            rows = await db.execute(
                text(sql),
                {
                    "kb": str(kb_id),
                    "tenant": tenant_id,
                    "qvec": qvec_literal,
                    "min_sim": settings.GRAPH_MIN_SIMILARITY,
                    "top_k": max(1, top_k),
                },
            )
            vids = [uuid.UUID(str(r[0])) for r in rows.fetchall()]
            if vids:
                vec_result = await db.execute(
                    select(GraphEntity).where(
                        GraphEntity.kb_id == kb_id,
                        GraphEntity.tenant_id == tenant_id,
                        GraphEntity.id.in_(vids),
                    )
                )
                vector_hits = list(vec_result.scalars().all())

        seen: set[uuid.UUID] = set()
        merged: list[GraphEntity] = []
        for e in [*exact, *vector_hits]:
            if e.id not in seen:
                seen.add(e.id)
                merged.append(e)
        return merged

    async def list_entities(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        entity_type: str | None = None,
        q: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict:
        """实体列表（类型 / 名称关键词过滤 + 分页）。"""
        filters = [GraphEntity.kb_id == kb_id, GraphEntity.tenant_id == tenant_id]
        if entity_type:
            filters.append(GraphEntity.entity_type == entity_type)
        if q:
            pattern = f"%{q.strip()}%"
            filters.append(
                or_(
                    GraphEntity.canonical_name.ilike(pattern),
                    GraphEntity.name.ilike(pattern),
                )
            )

        total = (
            await db.execute(select(func.count(GraphEntity.id)).where(*filters))
        ).scalar_one()
        rows = (
            (
                await db.execute(
                    select(GraphEntity)
                    .where(*filters)
                    .order_by(GraphEntity.canonical_name)
                    .offset((max(1, page) - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        return {
            "items": [_entity_to_dict(e) for e in rows],
            "total": int(total),
            "page": page,
            "page_size": page_size,
        }

    async def update_entity(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        entity_id: uuid.UUID,
        name: str | None = None,
        canonical_name: str | None = None,
        entity_type: str | None = None,
        description: str | None = None,
    ) -> GraphEntity | None:
        """修正实体（GRAPH_RAG_SPEC §3.3 手动修正）。"""
        entity = await self.get_entity(
            db, tenant_id=tenant_id, kb_id=kb_id, entity_id=entity_id
        )
        if entity is None:
            return None
        if name is not None:
            entity.name = name
        if canonical_name is not None:
            entity.canonical_name = canonical_name
        if entity_type is not None:
            entity.entity_type = entity_type
        if description is not None:
            entity.description = description
        entity.updated_at = _utcnow()
        await db.flush()
        return entity

    async def merge_entities(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        keep_id: uuid.UUID,
        remove_id: uuid.UUID,
    ) -> bool:
        """合并两个实体：remove 的关系与事件全部重指向 keep，随后删除 remove（GRAPH_RAG_SPEC §3.3）。"""
        keep = await self.get_entity(
            db, tenant_id=tenant_id, kb_id=kb_id, entity_id=keep_id
        )
        remove = await self.get_entity(
            db, tenant_id=tenant_id, kb_id=kb_id, entity_id=remove_id
        )
        if keep is None or remove is None:
            return False
        # 关系重指向
        await db.execute(
            update(GraphRelation)
            .where(GraphRelation.source_entity_id == remove_id)
            .values(source_entity_id=keep_id)
        )
        await db.execute(
            update(GraphRelation)
            .where(GraphRelation.target_entity_id == remove_id)
            .values(target_entity_id=keep_id)
        )
        # 事件重指向
        await db.execute(
            update(GraphEvent)
            .where(GraphEvent.subject_entity_id == remove_id)
            .values(subject_entity_id=keep_id)
        )
        # 合并 chunk 锚点与描述
        keep.chunk_ids = list({*(keep.chunk_ids or []), *(remove.chunk_ids or [])})
        if remove.description and not keep.description:
            keep.description = remove.description
        keep.updated_at = _utcnow()
        await db.delete(remove)
        await db.flush()
        return True

    async def delete_entity(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        entity_id: uuid.UUID,
    ) -> bool:
        """删除实体（级联删除其关系与事件）。"""
        entity = await self.get_entity(
            db, tenant_id=tenant_id, kb_id=kb_id, entity_id=entity_id
        )
        if entity is None:
            return False
        await db.execute(
            delete(GraphRelation).where(
                (GraphRelation.source_entity_id == entity_id)
                | (GraphRelation.target_entity_id == entity_id)
            )
        )
        await db.execute(
            delete(GraphEvent).where(GraphEvent.subject_entity_id == entity_id)
        )
        await db.delete(entity)
        await db.flush()
        return True

    async def delete_by_doc(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        doc_id: int,
    ) -> None:
        """按证据移除文档图数据，保留仍有其他文档证据的共享实体。"""
        chunk_ids = list(
            (
                await db.execute(
                    select(KnowledgeChunk.id).where(
                        KnowledgeChunk.kb_id == kb_id,
                        KnowledgeChunk.tenant_id == tenant_id,
                        KnowledgeChunk.doc_id == doc_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        relation_filters = [
            GraphRelation.kb_id == kb_id,
            GraphRelation.tenant_id == tenant_id,
        ]
        if chunk_ids:
            relation_filters.append(GraphRelation.source_chunk_id.in_(chunk_ids))
            await db.execute(delete(GraphRelation).where(*relation_filters))
        event_filter = (
            (GraphEvent.doc_id == doc_id)
            | (GraphEvent.source_chunk_id.in_(chunk_ids) if chunk_ids else False)
        )
        await db.execute(
            delete(GraphEvent).where(
                GraphEvent.kb_id == kb_id,
                GraphEvent.tenant_id == tenant_id,
                event_filter,
            )
        )
        entities = list(
            (
                await db.execute(
                    select(GraphEntity).where(
                        GraphEntity.kb_id == kb_id,
                        GraphEntity.tenant_id == tenant_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        removed = set(int(value) for value in chunk_ids)
        for entity in entities:
            old_chunks = [int(value) for value in (entity.chunk_ids or [])]
            remaining = [value for value in old_chunks if value not in removed]
            if remaining == old_chunks and entity.doc_id != doc_id:
                continue
            if remaining:
                entity.chunk_ids = remaining
                if entity.doc_id == doc_id:
                    next_doc_id = (
                        await db.execute(
                            select(KnowledgeChunk.doc_id)
                            .where(KnowledgeChunk.id.in_(remaining))
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    entity.doc_id = next_doc_id
                continue
            await db.execute(
                delete(GraphRelation).where(
                    GraphRelation.kb_id == kb_id,
                    GraphRelation.tenant_id == tenant_id,
                    (GraphRelation.source_entity_id == entity.id)
                    | (GraphRelation.target_entity_id == entity.id),
                )
            )
            await db.execute(
                delete(GraphEvent).where(
                    GraphEvent.kb_id == kb_id,
                    GraphEvent.tenant_id == tenant_id,
                    GraphEvent.subject_entity_id == entity.id,
                )
            )
            await db.delete(entity)
        # 社区成员发生变化后旧摘要不再可信；后续图构建会重新生成。
        await db.execute(
            delete(GraphCommunity).where(
                GraphCommunity.kb_id == kb_id,
                GraphCommunity.tenant_id == tenant_id,
            )
        )
        await db.flush()

    async def clear_kb(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
    ) -> None:
        """清空知识库全部图谱数据（rebuild 用）。"""
        await db.execute(
            delete(GraphRelation).where(
                GraphRelation.kb_id == kb_id,
                GraphRelation.tenant_id == tenant_id,
            )
        )
        await db.execute(
            delete(GraphEvent).where(
                GraphEvent.kb_id == kb_id,
                GraphEvent.tenant_id == tenant_id,
            )
        )
        await db.execute(
            delete(GraphCommunity).where(
                GraphCommunity.kb_id == kb_id,
                GraphCommunity.tenant_id == tenant_id,
            )
        )
        await db.execute(
            delete(GraphEntity).where(
                GraphEntity.kb_id == kb_id,
                GraphEntity.tenant_id == tenant_id,
            )
        )
        await db.flush()

    # ---------- 关系 CRUD ----------

    async def upsert_relation(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        source_entity_id: uuid.UUID,
        target_entity_id: uuid.UUID,
        relation_type: str,
        description: str | None,
        weight: float,
        source_chunk_id: int | None,
    ) -> GraphRelation:
        """幂等 upsert 关系；重复出现 weight 累加（GRAPH_RAG_SPEC §3.2 步骤 2）。"""
        result = await db.execute(
            select(GraphRelation).where(
                GraphRelation.tenant_id == tenant_id,
                GraphRelation.kb_id == kb_id,
                GraphRelation.source_entity_id == source_entity_id,
                GraphRelation.target_entity_id == target_entity_id,
                GraphRelation.relation_type == relation_type,
            )
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            existing.weight = min(1.0, (existing.weight or 0.5) + weight)
            existing.description = existing.description or description
            existing.source_chunk_id = existing.source_chunk_id or source_chunk_id
            await db.flush()
            return existing
        rel = GraphRelation(
            tenant_id=tenant_id,
            kb_id=kb_id,
            source_entity_id=source_entity_id,
            target_entity_id=target_entity_id,
            relation_type=relation_type,
            description=description,
            weight=min(1.0, weight),
            source_chunk_id=source_chunk_id,
        )
        db.add(rel)
        await db.flush()
        return rel

    async def neighbors(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        entity_id: uuid.UUID,
        hop: int = 1,
        relation_type: str | None = None,
    ) -> list[NeighborNode]:
        """沿关系边做 1~N 跳邻居遍历（递归 CTE），收集证据（GRAPH_RAG_SPEC §4.2 expand）。

        限制 hop 最大 GRAPH_MAX_HOP，避免递归过深。
        """
        from ...config import settings

        hop = max(1, min(hop, settings.GRAPH_MAX_HOP))
        where_extra = "AND r.relation_type = :rtype" if relation_type else ""
        sql = f"""
            WITH RECURSIVE traverse AS (
                SELECT r.source_entity_id, r.target_entity_id, r.relation_type,
                       r.source_chunk_id, 1 AS hop
                FROM graph_relations r
                WHERE r.tenant_id = :tenant AND r.kb_id = :kb
                  AND (r.source_entity_id = :seed OR r.target_entity_id = :seed)
                {where_extra}
                UNION ALL
                SELECT r.source_entity_id, r.target_entity_id, r.relation_type,
                       r.source_chunk_id, t.hop + 1
                FROM graph_relations r
                JOIN traverse t ON (r.source_entity_id = t.target_entity_id)
                WHERE r.tenant_id = :tenant AND r.kb_id = :kb
                  AND t.hop < :max_hop
                {where_extra}
            )
            SELECT DISTINCT ge.id, ge.name, ge.canonical_name, ge.entity_type,
                            ge.description, t.relation_type, t.source_chunk_id, t.hop
            FROM traverse t
            JOIN graph_entities ge
                 ON ge.id = t.target_entity_id
                 AND ge.tenant_id = :tenant AND ge.kb_id = :kb
            WHERE ge.id != :seed
            ORDER BY t.hop
            LIMIT :limit
        """
        params: dict = {
            "tenant": tenant_id,
            "kb": str(kb_id),
            "seed": str(entity_id),
            "max_hop": hop,
            "limit": 500,
        }
        if relation_type:
            params["rtype"] = relation_type
        rows = await db.execute(text(sql), params)
        nodes: list[NeighborNode] = []
        for row in rows.fetchall():
            nodes.append(
                NeighborNode(
                    entity_id=str(row[0]),
                    name=row[1],
                    canonical_name=row[2],
                    entity_type=row[3],
                    description=row[4],
                    relation_type=row[5],
                    source_chunk_id=int(row[6]) if row[6] is not None else None,
                    hop=int(row[7]),
                )
            )
        return nodes

    async def relations_between(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        entity_ids: list[uuid.UUID],
    ) -> list[RelationEdge]:
        """返回实体集合之间的全部关系边（用于实体详情渲染）。"""
        if not entity_ids:
            return []
        ids = [str(e) for e in entity_ids]
        sql = """
            SELECT r.id, r.source_entity_id, se.name AS src_name,
                   r.target_entity_id, te.name AS tgt_name,
                   r.relation_type, r.description, r.weight, r.source_chunk_id
            FROM graph_relations r
            JOIN graph_entities se ON se.id = r.source_entity_id
            JOIN graph_entities te ON te.id = r.target_entity_id
            WHERE r.tenant_id = :tenant AND r.kb_id = :kb
              AND (r.source_entity_id = ANY(:ids) OR r.target_entity_id = ANY(:ids))
            ORDER BY r.weight DESC
        """
        rows = await db.execute(
            text(sql), {"tenant": tenant_id, "kb": str(kb_id), "ids": ids}
        )
        return [
            RelationEdge(
                relation_id=str(r[0]),
                source_entity_id=str(r[1]),
                source_name=r[2],
                target_entity_id=str(r[3]),
                target_name=r[4],
                relation_type=r[5],
                description=r[6],
                weight=float(r[7]),
                source_chunk_id=int(r[8]) if r[8] is not None else None,
            )
            for r in rows.fetchall()
        ]

    async def list_relations(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        q: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        """分页返回知识库关系，并附带起止实体名称。"""
        page = max(1, page)
        page_size = max(1, min(page_size, 200))
        filters = [
            GraphRelation.kb_id == kb_id,
            GraphRelation.tenant_id == tenant_id,
        ]
        source = aliased(GraphEntity)
        target = aliased(GraphEntity)
        if q:
            pattern = f"%{q.strip()}%"
            filters.append(
                or_(
                    source.name.ilike(pattern),
                    source.canonical_name.ilike(pattern),
                    target.name.ilike(pattern),
                    target.canonical_name.ilike(pattern),
                    GraphRelation.relation_type.ilike(pattern),
                    GraphRelation.description.ilike(pattern),
                )
            )
        total = int(
            (
                await db.execute(
                    select(func.count(GraphRelation.id))
                    .join(source, source.id == GraphRelation.source_entity_id)
                    .join(target, target.id == GraphRelation.target_entity_id)
                    .where(*filters)
                )
            ).scalar_one()
        )
        rows = (
            await db.execute(
                select(GraphRelation, source.name, target.name)
                .join(source, source.id == GraphRelation.source_entity_id)
                .join(target, target.id == GraphRelation.target_entity_id)
                .where(*filters)
                .order_by(GraphRelation.created_at.desc(), GraphRelation.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        return {
            "items": [
                {
                    "relation_id": str(relation.id),
                    "source_entity_id": str(relation.source_entity_id),
                    "source_name": source_name,
                    "target_entity_id": str(relation.target_entity_id),
                    "target_name": target_name,
                    "relation_type": relation.relation_type,
                    "description": relation.description,
                    "weight": float(relation.weight),
                    "source_chunk_id": relation.source_chunk_id,
                    "created_at": relation.created_at.isoformat() if relation.created_at else None,
                }
                for relation, source_name, target_name in rows
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    # ---------- 事件 ----------

    async def add_event(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        doc_id: int | None,
        event_type: str | None,
        subject_entity_id: uuid.UUID,
        predicate: str | None,
        object: str | None,
        order_no: int | None,
        source_chunk_id: int | None,
        summary: str | None,
    ) -> GraphEvent:
        event = GraphEvent(
            tenant_id=tenant_id,
            kb_id=kb_id,
            doc_id=doc_id,
            event_type=event_type,
            subject_entity_id=subject_entity_id,
            predicate=predicate,
            object=object,
            order_no=order_no,
            source_chunk_id=source_chunk_id,
            summary=summary,
        )
        db.add(event)
        await db.flush()
        return event

    async def timeline(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        entity_name: str,
    ) -> list[GraphEvent]:
        """按 order_no 返回实体的事件时间线（GRAPH_RAG_SPEC §4.2 timeline）。"""
        entity = await self.get_entity_by_name(
            db, tenant_id=tenant_id, kb_id=kb_id, name=entity_name
        )
        if entity is None:
            return []
        result = await db.execute(
            select(GraphEvent)
            .where(
                GraphEvent.subject_entity_id == entity.id,
                GraphEvent.tenant_id == tenant_id,
                GraphEvent.kb_id == kb_id,
            )
            .order_by(GraphEvent.order_no.asc().nulls_last())
        )
        return list(result.scalars().all())

    async def list_events(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        q: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        """分页返回知识库事件，并附带主体实体名称。"""
        page = max(1, page)
        page_size = max(1, min(page_size, 200))
        filters = [
            GraphEvent.kb_id == kb_id,
            GraphEvent.tenant_id == tenant_id,
        ]
        if q:
            pattern = f"%{q.strip()}%"
            filters.append(
                or_(
                    GraphEntity.name.ilike(pattern),
                    GraphEntity.canonical_name.ilike(pattern),
                    GraphEvent.event_type.ilike(pattern),
                    GraphEvent.predicate.ilike(pattern),
                    GraphEvent.object.ilike(pattern),
                    GraphEvent.summary.ilike(pattern),
                )
            )
        total = int(
            (
                await db.execute(
                    select(func.count(GraphEvent.id))
                    .join(GraphEntity, GraphEntity.id == GraphEvent.subject_entity_id)
                    .where(*filters)
                )
            ).scalar_one()
        )
        rows = (
            await db.execute(
                select(GraphEvent, GraphEntity.name)
                .join(GraphEntity, GraphEntity.id == GraphEvent.subject_entity_id)
                .where(*filters)
                .order_by(
                    GraphEvent.order_no.asc().nulls_last(),
                    GraphEvent.created_at.desc(),
                    GraphEvent.id,
                )
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        return {
            "items": [
                {
                    "event_id": str(event.id),
                    "subject_entity_id": str(event.subject_entity_id),
                    "subject_name": subject_name,
                    "order_no": event.order_no,
                    "predicate": event.predicate,
                    "object": event.object,
                    "summary": event.summary,
                    "event_type": event.event_type,
                    "source_chunk_id": event.source_chunk_id,
                    "created_at": event.created_at.isoformat() if event.created_at else None,
                }
                for event, subject_name in rows
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    # ---------- 社区 ----------

    async def upsert_community(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        level: int,
        summary: str | None,
        keywords: list[str],
        member_entity_ids: list[uuid.UUID],
    ) -> GraphCommunity:
        community = GraphCommunity(
            tenant_id=tenant_id,
            kb_id=kb_id,
            level=level,
            summary=summary,
            keywords=keywords,
            member_entity_ids=member_entity_ids,
        )
        db.add(community)
        await db.flush()
        return community

    async def communities_by_level(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        level: int = 0,
    ) -> list[GraphCommunity]:
        result = await db.execute(
            select(GraphCommunity)
            .where(
                GraphCommunity.kb_id == kb_id,
                GraphCommunity.tenant_id == tenant_id,
                GraphCommunity.level == level,
            )
            .order_by(GraphCommunity.keywords)
        )
        return list(result.scalars().all())

    async def search_communities(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        query: str,
        top_k: int = 5,
    ) -> list[GraphCommunity]:
        """按关键词/摘要召回社区（全局性问题，GRAPH_RAG_SPEC §4.2 community_summary）。"""
        q = f"%{query}%"
        result = await db.execute(
            select(GraphCommunity)
            .where(
                GraphCommunity.kb_id == kb_id,
                GraphCommunity.tenant_id == tenant_id,
                GraphCommunity.summary.ilike(q)
                | GraphCommunity.keywords.any(query),
            )
            .order_by(GraphCommunity.level)
            .limit(max(1, top_k))
        )
        return list(result.scalars().all())


# ---------- 辅助工具 ----------


def _utcnow():
    from datetime import UTC, datetime

    return datetime.now(UTC)


def _entity_to_dict(e: GraphEntity) -> dict:
    return {
        "id": str(e.id),
        "kb_id": str(e.kb_id),
        "doc_id": e.doc_id,
        "entity_type": e.entity_type,
        "name": e.name,
        "canonical_name": e.canonical_name,
        "description": e.description,
        "meta": e.meta or {},
        "chunk_ids": [str(c) for c in (e.chunk_ids or [])],
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "updated_at": e.updated_at.isoformat() if e.updated_at else None,
    }


repository = GraphRepository()
