"""KnowledgeService 门面：文档任务 / 分块治理 / 测试检索（KB_MANAGEMENT_SPEC §3）。

- 知识库 CRUD（租户隔离：所有查询带 tenant_id，越权返回 None 而非抛错泄露存在性）
- 文档上传（落盘 + DocumentMeta + 异步解析任务调度）与删除
- 分块治理：列表 / 预览 / 编辑 / 启停 / 重新向量化
- 检索（供 knowledge_search 工具与 test-search 调试接口使用）
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..storage.models import (
    DocumentMeta,
    EvaluationRun,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeIndexJob,
    TraceScore,
)
from .chunker import preview_chunks
from .embedder import EmbeddingError, embed_texts
from .files import remove_upload, save_upload
from .ingest import (
    IngestError,
    _refresh_stats,
    parse_and_ingest,
    validate_file,
)
from .jobs import index_job_service
from .policy import GRAPH_OVERRIDES, IndexPolicyError, validate_index_settings
from .retriever import Hit, retrieve, retrieve_hybrid

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
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        name: str,
        description: str | None = None,
        index_mode: str = "standard",
        graph_build_policy: str = "manual",
    ) -> KnowledgeBase | None:
        """创建知识库；同名已存在返回 None（幂等保护）。"""
        name = name.strip()
        if not name:
            return None
        existing = await self.get_by_name(db, tenant_id=tenant_id, name=name)
        if existing is not None and existing.status == "active":
            return None
        validate_index_settings(index_mode, graph_build_policy)
        kb = KnowledgeBase(
            tenant_id=tenant_id,
            name=name,
            description=description,
            index_mode=index_mode,
            graph_build_policy=graph_build_policy,
            index_status="empty",
            graph_status="none" if index_mode == "graph_enhanced" else "disabled",
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

    async def update_index_settings(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        graph_build_policy: str | None = None,
        settings_patch: dict[str, Any] | None = None,
    ) -> KnowledgeBase | None:
        kb = await self.get(db, tenant_id=tenant_id, kb_id=kb_id)
        if kb is None:
            return None
        policy = graph_build_policy or kb.graph_build_policy
        validate_index_settings(kb.index_mode, policy)
        kb.graph_build_policy = policy
        if settings_patch is not None:
            allowed = {"chunk_size", "chunk_overlap", "graph_max_hop", "graph_entity_types"}
            kb.settings = {
                **(kb.settings or {}),
                **{key: value for key, value in settings_patch.items() if key in allowed},
            }
        kb.updated_at = _now()
        await db.commit()
        await db.refresh(kb)
        return kb

    async def transition_index_mode(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        target_mode: str,
        graph_build_policy: str = "manual",
        build_existing_documents: bool = True,
    ) -> dict[str, Any] | None:
        from .graph.builder import schedule_build
        from .status import refresh_kb_status

        kb = await self.get(db, tenant_id=tenant_id, kb_id=kb_id)
        if kb is None:
            return None
        validate_index_settings(target_mode, graph_build_policy)
        docs = list(
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
        estimated_chunks = int(
            (
                await db.execute(
                    select(func.count(KnowledgeChunk.id)).where(
                        KnowledgeChunk.kb_id == kb_id,
                        KnowledgeChunk.tenant_id == tenant_id,
                    )
                )
            ).scalar_one()
        )
        kb.index_mode = target_mode
        kb.graph_build_policy = graph_build_policy
        job_ids: list[str] = []
        if target_mode == "standard":
            for doc in docs:
                active = await index_job_service.active_for_doc(
                    db, doc_id=doc.id, job_type="graph_build"
                )
                if active is not None and active.status == "queued":
                    await index_job_service.cancel(db, active)
        else:
            for doc in docs:
                if doc.graph_override == "exclude":
                    doc.graph_status = "excluded"
                    continue
                if doc.vector_status != "ready":
                    doc.graph_status = "pending"
                    continue
                if doc.graph_status != "ready":
                    doc.graph_status = "pending"
                if (
                    build_existing_documents
                    and graph_build_policy == "auto"
                    and settings.GRAPH_ENABLED
                    and doc.graph_status != "ready"
                ):
                    job, created = await index_job_service.enqueue(
                        db,
                        tenant_id=tenant_id,
                        kb_id=kb_id,
                        doc_id=doc.id,
                        job_type="graph_build",
                        meta={"reason": "mode_transition", "graph_version": kb.graph_version},
                    )
                    job_ids.append(str(job.id))
                    if created:
                        schedule_build(
                            db, kb_id, tenant_id, full=False, doc_id=doc.id, job_id=job.id
                        )
        await db.commit()
        await refresh_kb_status(db, tenant_id=tenant_id, kb_id=kb_id, commit=True)
        return {
            "transition_id": str(uuid.uuid4()),
            "affected_documents": len(docs),
            "estimated_chunks": estimated_chunks,
            "job_ids": job_ids,
        }

    async def update_graph_override(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        doc_id: int,
        graph_override: str,
    ) -> DocumentMeta | None:
        from .graph.builder import schedule_build
        from .graph.repository import repository
        from .policy import resolve_graph_action
        from .status import refresh_kb_status

        kb = await self.get(db, tenant_id=tenant_id, kb_id=kb_id)
        doc = await self.get_document(
            db, tenant_id=tenant_id, kb_id=kb_id, doc_id=doc_id
        )
        if kb is None or doc is None:
            return None
        action = resolve_graph_action(
            platform_enabled=settings.GRAPH_ENABLED,
            index_mode=kb.index_mode,
            build_policy=kb.graph_build_policy,
            graph_override=graph_override,
        )
        doc.graph_override = graph_override
        if action == "excluded":
            await repository.delete_by_doc(
                db, tenant_id=tenant_id, kb_id=kb_id, doc_id=doc_id
            )
            doc.graph_status = "excluded"
        elif doc.vector_status == "ready":
            doc.graph_status = "pending"
            if action == "enqueue":
                job, created = await index_job_service.enqueue(
                    db, tenant_id=tenant_id, kb_id=kb_id, doc_id=doc_id,
                    job_type="graph_build",
                    meta={"content_hash": doc.content_hash, "graph_version": kb.graph_version},
                )
                if created:
                    schedule_build(
                        db, kb_id, tenant_id, full=False, doc_id=doc_id, job_id=job.id
                    )
        await db.commit()
        await refresh_kb_status(db, tenant_id=tenant_id, kb_id=kb_id, commit=True)
        await db.refresh(doc)
        return doc

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
                "parse_method": doc.parse_method,
                "parse_source": doc.parse_source,
                "graph_override": doc.graph_override,
                "vector_status": doc.vector_status,
                "graph_status": doc.graph_status,
                "vector_error": doc.vector_error,
                "graph_error": doc.graph_error,
                "vector_indexed_at": (
                    doc.vector_indexed_at.isoformat() if doc.vector_indexed_at else None
                ),
                "graph_indexed_at": (
                    doc.graph_indexed_at.isoformat() if doc.graph_indexed_at else None
                ),
                "graph_version": doc.graph_version,
                "content_hash": doc.content_hash,
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
        parse_method: str,
        replace: bool = True,
        mime_type: str | None = None,
        graph_override: str = "inherit",
    ) -> DocumentMeta:
        """上传文档：校验 → 落盘 → 建 DocumentMeta(pending) → 调度异步解析。

        replace=True：同名文件先删除旧文档记录（含文件/chunks）再重建（幂等）。
        返回 DocumentMeta（doc_id / source_file / parse_status）。
        """
        kb = await self.get(db, tenant_id=tenant_id, kb_id=kb_id)
        if kb is None:
            raise IngestError("知识库不存在或无权访问")
        if parse_method not in {"text", "ocr"}:
            raise IngestError("解析方式必须是 text 或 ocr")
        if graph_override not in GRAPH_OVERRIDES:
            raise IndexPolicyError("无效的文档图谱参与方式")
        if kb.index_mode == "standard" and graph_override == "include":
            raise IndexPolicyError("标准知识库中的文档不能强制参与图谱构建")
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
            parse_method=parse_method,
            graph_override=graph_override,
            vector_status="pending",
            graph_status=("excluded" if kb.index_mode == "standard" or graph_override == "exclude" else "pending"),
            content_hash=hashlib.sha256(content).hexdigest(),
        )
        db.add(doc)
        await db.flush()
        await db.commit()
        await db.refresh(doc)
        await index_job_service.enqueue(
            db,
            tenant_id=tenant_id,
            kb_id=kb_id,
            doc_id=doc.id,
            job_type="parse",
            meta={"source_file": filename, "content_hash": doc.content_hash, "parse_method": parse_method},
        )
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
        from .graph.repository import repository

        doc.vector_status = "stale"
        doc.graph_status = "excluded" if doc.graph_status == "excluded" else "stale"
        await db.flush()
        await repository.delete_by_doc(
            db, tenant_id=tenant_id, kb_id=kb_id, doc_id=doc.id
        )
        await db.execute(
            delete(KnowledgeChunk).where(
                KnowledgeChunk.doc_id == doc.id,
                KnowledgeChunk.kb_id == kb_id,
                KnowledgeChunk.tenant_id == tenant_id,
            )
        )
        doc.parse_status = "reparsing"
        doc.error = None
        doc.vector_error = None
        doc.graph_error = None
        await db.commit()
        await db.refresh(doc)
        await index_job_service.enqueue(
            db,
            tenant_id=tenant_id,
            kb_id=kb_id,
            doc_id=doc.id,
            job_type="parse",
            meta={"source_file": doc.source_file, "reason": "reparse", "parse_method": doc.parse_method},
        )
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
        from .graph.repository import repository

        await db.execute(
            update(KnowledgeIndexJob)
            .where(
                KnowledgeIndexJob.doc_id == doc.id,
                KnowledgeIndexJob.tenant_id == doc.tenant_id,
                KnowledgeIndexJob.kb_id == doc.kb_id,
            )
            .values(
                doc_id=None,
                status="cancelled",
                stage="document_deleted",
                finished_at=_now(),
            )
        )
        await repository.delete_by_doc(
            db, tenant_id=doc.tenant_id, kb_id=doc.kb_id, doc_id=doc.id
        )
        await db.execute(
            delete(KnowledgeChunk).where(
                KnowledgeChunk.doc_id == doc.id,
                KnowledgeChunk.kb_id == doc.kb_id,
                KnowledgeChunk.tenant_id == doc.tenant_id,
            )
        )
        await db.execute(delete(DocumentMeta).where(DocumentMeta.id == doc.id))
        if doc.file_path:
            await asyncio.to_thread(remove_upload, doc.file_path)
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

    async def _resolve_kb_ids(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_ids: list[uuid.UUID] | None = None,
        kb_name: str | None = None,
    ) -> list[uuid.UUID] | None:
        """解析有效知识库 id 列表；None 表示无可检索知识库。

        - kb_name 给定：仅该知识库；不存在/不属于当前租户/非 active → None（防越权）。
        - kb_ids 给定（会话白名单）：仅白名单内且 active 的。
        - 均未给定：当前租户全部 active 知识库。
        """
        if kb_name:
            kb = await self.get_by_name(db, tenant_id=tenant_id, name=kb_name)
            if kb is None or kb.status != "active":
                return None
            return [kb.id]
        stmt = select(KnowledgeBase.id).where(
            KnowledgeBase.tenant_id == tenant_id,
            KnowledgeBase.status == "active",
        )
        if kb_ids is not None:
            if not kb_ids:
                return None
            stmt = stmt.where(KnowledgeBase.id.in_(kb_ids))
        result = await db.execute(stmt)
        ids = list(result.scalars().all())
        return ids or None

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
        """供知识检索工具使用（兼容旧入口）：kb_name 指定时必须校验其归属当前租户（防越权）。

        - kb_name 给定：仅检索该知识库；若不存在/不属于当前租户 → 返回空。
        - kb_ids 给定（会话白名单）：仅检索白名单内知识库。
        - 均未给定：检索当前租户全部知识库。
        """
        resolved = await self._resolve_kb_ids(
            db, tenant_id=tenant_id, kb_ids=kb_ids, kb_name=kb_name
        )
        if not resolved:
            return []
        return await retrieve(
            db,
            tenant_id=tenant_id,
            query=query,
            kb_ids=resolved,
            top_k=top_k,
        )

    async def retrieve_v2(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        query: str,
        kb_ids: list[uuid.UUID] | None = None,
        kb_name: str | None = None,
        top_k: int = 5,
        strategy: str = "auto",
        rerank: bool = True,
        expand_query: bool = False,
        mode: str | None = None,
    ) -> list[Hit]:
        """检索 v2（AGENTIC_RAG_UPGRADE_SPEC §3.7）：供 knowledge_search 工具使用。

        校验租户/知识库 → 改写(可选) → 混合检索 → Rerank(可选，不可用自动降级)。
        kb_name 指定时校验归属当前租户（防越权，沿用现逻辑）。
        """
        resolved = await self._resolve_kb_ids(
            db, tenant_id=tenant_id, kb_ids=kb_ids, kb_name=kb_name
        )
        if not resolved:
            return []
        if mode is not None and settings.KB_UNIFIED_RETRIEVAL_ENABLED:
            from .retrieval_service import retrieve_knowledge

            result = await retrieve_knowledge(
                db,
                tenant_id=tenant_id,
                query=query,
                kb_ids=resolved,
                mode=mode,
                top_k=top_k,
                rerank=rerank,
                expand_query=expand_query,
            )
            return [
                Hit(
                    text=item["text"],
                    source_file=item["source_file"],
                    chunk_index=item["chunk_index"],
                    score=float(item["score"]),
                    kb_id=uuid.UUID(item["kb_id"]),
                    chunk_id=int(item["chunk_id"]),
                    strategy=item["strategy"],
                    rerank_score=item.get("rerank_score"),
                    doc_id=item.get("doc_id"),
                )
                for item in result["hits"]
            ]
        return await retrieve_hybrid(
            db,
            tenant_id=tenant_id,
            query=query,
            kb_ids=resolved,
            top_k=top_k,
            strategy=strategy,
            rerank=rerank,
            expand_query=expand_query,
        )

    async def retrieve_unified(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        query: str,
        kb_ids: list[uuid.UUID] | None = None,
        kb_name: str | None = None,
        mode: str = "auto",
        top_k: int = 5,
        rerank: bool = True,
        expand_query: bool = False,
        graph_hop: int = 2,
    ) -> dict[str, Any]:
        from .retrieval_service import retrieve_knowledge

        resolved = await self._resolve_kb_ids(
            db, tenant_id=tenant_id, kb_ids=kb_ids, kb_name=kb_name
        )
        return await retrieve_knowledge(
            db,
            tenant_id=tenant_id,
            query=query,
            kb_ids=resolved or [],
            mode=mode,
            top_k=top_k,
            rerank=rerank,
            expand_query=expand_query,
            graph_hop=graph_hop,
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
            "has_embedding": chunk.embedding is not None,
            "metadata": chunk.meta or {},
            "created_at": chunk.created_at.isoformat() if chunk.created_at else None,
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
                    .order_by(
                        KnowledgeChunk.doc_id.asc().nulls_last(),
                        KnowledgeChunk.chunk_index.asc(),
                        KnowledgeChunk.id.asc(),
                    )
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

    # ---------- 检索评测（AGENTIC_RAG_UPGRADE_SPEC §3.7 / §3.9） ----------

    async def evaluate(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        name: str,
        dataset: list[dict],
        variants: list[str] | None = None,
        judge: bool = False,
    ) -> EvaluationRun | None:
        """检索评测 runner：

        1. 对每个用例跑 variants（如 baseline / hybrid / hybrid+rerank）检索，
           用 expected_chunk_ids/expected_doc_ids 计算 hit_rate@k / mrr@k。
        2. judge=True 时追加 LLM-as-judge 端到端评分（faithfulness/relevancy），写 TraceScore。
        3. 落 EvaluationRun（dataset / variants / results）。
        - kb 不存在或不属于当前租户 → None（接口层转 404）。
        """
        from . import evaluator

        kb = await self.get(db, tenant_id=tenant_id, kb_id=kb_id)
        if kb is None:
            return None

        cases, skipped = evaluator.parse_dataset(dataset)
        variant_names = variants or evaluator.DEFAULT_VARIANTS

        # expected_doc_ids 宽松匹配：解析该 kb 下这些文档的全部分块 id
        all_doc_ids: set[str] = set()
        for case in cases:
            all_doc_ids |= case.expected_doc_ids
        doc_chunks: dict[str, set[str]] = {}
        if all_doc_ids:
            valid_doc_ints = [int(d) for d in all_doc_ids if str(d).isdigit()]
            if valid_doc_ints:
                rows = (
                    await db.execute(
                        select(KnowledgeChunk.doc_id, KnowledgeChunk.id).where(
                            KnowledgeChunk.kb_id == kb_id,
                            KnowledgeChunk.tenant_id == tenant_id,
                            KnowledgeChunk.doc_id.in_(valid_doc_ints),
                        )
                    )
                ).fetchall()
                for doc_id, chunk_id in rows:
                    doc_chunks.setdefault(str(doc_id), set()).add(str(chunk_id))

        variant_results: list[dict] = []
        last_hits_by_case: dict[int, list[Hit]] = {}
        for variant in variant_names:
            config = evaluator.VARIANT_CONFIGS.get(variant)
            if config is None:
                continue
            result_ids: list[list[str]] = []
            expected_sets: list[set[str]] = []
            case_rows: list[dict] = []
            for index, case in enumerate(cases):
                expected = set(case.expected_chunk_ids)
                for doc_id in case.expected_doc_ids:
                    expected |= doc_chunks.get(doc_id, set())
                hits = await retrieve_hybrid(
                    db,
                    tenant_id=tenant_id,
                    query=case.query,
                    kb_ids=[kb_id],
                    top_k=settings.RAG_TOP_K,
                    strategy=config["strategy"],
                    rerank=config["rerank"],
                )
                top_ids = [str(h.chunk_id) for h in hits]
                result_ids.append(top_ids)
                expected_sets.append(expected)
                first_rank = next(
                    (rank for rank, cid in enumerate(top_ids, start=1) if cid in expected),
                    0,
                )
                case_rows.append(
                    {
                        "query": case.query,
                        "hit": bool(expected & set(top_ids)),
                        "rank": first_rank,
                        "top_chunk_ids": top_ids,
                    }
                )
                last_hits_by_case[index] = hits
            variant_results.append(
                {
                    "variant": variant,
                    "hit_rate": round(evaluator.hit_rate(result_ids, expected_sets), 4),
                    "mrr": round(evaluator.mrr(result_ids, expected_sets), 4),
                    "cases": case_rows,
                }
            )

        # LLM-as-judge（可选）：对每个用例基于最优上下文做端到端评分，写 TraceScore
        judge_summary: dict | None = None
        if judge and cases:
            judge_cases: list[dict] = []
            faith_scores: list[float] = []
            relev_scores: list[float] = []
            for index, case in enumerate(cases):
                hits = last_hits_by_case.get(index, [])
                contexts = [h.text for h in hits]
                answer = case.reference_answer
                if not answer:
                    answer = await evaluator.generate_answer(case.query, contexts)
                verdict = await evaluator.judge_answer(case.query, contexts, answer)
                judge_cases.append(
                    {
                        "query": case.query,
                        "faithfulness": verdict["faithfulness"],
                        "relevancy": verdict["relevancy"],
                    }
                )
                faith_scores.append(verdict["faithfulness"])
                relev_scores.append(verdict["relevancy"])
            judge_summary = {
                "avg_faithfulness": round(sum(faith_scores) / len(faith_scores), 4),
                "avg_relevancy": round(sum(relev_scores) / len(relev_scores), 4),
                "cases": judge_cases,
            }

        run = EvaluationRun(
            tenant_id=tenant_id,
            name=name,
            status="done",
            dataset={"kb_id": str(kb_id), "items": dataset},
            variants=variant_names,
            results={
                "variants": variant_results,
                "judge": judge_summary,
                "stats": {"total": len(dataset), "valid": len(cases), "skipped": skipped},
            },
        )
        db.add(run)
        await db.flush()

        if judge_summary:
            for index, jc in enumerate(judge_summary["cases"]):
                trace_id = f"rag-eval-{run.id}-{index}"
                for score_name in ("faithfulness", "relevancy"):
                    db.add(
                        TraceScore(
                            tenant_id=tenant_id,
                            trace_id=trace_id,
                            score_name=score_name,
                            value=float(jc[score_name]),
                            source="rag_eval",
                            reason=f"query={jc['query'][:80]}",
                        )
                    )
        await db.commit()
        await db.refresh(run)
        return run


knowledge_service = KnowledgeService()
