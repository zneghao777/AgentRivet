"""KnowledgeService 门面：文档任务 / 分块治理 / 测试检索（KB_MANAGEMENT_SPEC §3）。

- 知识库 CRUD（租户隔离：所有查询带 tenant_id，越权返回 None 而非抛错泄露存在性）
- 文档上传（落盘 + DocumentMeta + 异步解析任务调度）与删除
- 分块治理：列表 / 预览 / 编辑 / 启停 / 重新向量化
- 检索（供 knowledge_search 工具与 test-search 调试接口使用）
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..storage.models import DocumentMeta, KnowledgeBase, KnowledgeChunk
from .chunker import preview_chunks
from .embedder import EmbeddingError, embed_texts
from .files import remove_upload, save_upload
from .ingest import (
    IngestError,
    _refresh_stats,
    parse_and_ingest,
    validate_file,
)
from .retriever import Hit, retrieve

logger = logging.getLogger(__name__)

# 后台解析并发上限（SPEC §8：有界 Semaphore，如 3）
PARSE_CONCURRENCY = 3
_parse_semaphore = asyncio.Semaphore(PARSE_CONCURRENCY)
_parse_tasks: set[asyncio.Task] = set()


def _now() -> datetime:
    return datetime.now(UTC)


def _schedule_parse(doc_id: int) -> None:
    """调度后台解析任务（fire-and-forget，独立 session 执行）。"""
    task = asyncio.create_task(_run_parse(doc_id))
    _parse_tasks.add(task)
    task.add_done_callback(_parse_tasks.discard)


async def _run_parse(doc_id: int) -> None:
    """后台解析任务：限并发 + 异常兜底（不向上抛）。"""
    from ..storage.db import AsyncSessionLocal

    async with _parse_semaphore:
        try:
            async with AsyncSessionLocal() as db:
                doc = await db.get(DocumentMeta, doc_id)
                if doc is None:
                    logger.warning("后台解析：文档不存在 doc_id=%s", doc_id)
                    return
                await parse_and_ingest(db, doc)
        except Exception as exc:  # noqa: BLE001
            logger.error("后台解析任务失败 doc_id=%s: %s", doc_id, exc)


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
        """软删除知识库（状态置 deleted）；级联物理删除 chunks 与文档元数据/文件。"""
        kb = await self.get(db, tenant_id=tenant_id, kb_id=kb_id)
        if kb is None:
            return False
        docs = (
            (
                await db.execute(
                    select(DocumentMeta).where(
                        DocumentMeta.kb_id == kb_id,
                        DocumentMeta.tenant_id == tenant_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for doc in docs:
            await self._delete_doc_record(db, doc, commit=False)
        await db.execute(
            delete(KnowledgeChunk).where(
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
        """文档列表（含 parse_status / chunk_count / 错误信息）。"""
        kb = await self.get(db, tenant_id=tenant_id, kb_id=kb_id)
        if kb is None:
            return []
        rows = (
            (
                await db.execute(
                    select(DocumentMeta, func.count(KnowledgeChunk.id))
                    .outerjoin(
                        KnowledgeChunk, KnowledgeChunk.doc_id == DocumentMeta.id
                    )
                    .where(
                        DocumentMeta.kb_id == kb_id,
                        DocumentMeta.tenant_id == tenant_id,
                    )
                    .group_by(DocumentMeta.id)
                    .order_by(DocumentMeta.created_at.desc())
                )
            )
            .fetchall()
        )
        return [
            {
                "doc_id": doc.id,
                "kb_id": str(doc.kb_id),
                "source_file": doc.source_file,
                "file_size": doc.file_size,
                "mime_type": doc.mime_type,
                "parse_status": doc.parse_status,
                "parse_source": doc.parse_source,
                "chunk_count": count,
                "error": doc.error,
                "created_at": doc.created_at.isoformat() if doc.created_at else None,
                "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
            }
            for doc, count in rows
        ]

    async def get_document(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        doc_id: int,
    ) -> DocumentMeta | None:
        """按 doc_id + kb + 租户查询文档（越权返回 None）。"""
        result = await db.execute(
            select(DocumentMeta).where(
                DocumentMeta.id == doc_id,
                DocumentMeta.kb_id == kb_id,
                DocumentMeta.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    async def upload_document(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        filename: str,
        content: bytes,
        replace: bool = True,
        mime_type: str | None = None,
    ) -> DocumentMeta:
        """上传文档：校验 → 落盘 → 建 DocumentMeta(pending) → 调度异步解析。

        replace=True：同名文件先删除旧文档记录（含文件/chunks）再重建（幂等）。
        返回 DocumentMeta（doc_id / source_file / parse_status）。
        """
        kb = await self.get(db, tenant_id=tenant_id, kb_id=kb_id)
        if kb is None:
            raise IngestError("知识库不存在或无权访问")
        ext = validate_file(filename, content)
        rel_path = save_upload(tenant_id, kb_id, ext, content)
        if replace:
            await self._delete_docs_by_source(
                db, tenant_id=tenant_id, kb_id=kb_id, source_file=filename
            )
        doc = DocumentMeta(
            kb_id=kb_id,
            tenant_id=tenant_id,
            source_file=filename,
            file_path=rel_path,
            file_size=len(content),
            mime_type=mime_type or "application/octet-stream",
            parse_status="pending",
        )
        db.add(doc)
        await db.flush()
        await db.commit()
        await db.refresh(doc)
        # 调度后台解析（pending → parsing → parsed/failed）
        _schedule_parse(doc.id)
        logger.info("文档已上传并调度解析 doc=%s file=%s", doc.id, filename)
        return doc

    async def reparse_document(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        doc_id: int,
    ) -> DocumentMeta | None:
        """重新解析：清旧 chunks → 状态 reparsing → 调度后台解析。"""
        doc = await self.get_document(
            db, tenant_id=tenant_id, kb_id=kb_id, doc_id=doc_id
        )
        if doc is None:
            return None
        await db.execute(
            delete(KnowledgeChunk).where(
                KnowledgeChunk.doc_id == doc.id,
                KnowledgeChunk.kb_id == kb_id,
                KnowledgeChunk.tenant_id == tenant_id,
            )
        )
        doc.parse_status = "reparsing"
        doc.error = None
        await db.commit()
        await db.refresh(doc)
        _schedule_parse(doc.id)
        return doc

    async def delete_document(
        self, db: AsyncSession, *, tenant_id: str, kb_id: uuid.UUID, doc_id: int
    ) -> bool:
        """删除文档：文件 + 分块 + DocumentMeta 记录，并刷新统计。"""
        kb = await self.get(db, tenant_id=tenant_id, kb_id=kb_id)
        if kb is None:
            return False
        doc = await self.get_document(
            db, tenant_id=tenant_id, kb_id=kb_id, doc_id=doc_id
        )
        if doc is None:
            return False
        await self._delete_doc_record(db, doc, commit=False)
        await _refresh_stats(db, kb_id, tenant_id)
        await db.commit()
        return True

    async def _delete_doc_record(
        self, db: AsyncSession, doc: DocumentMeta, *, commit: bool = True
    ) -> None:
        """删除单个文档记录：文件落盘 + chunks + DocumentMeta。"""
        if doc.file_path:
            await asyncio.to_thread(remove_upload, doc.file_path)
        await db.execute(
            delete(KnowledgeChunk).where(
                KnowledgeChunk.doc_id == doc.id,
                KnowledgeChunk.kb_id == doc.kb_id,
                KnowledgeChunk.tenant_id == doc.tenant_id,
            )
        )
        await db.execute(delete(DocumentMeta).where(DocumentMeta.id == doc.id))
        if commit:
            await db.commit()

    async def _delete_docs_by_source(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        source_file: str,
    ) -> None:
        """同名文件替换语义：删除旧 DocumentMeta 及其 chunks / 文件。"""
        docs = (
            (
                await db.execute(
                    select(DocumentMeta).where(
                        DocumentMeta.kb_id == kb_id,
                        DocumentMeta.tenant_id == tenant_id,
                        DocumentMeta.source_file == source_file,
                    )
                )
            )
            .scalars()
            .all()
        )
        for doc in docs:
            await self._delete_doc_record(db, doc, commit=False)

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

    # ---------- 分块治理（KB_MANAGEMENT_SPEC §3.5） ----------

    async def _get_chunk(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        chunk_id: int,
    ) -> KnowledgeChunk | None:
        """按 chunk_id + kb + 租户查询分块（越权返回 None）。"""
        result = await db.execute(
            select(KnowledgeChunk).where(
                KnowledgeChunk.id == chunk_id,
                KnowledgeChunk.kb_id == kb_id,
                KnowledgeChunk.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _chunk_to_dict(chunk: KnowledgeChunk) -> dict[str, Any]:
        heading = (chunk.meta or {}).get("heading") if chunk.meta else None
        return {
            "chunk_id": int(chunk.id),
            "kb_id": str(chunk.kb_id),
            "doc_id": int(chunk.doc_id) if chunk.doc_id else None,
            "source_file": chunk.source_file,
            "heading": heading,
            "text": chunk.chunk_text,
            "char_count": len(chunk.chunk_text),
            "enabled": bool(chunk.enabled),
            "chunk_index": chunk.chunk_index,
            "updated_at": chunk.updated_at.isoformat() if chunk.updated_at else None,
        }

    async def list_chunks(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        doc_id: int | None = None,
        q: str | None = None,
        enabled: bool | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """分块列表：文档过滤 / 关键词过滤 / 启停过滤 + 分页。"""
        kb = await self.get(db, tenant_id=tenant_id, kb_id=kb_id)
        if kb is None:
            return {"items": [], "total": 0, "page": page, "page_size": page_size}

        filters = [
            KnowledgeChunk.kb_id == kb_id,
            KnowledgeChunk.tenant_id == tenant_id,
        ]
        if doc_id is not None:
            filters.append(KnowledgeChunk.doc_id == doc_id)
        if q:
            filters.append(KnowledgeChunk.chunk_text.ilike(f"%{q}%"))
        if enabled is not None:
            filters.append(KnowledgeChunk.enabled == enabled)

        total = (
            await db.execute(select(func.count(KnowledgeChunk.id)).where(*filters))
        ).scalar_one()
        page = max(1, page)
        page_size = max(1, min(page_size, 200))
        rows = (
            (
                await db.execute(
                    select(KnowledgeChunk)
                    .where(*filters)
                    .order_by(KnowledgeChunk.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        return {
            "items": [self._chunk_to_dict(c) for c in rows],
            "total": int(total),
            "page": page,
            "page_size": page_size,
        }

    def preview_document(
        self, *, text: str, chunk_size: int | None = None
    ) -> list[dict[str, Any]]:
        """分块预览（纯函数，不落库）。"""
        return preview_chunks(text, chunk_size)

    async def update_chunk(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        chunk_id: int,
        text: str | None = None,
        enabled: bool | None = None,
    ) -> KnowledgeChunk | None:
        """编辑分块：text 变更必须重新向量化（防向量漂移）；enabled 即时更新。"""
        chunk = await self._get_chunk(
            db, tenant_id=tenant_id, kb_id=kb_id, chunk_id=chunk_id
        )
        if chunk is None:
            return None
        if text is not None and text.strip() != chunk.chunk_text:
            try:
                vectors = await embed_texts([text])
            except EmbeddingError as exc:
                raise IngestError(str(exc)) from exc
            chunk.chunk_text = text
            chunk.embedding = vectors[0]
        if enabled is not None:
            chunk.enabled = enabled
        chunk.updated_at = _now()
        await db.commit()
        await db.refresh(chunk)
        return chunk

    async def toggle_chunk(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        chunk_id: int,
        enabled: bool,
    ) -> KnowledgeChunk | None:
        """启用 / 停用分块（不重算向量）。"""
        chunk = await self._get_chunk(
            db, tenant_id=tenant_id, kb_id=kb_id, chunk_id=chunk_id
        )
        if chunk is None:
            return None
        chunk.enabled = enabled
        chunk.updated_at = _now()
        await db.commit()
        await db.refresh(chunk)
        return chunk

    async def reembed_chunk(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        chunk_id: int,
    ) -> KnowledgeChunk | None:
        """重新向量化单个分块（内容未变，仅重算 embedding）。"""
        chunk = await self._get_chunk(
            db, tenant_id=tenant_id, kb_id=kb_id, chunk_id=chunk_id
        )
        if chunk is None:
            return None
        try:
            vectors = await embed_texts([chunk.chunk_text])
        except EmbeddingError as exc:
            raise IngestError(str(exc)) from exc
        chunk.embedding = vectors[0]
        chunk.updated_at = _now()
        await db.commit()
        await db.refresh(chunk)
        return chunk


knowledge_service = KnowledgeService()
