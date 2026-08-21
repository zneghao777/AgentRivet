"""图谱服务门面（GraphRAGService）：构建 / 状态 / 实体治理 / 查询。

供 API 路由（router_knowledge_graph）与 Agent 工具复用。
所有操作校验 tenant_id + kb_id 归属，防越权。
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import settings
from ...storage.models import DocumentMeta, KnowledgeBase
from ..jobs import index_job_service, job_to_dict
from ..policy import IndexPolicyError
from ..status import refresh_kb_status
from .builder import schedule_build
from .graph_retriever import graph_retriever
from .models import GraphEvent
from .repository import repository

logger = logging.getLogger(__name__)


class GraphRAGService:
    """图谱服务。"""

    async def get_kb(self, db: AsyncSession, *, tenant_id: str, kb_id: uuid.UUID) -> KnowledgeBase | None:
        from ...knowledge.service import knowledge_service

        return await knowledge_service.get(db, tenant_id=tenant_id, kb_id=kb_id)

    async def trigger_build(
        self, db: AsyncSession, *, tenant_id: str, kb_id: uuid.UUID
    ) -> dict:
        """触发异步图谱构建。"""
        kb = await self.get_kb(db, tenant_id=tenant_id, kb_id=kb_id)
        if kb is None:
            raise KeyError("knowledge base not found")
        self._validate_build_capability(kb)
        docs = list(
            (
                await db.execute(
                    select(DocumentMeta).where(
                        DocumentMeta.kb_id == kb_id,
                        DocumentMeta.tenant_id == tenant_id,
                        DocumentMeta.vector_status == "ready",
                        DocumentMeta.graph_status.in_({"pending", "failed", "stale"}),
                    )
                )
            )
            .scalars()
            .all()
        )
        job_ids: list[str] = []
        for doc in docs:
            job, created = await index_job_service.enqueue(
                db,
                tenant_id=tenant_id,
                kb_id=kb_id,
                doc_id=doc.id,
                job_type="graph_build",
                meta={"content_hash": doc.content_hash, "graph_version": kb.graph_version},
            )
            job_ids.append(str(job.id))
            if created:
                doc.graph_status = "pending"
                schedule_build(
                    db, kb_id, tenant_id, full=False, doc_id=doc.id, job_id=job.id
                )
        await refresh_kb_status(db, tenant_id=tenant_id, kb_id=kb_id, commit=True)
        return {
            "kb_id": str(kb_id),
            "graph_status": kb.graph_status,
            "affected_documents": len(docs),
            "job_ids": job_ids,
        }

    async def trigger_document_build(
        self, db: AsyncSession, *, tenant_id: str, kb_id: uuid.UUID, doc_id: int
    ) -> dict:
        kb = await self._require_kb(db, tenant_id, kb_id)
        self._validate_build_capability(kb)
        doc = (
            await db.execute(
                select(DocumentMeta).where(
                    DocumentMeta.id == doc_id,
                    DocumentMeta.kb_id == kb_id,
                    DocumentMeta.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if doc is None:
            raise KeyError("document not found")
        if doc.vector_status != "ready":
            raise IndexPolicyError("文本索引尚未就绪")
        if doc.graph_override == "exclude":
            raise IndexPolicyError("该文档已设置为不参与图谱")
        if doc.graph_status == "ready" and doc.graph_version == kb.graph_version:
            return {
                "job_id": None,
                "kb_id": str(kb_id),
                "doc_id": doc.id,
                "job_type": "graph_build",
                "status": "succeeded",
                "stage": "already_current",
                "progress": 100,
                "attempt": 0,
                "max_attempts": 0,
                "error_code": None,
                "error_message": None,
                "meta": {
                    "content_hash": doc.content_hash,
                    "graph_version": kb.graph_version,
                },
                "started_at": None,
                "finished_at": None,
                "created_at": None,
                "updated_at": None,
            }
        job, created = await index_job_service.enqueue(
            db,
            tenant_id=tenant_id,
            kb_id=kb_id,
            doc_id=doc.id,
            job_type="graph_build",
            meta={"content_hash": doc.content_hash, "graph_version": kb.graph_version},
        )
        doc.graph_status = "pending"
        await db.commit()
        if created:
            schedule_build(
                db, kb_id, tenant_id, full=False, doc_id=doc.id, job_id=job.id
            )
        await refresh_kb_status(db, tenant_id=tenant_id, kb_id=kb_id, commit=True)
        return job_to_dict(job)

    async def trigger_rebuild(
        self, db: AsyncSession, *, tenant_id: str, kb_id: uuid.UUID
    ) -> dict:
        """全量重建图谱。"""
        kb = await self.get_kb(db, tenant_id=tenant_id, kb_id=kb_id)
        if kb is None:
            raise KeyError("knowledge base not found")
        self._validate_build_capability(kb)
        job, created = await index_job_service.enqueue(
            db,
            tenant_id=tenant_id,
            kb_id=kb_id,
            job_type="graph_build",
            meta={"full": True, "graph_version": kb.graph_version},
        )
        if created:
            schedule_build(db, kb_id, tenant_id, full=True, job_id=job.id)
        return {"kb_id": str(kb_id), "graph_status": "pending", "job_ids": [str(job.id)]}

    async def status(
        self, db: AsyncSession, *, tenant_id: str, kb_id: uuid.UUID
    ) -> dict:
        """图谱构建状态。"""
        kb = await self.get_kb(db, tenant_id=tenant_id, kb_id=kb_id)
        if kb is None:
            raise KeyError("knowledge base not found")
        return {
            "kb_id": str(kb_id),
            "graph_status": kb.graph_status,
            "graph_meta": kb.graph_meta or {},
            "platform_enabled": settings.GRAPH_ENABLED,
        }

    async def list_entities(
        self, db: AsyncSession, *, tenant_id: str, kb_id: uuid.UUID,
        entity_type: str | None = None, q: str | None = None,
        page: int = 1, page_size: int = 50,
    ) -> dict:
        await self._require_kb(db, tenant_id, kb_id)
        return await repository.list_entities(
            db, tenant_id=tenant_id, kb_id=kb_id,
            entity_type=entity_type, q=q, page=page, page_size=page_size,
        )

    async def list_relations(
        self, db: AsyncSession, *, tenant_id: str, kb_id: uuid.UUID,
        q: str | None = None, page: int = 1, page_size: int = 20,
    ) -> dict:
        await self._require_kb(db, tenant_id, kb_id)
        return await repository.list_relations(
            db, tenant_id=tenant_id, kb_id=kb_id, q=q,
            page=page, page_size=page_size,
        )

    async def list_events(
        self, db: AsyncSession, *, tenant_id: str, kb_id: uuid.UUID,
        q: str | None = None, page: int = 1, page_size: int = 20,
    ) -> dict:
        await self._require_kb(db, tenant_id, kb_id)
        return await repository.list_events(
            db, tenant_id=tenant_id, kb_id=kb_id, q=q,
            page=page, page_size=page_size,
        )

    async def subgraph(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        entity_id: uuid.UUID,
        hop: int = 1,
        limit: int = 100,
    ) -> dict:
        await self._require_kb(db, tenant_id, kb_id)
        entity = await repository.get_entity(
            db, tenant_id=tenant_id, kb_id=kb_id, entity_id=entity_id
        )
        if entity is None:
            raise KeyError("entity not found")
        neighbors = await repository.neighbors(
            db,
            tenant_id=tenant_id,
            kb_id=kb_id,
            entity_id=entity_id,
            hop=max(1, min(hop, 3)),
        )
        neighbors = neighbors[: max(1, min(limit - 1, 299))]
        ids = [entity_id, *[uuid.UUID(item.entity_id) for item in neighbors]]
        relations = await repository.relations_between(
            db, tenant_id=tenant_id, kb_id=kb_id, entity_ids=ids
        )
        return {
            "nodes": [
                {
                    "id": str(entity.id),
                    "name": entity.name,
                    "entity_type": entity.entity_type,
                    "description": entity.description,
                },
                *[
                    {
                        "id": item.entity_id,
                        "name": item.name,
                        "entity_type": item.entity_type,
                        "description": item.description,
                    }
                    for item in neighbors
                ],
            ],
            "edges": [
                {
                    "id": edge.relation_id,
                    "source": edge.source_entity_id,
                    "target": edge.target_entity_id,
                    "relation_type": edge.relation_type,
                    "source_chunk_id": edge.source_chunk_id,
                }
                for edge in relations[:600]
            ],
        }

    async def clear_graph(
        self, db: AsyncSession, *, tenant_id: str, kb_id: uuid.UUID
    ) -> dict:
        kb = await self._require_kb(db, tenant_id, kb_id)
        await repository.clear_kb(db, tenant_id=tenant_id, kb_id=kb_id)
        docs = list(
            (
                await db.execute(
                    select(DocumentMeta).where(
                        DocumentMeta.kb_id == kb_id,
                        DocumentMeta.tenant_id == tenant_id,
                        DocumentMeta.graph_status != "excluded",
                    )
                )
            )
            .scalars()
            .all()
        )
        for doc in docs:
            doc.graph_status = "pending"
            doc.graph_indexed_at = None
            doc.graph_version = None
        kb.graph_meta = {}
        await db.commit()
        await refresh_kb_status(db, tenant_id=tenant_id, kb_id=kb_id, commit=True)
        return {"kb_id": str(kb_id), "graph_status": kb.graph_status}

    async def entity_detail(
        self, db: AsyncSession, *, tenant_id: str, kb_id: uuid.UUID, entity_id: uuid.UUID
    ) -> dict | None:
        """实体详情 + 邻居 + 关系边。"""
        entity = await repository.get_entity(
            db, tenant_id=tenant_id, kb_id=kb_id, entity_id=entity_id
        )
        if entity is None:
            return None
        neighbors = await repository.neighbors(
            db, tenant_id=tenant_id, kb_id=kb_id, entity_id=entity.id, hop=1
        )
        neighbor_ids = [uuid.UUID(n.entity_id) for n in neighbors]
        edges = await repository.relations_between(
            db, tenant_id=tenant_id, kb_id=kb_id,
            entity_ids=[entity.id, *neighbor_ids],
        )
        return {
            "id": str(entity.id),
            "kb_id": str(entity.kb_id),
            "doc_id": entity.doc_id,
            "entity_type": entity.entity_type,
            "name": entity.name,
            "canonical_name": entity.canonical_name,
            "description": entity.description,
            "meta": entity.meta or {},
            "chunk_ids": [int(c) for c in (entity.chunk_ids or [])],
            "neighbors": [
                {"entity_id": n.entity_id, "name": n.name, "entity_type": n.entity_type,
                 "relation_type": n.relation_type, "hop": n.hop}
                for n in neighbors
            ],
            "relations": [
                {"source_name": e.source_name, "target_name": e.target_name,
                 "relation_type": e.relation_type, "description": e.description, "weight": e.weight}
                for e in edges
            ],
        }

    async def update_entity(
        self, db: AsyncSession, *, tenant_id: str, kb_id: uuid.UUID, entity_id: uuid.UUID,
        name: str | None = None, canonical_name: str | None = None,
        entity_type: str | None = None, description: str | None = None,
    ) -> dict | None:
        await self._require_kb(db, tenant_id, kb_id)
        entity = await repository.update_entity(
            db, tenant_id=tenant_id, kb_id=kb_id, entity_id=entity_id,
            name=name, canonical_name=canonical_name,
            entity_type=entity_type, description=description,
        )
        if entity is None:
            return None
        await db.commit()
        return {
            "id": str(entity.id), "name": entity.name,
            "canonical_name": entity.canonical_name, "entity_type": entity.entity_type,
            "description": entity.description,
        }

    async def merge_entities(
        self, db: AsyncSession, *, tenant_id: str, kb_id: uuid.UUID,
        keep_id: uuid.UUID, remove_id: uuid.UUID,
    ) -> bool:
        await self._require_kb(db, tenant_id, kb_id)
        ok = await repository.merge_entities(
            db, tenant_id=tenant_id, kb_id=kb_id, keep_id=keep_id, remove_id=remove_id
        )
        if ok:
            await db.commit()
        return ok

    async def delete_entity(
        self, db: AsyncSession, *, tenant_id: str, kb_id: uuid.UUID, entity_id: uuid.UUID
    ) -> bool:
        await self._require_kb(db, tenant_id, kb_id)
        ok = await repository.delete_entity(
            db, tenant_id=tenant_id, kb_id=kb_id, entity_id=entity_id
        )
        if ok:
            await db.commit()
        return ok

    async def search(
        self, db: AsyncSession, *, tenant_id: str, kb_id: uuid.UUID,
        query: str, strategy: str = "auto", hop: int = 2,
    ) -> dict:
        """语义图检索（GRAPH_RAG_SPEC §6.2 POST /graph/search）。"""
        await self._require_kb(db, tenant_id, kb_id)
        return await graph_retriever.hybrid_search(
            db, tenant_id=tenant_id, kb_id=kb_id,
            query=query, strategy=strategy, hop=hop, top_k=10,
        )

    async def timeline(
        self, db: AsyncSession, *, tenant_id: str, kb_id: uuid.UUID, entity_name: str
    ) -> list[GraphEvent]:
        await self._require_kb(db, tenant_id, kb_id)
        return await repository.timeline(
            db, tenant_id=tenant_id, kb_id=kb_id, entity_name=entity_name
        )

    async def _require_kb(self, db: AsyncSession, tenant_id: str, kb_id: uuid.UUID) -> KnowledgeBase:
        kb = await self.get_kb(db, tenant_id=tenant_id, kb_id=kb_id)
        if kb is None:
            raise KeyError("knowledge base not found")
        return kb

    @staticmethod
    def _validate_build_capability(kb: KnowledgeBase) -> None:
        if not settings.GRAPH_ENABLED:
            raise IndexPolicyError(
                "当前环境未启用图谱服务", code="GRAPH_PLATFORM_DISABLED"
            )
        if kb.index_mode != "graph_enhanced":
            raise IndexPolicyError("标准知识库不能构建或查询知识图谱")


graph_service = GraphRAGService()
