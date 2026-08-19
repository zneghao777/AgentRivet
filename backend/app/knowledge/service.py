"""KnowledgeService 门面：面向路由与工具的统一入口。

- 知识库 CRUD（租户隔离：所有查询带 tenant_id，越权返回 None 而非抛错泄露存在性）
- 文档上传/删除
- 检索（供 knowledge_search 工具与调试接口使用）
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..storage.models import KnowledgeBase, KnowledgeChunk
from .ingest import IngestError, delete_document_chunks, ingest_document
from .retriever import Hit, retrieve


def _now() -> datetime:
    return datetime.now(UTC)


class KnowledgeService:
    """知识库服务门面。"""

    async def create(
        self, db: AsyncSession, *, tenant_id: str, name: str, description: str | None = None
    ) -> KnowledgeBase | None:
        """创建知识库；同名已存在返回 None（幂等保护）。"""
        name = name.strip()
        if not name:
            return None
        existing = await self.get_by_name(db, tenant_id=tenant_id, name=name)
        if existing is not None and existing.status == "active":
            return None
        kb = KnowledgeBase(
            tenant_id=tenant_id,
            name=name,
            description=description,
            status="active",
        )
        db.add(kb)
        await db.commit()
        await db.refresh(kb)
        return kb

    async def list(
        self, db: AsyncSession, *, tenant_id: str, include_deleted: bool = False
    ) -> list[KnowledgeBase]:
        stmt = select(KnowledgeBase).where(KnowledgeBase.tenant_id == tenant_id)
        if not include_deleted:
            stmt = stmt.where(KnowledgeBase.status != "deleted")
        stmt = stmt.order_by(KnowledgeBase.created_at.desc())
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get(
        self, db: AsyncSession, *, tenant_id: str, kb_id: uuid.UUID
    ) -> KnowledgeBase | None:
        """按 id + 租户查询（越权返回 None，接口层转 404）。"""
        result = await db.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.id == kb_id,
                KnowledgeBase.tenant_id == tenant_id,
                KnowledgeBase.status != "deleted",
            )
        )
        return result.scalar_one_or_none()

    async def get_by_name(
        self, db: AsyncSession, *, tenant_id: str, name: str
    ) -> KnowledgeBase | None:
        result = await db.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.tenant_id == tenant_id,
                KnowledgeBase.name == name,
            )
        )
        return result.scalar_one_or_none()

    async def resolve_ids_by_names(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        names: list[str],
    ) -> list[uuid.UUID]:
        """将会话白名单的知识库名称解析为 id 列表（仅返回当前租户 active 的）。"""
        stmt = select(KnowledgeBase.id).where(
            KnowledgeBase.tenant_id == tenant_id,
            KnowledgeBase.status == "active",
        )
        if names:
            stmt = stmt.where(KnowledgeBase.name.in_(names))
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def set_enabled(
        self, db: AsyncSession, *, tenant_id: str, kb_id: uuid.UUID, enabled: bool
    ) -> KnowledgeBase | None:
        """Enable or disable a knowledge base without removing its documents."""
        kb = await self.get(db, tenant_id=tenant_id, kb_id=kb_id)
        if kb is None:
            return None
        kb.status = "active" if enabled else "inactive"
        kb.updated_at = _now()
        await db.commit()
        await db.refresh(kb)
        return kb

    async def delete(
        self, db: AsyncSession, *, tenant_id: str, kb_id: uuid.UUID
    ) -> bool:
        """软删除知识库（状态置 deleted）；级联物理删除 chunks。"""
        kb = await self.get(db, tenant_id=tenant_id, kb_id=kb_id)
        if kb is None:
            return False
        from sqlalchemy import delete as sa_delete

        await db.execute(
            sa_delete(KnowledgeChunk).where(
                KnowledgeChunk.kb_id == kb_id,
                KnowledgeChunk.tenant_id == tenant_id,
            )
        )
        kb.status = "deleted"
        kb.updated_at = _now()
        await db.commit()
        return True

    async def list_documents(
        self, db: AsyncSession, *, tenant_id: str, kb_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        """知识库详情 + 文档列表（按 source_file 聚合 chunk 数，doc_id 取首块 id）。"""
        kb = await self.get(db, tenant_id=tenant_id, kb_id=kb_id)
        if kb is None:
            return []
        from sqlalchemy import func

        rows = await db.execute(
            select(
                KnowledgeChunk.source_file,
                func.count(KnowledgeChunk.id),
                func.min(KnowledgeChunk.id),
                func.max(KnowledgeChunk.created_at),
            )
            .where(
                KnowledgeChunk.kb_id == kb_id,
                KnowledgeChunk.tenant_id == tenant_id,
            )
            .group_by(KnowledgeChunk.source_file)
        )
        return [
            {
                "doc_id": int(doc_id),
                "source_file": source,
                "chunk_count": count,
                "created_at": created.isoformat() if created else None,
            }
            for source, count, doc_id, created in rows.fetchall()
        ]

    async def upload_document(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        filename: str,
        content: bytes,
        replace: bool = True,
    ) -> tuple[int, int]:
        """上传并摄取文档，返回 (doc_id, chunk_count)。"""
        kb = await self.get(db, tenant_id=tenant_id, kb_id=kb_id)
        if kb is None:
            raise IngestError("知识库不存在或无权访问")
        return await ingest_document(
            db,
            kb_id=kb_id,
            tenant_id=tenant_id,
            filename=filename,
            content=content,
            replace=replace,
        )

    async def delete_document(
        self, db: AsyncSession, *, tenant_id: str, kb_id: uuid.UUID, doc_id: int
    ) -> bool:
        """按 doc_id（该文档首块 id）删除单个文档的所有 chunks。"""
        kb = await self.get(db, tenant_id=tenant_id, kb_id=kb_id)
        if kb is None:
            return False
        result = await db.execute(
            select(KnowledgeChunk.source_file).where(
                KnowledgeChunk.id == doc_id,
                KnowledgeChunk.kb_id == kb_id,
                KnowledgeChunk.tenant_id == tenant_id,
            )
        )
        source_file = result.scalar_one_or_none()
        if source_file is None:
            return False
        await delete_document_chunks(
            db, kb_id=kb_id, tenant_id=tenant_id, source_file=source_file
        )
        return True

    async def search(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        query: str,
        top_k: int = 5,
    ) -> list[Hit]:
        """调试检索：限定单个知识库内检索。"""
        kb = await self.get(db, tenant_id=tenant_id, kb_id=kb_id)
        if kb is None or kb.status != "active":
            return []
        return await retrieve(
            db,
            tenant_id=tenant_id,
            query=query,
            kb_ids=[kb_id],
            top_k=top_k,
        )

    async def retrieve(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        query: str,
        kb_ids: list[uuid.UUID] | None = None,
        kb_name: str | None = None,
        top_k: int = 5,
    ) -> list[Hit]:
        """供知识检索工具使用：kb_name 指定时必须校验其归属当前租户（防越权）。

        - kb_name 给定：仅检索该知识库；若不存在/不属于当前租户 → 返回空。
        - kb_ids 给定（会话白名单）：仅检索白名单内知识库。
        - 均未给定：检索当前租户全部知识库。
        """
        if kb_name:
            kb = await self.get_by_name(db, tenant_id=tenant_id, name=kb_name)
            if kb is None or kb.status != "active":
                return []
            kb_ids = [kb.id]
        else:
            stmt = select(KnowledgeBase.id).where(
                KnowledgeBase.tenant_id == tenant_id,
                KnowledgeBase.status == "active",
            )
            if kb_ids is not None:
                if not kb_ids:
                    return []
                stmt = stmt.where(KnowledgeBase.id.in_(kb_ids))
            result = await db.execute(stmt)
            kb_ids = list(result.scalars().all())
            if not kb_ids:
                return []
        return await retrieve(
            db,
            tenant_id=tenant_id,
            query=query,
            kb_ids=kb_ids,
            top_k=top_k,
        )


knowledge_service = KnowledgeService()
