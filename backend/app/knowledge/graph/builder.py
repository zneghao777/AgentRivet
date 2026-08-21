"""图谱构建流水线（GRAPH_RAG_SPEC §3 步骤 1~6）。

挂到现有 ingest 之后异步执行：
1. 实体抽取（LLM，批量合并 chunk 控制成本）
2. 关系抽取
3. 事件抽取
4. 实体归一化（alias 规则 + canonical_name）
5. 社区发现 + 摘要
6. 落库 + 索引（实体 embedding、社区写入）

更新 KnowledgeBase.graph_status = 'ready'。全程独立 session，异常不中断其他任务。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import settings
from ...storage.models import DocumentMeta, KnowledgeBase, KnowledgeChunk
from ..embedder import EmbeddingError, embed_texts
from ..jobs import index_job_service
from ..status import refresh_kb_status
from .community import build_communities, summarize_community
from .extractor import ExtractionResult, extract_batch
from .models import GraphCommunity, GraphEntity, GraphEvent, GraphRelation
from .repository import repository
from .resolver import resolve_names

logger = logging.getLogger(__name__)

# 后台图谱构建并发上限
GRAPH_CONCURRENCY = settings.GRAPH_CONCURRENCY
_build_semaphore = asyncio.Semaphore(GRAPH_CONCURRENCY)
_build_tasks: set[asyncio.Task] = set()


def _now() -> datetime:
    return datetime.now(UTC)


def schedule_build(db: AsyncSession | None, kb_id: uuid.UUID, tenant_id: str,
                   *, full: bool = True, doc_id: int | None = None,
                   job_id: uuid.UUID | None = None) -> None:
    """调度后台图谱构建任务（fire-and-forget）。full=True 全量重建，否则增量新增文档。

    db 仅用于校验/标记（可传 None，任务内会开独立 session）。
    """
    task = asyncio.create_task(
        _run_build(kb_id, tenant_id, full=full, doc_id=doc_id, job_id=job_id)
    )
    _build_tasks.add(task)
    task.add_done_callback(_build_tasks.discard)


async def _run_build(kb_id: uuid.UUID, tenant_id: str, *, full: bool = True,
                     doc_id: int | None = None,
                     job_id: uuid.UUID | None = None) -> None:
    """后台构建任务：限并发 + 异常兜底。"""
    from ...storage.db import AsyncSessionLocal

    async with _build_semaphore:
        try:
            async with AsyncSessionLocal() as db:
                await build_kb_graph(
                    db, kb_id=kb_id, tenant_id=tenant_id, full=full,
                    doc_id=doc_id, job_id=job_id,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("图谱构建失败 kb=%s: %s", kb_id, exc)
            try:
                async with AsyncSessionLocal() as db:
                    kb = await db.get(KnowledgeBase, kb_id)
                    if kb is not None and kb.tenant_id == tenant_id:
                        kb.graph_meta = {**(kb.graph_meta or {}), "error": str(exc)}
                        if doc_id is not None:
                            doc = await db.get(DocumentMeta, doc_id)
                            if doc is not None and doc.tenant_id == tenant_id:
                                doc.graph_status = "failed"
                                doc.graph_error = str(exc)
                        job = None
                        if job_id is not None:
                            job = await index_job_service.get(
                                db, tenant_id=tenant_id, kb_id=kb_id, job_id=job_id
                            )
                        if job is None and doc_id is not None:
                            job = await index_job_service.active_for_doc(
                                db, doc_id=doc_id, job_type="graph_build"
                            )
                        await index_job_service.update(
                            db, job, status="failed", stage="failed", progress=100,
                            error_code="INDEX_JOB_FAILED", error_message=str(exc),
                            commit=False,
                        )
                        await db.commit()
                        await refresh_kb_status(
                            db, tenant_id=tenant_id, kb_id=kb_id, commit=True
                        )
            except Exception:  # noqa: BLE001
                logger.exception("图谱构建失败状态回写异常")


async def build_kb_graph(
    db: AsyncSession,
    *,
    kb_id: uuid.UUID,
    tenant_id: str,
    full: bool = True,
    doc_id: int | None = None,
    job_id: uuid.UUID | None = None,
) -> dict:
    """构建知识库图谱，返回统计。doc_id 给定则仅构建该文档（增量）。

    流程（GRAPH_RAG_SPEC §3）：
    1~3. 分批抽取实体/关系/事件；
    4. 实体归一化；
    5. 社区发现 + 摘要；
    6. 落库。
    """
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None or kb.tenant_id != tenant_id:
        raise ValueError("知识库不存在或无权访问")
    # 直接调用底层构建器视为受信任的显式迁移操作；公开 API 仍由
    # GraphRAGService 拒绝标准知识库的图谱构建请求。
    if kb.index_mode == "standard":
        kb.index_mode = "graph_enhanced"
        kb.graph_build_policy = "manual"
        docs_to_include = list(
            (
                await db.execute(
                    select(DocumentMeta).where(
                        DocumentMeta.kb_id == kb_id,
                        DocumentMeta.tenant_id == tenant_id,
                        DocumentMeta.vector_status == "ready",
                        DocumentMeta.graph_override != "exclude",
                    )
                )
            )
            .scalars()
            .all()
        )
        for included_doc in docs_to_include:
            included_doc.graph_status = "pending"
        await db.commit()
    job = None
    if job_id is not None:
        job = await index_job_service.get(
            db, tenant_id=tenant_id, kb_id=kb_id, job_id=job_id
        )
    if job is None:
        if doc_id is not None:
            job = await index_job_service.active_for_doc(
                db, doc_id=doc_id, job_type="graph_build"
            )
        else:
            candidates = await index_job_service.list(
                db, tenant_id=tenant_id, kb_id=kb_id, status="active", limit=20
            )
            job = next((item for item in candidates if item.job_type == "graph_build"), None)
    await index_job_service.update(
        db, job, status="running", stage="cleanup", progress=5
    )

    target_docs = list(
        (
            await db.execute(
                select(DocumentMeta).where(
                    DocumentMeta.kb_id == kb_id,
                    DocumentMeta.tenant_id == tenant_id,
                    DocumentMeta.vector_status == "ready",
                    DocumentMeta.graph_status != "excluded",
                    *([DocumentMeta.id == doc_id] if doc_id is not None else []),
                )
            )
        )
        .scalars()
        .all()
    )
    for target_doc in target_docs:
        target_doc.graph_status = "building"
        target_doc.graph_error = None
    await refresh_kb_status(db, tenant_id=tenant_id, kb_id=kb_id, commit=True)

    # 全量重建：清空旧图谱；增量：清该文档旧图
    if full:
        await repository.clear_kb(db, tenant_id=tenant_id, kb_id=kb_id)
    elif doc_id is not None:
        await repository.delete_by_doc(db, tenant_id=tenant_id, kb_id=kb_id, doc_id=doc_id)

    await index_job_service.update(
        db, job, stage="extracting", progress=15
    )

    # 读取该 kb（可选按 doc）的 chunk
    stmt = (
        select(KnowledgeChunk)
        .where(
            KnowledgeChunk.kb_id == kb_id,
            KnowledgeChunk.tenant_id == tenant_id,
            KnowledgeChunk.enabled.is_(True),
        )
        .order_by(KnowledgeChunk.chunk_index.asc())
    )
    if doc_id is not None:
        stmt = stmt.where(KnowledgeChunk.doc_id == doc_id)
    chunks = list((await db.execute(stmt)).scalars().all())
    if not chunks:
        for target_doc in target_docs:
            target_doc.graph_status = "ready"
            target_doc.graph_indexed_at = _now()
            target_doc.graph_version = kb.graph_version
        await index_job_service.update(
            db, job, status="succeeded", stage="completed", progress=100,
            meta={"chunks_processed": 0}, commit=False,
        )
        await db.commit()
        await refresh_kb_status(db, tenant_id=tenant_id, kb_id=kb_id, commit=True)
        return {"entities": 0, "relations": 0, "events": 0, "communities": 0}

    # 长文档保护：最多处理 GRAPH_BUILD_MAX_CHUNKS 个（GRAPH_RAG_SPEC §8 成本控制）
    chunks = chunks[: settings.GRAPH_BUILD_MAX_CHUNKS]

    # ---- 1~3. 分批抽取（含每批来源 chunk 锚点） ----
    batches = await _extract_in_batches(db, kb_id, tenant_id, chunks)
    await index_job_service.update(
        db, job, stage="normalizing", progress=55
    )

    # ---- 4. 实体归一化 ----
    all_entities = [e for (_batch, result) in batches for e in result.entities]
    name_to_canonical = resolve_names([e.name for e in all_entities])

    # 实体 embedding（复用向量模型）
    embeddings = await _embed_entity_descriptions(
        [(e.name, e.entity_type, e.description) for e in all_entities]
    )

    # 落库实体：按批写入，锚点用该批首个 chunk
    entity_by_name: dict[str, GraphEntity] = {}
    embed_idx = 0
    entity_total = 0
    for (batch_chunks, _result) in batches:
        source_chunk = batch_chunks[0] if batch_chunks else None
        for e in _result.entities:
            canonical = name_to_canonical.get(e.name, e.name)
            embedding = embeddings[embed_idx] if embed_idx < len(embeddings) else None
            embed_idx += 1
            persisted = await repository.upsert_entity(
                db,
                tenant_id=tenant_id,
                kb_id=kb_id,
                doc_id=source_chunk.doc_id if source_chunk else None,
                entity_type=e.entity_type,
                name=e.name,
                canonical_name=canonical,
                description=e.description,
                meta={"source": "extraction"},
                embedding=embedding,
                chunk_id=source_chunk.id if source_chunk else None,
            )
            entity_by_name.setdefault(canonical, persisted)
            entity_by_name.setdefault(e.name, persisted)
            entity_total += 1

    # 落库关系 / 事件（按批锚定来源 chunk）
    relations_written = 0
    events_written = 0
    for (batch_chunks, result) in batches:
        source_chunk = batch_chunks[0] if batch_chunks else None
        relations_written += await _write_relations(
            db, kb_id, tenant_id, result, entity_by_name, source_chunk
        )
        events_written += await _write_events(
            db, kb_id, tenant_id, result, entity_by_name, source_chunk
        )

    await db.commit()
    await index_job_service.update(
        db, job, stage="communities", progress=80
    )

    # ---- 5. 社区发现 + 摘要 ----
    if settings.GRAPH_COMMUNITY_ENABLED:
        await _build_communities(db, kb_id, tenant_id)
        await db.commit()

    counts = {}
    for key, model in (
        ("entities", GraphEntity),
        ("relations", GraphRelation),
        ("events", GraphEvent),
        ("communities", GraphCommunity),
    ):
        counts[key] = int(
            (
                await db.execute(
                    select(func.count(model.id)).where(
                        model.kb_id == kb_id,
                        model.tenant_id == tenant_id,
                    )
                )
            ).scalar_one()
        )
    kb.graph_meta = {
        **(kb.graph_meta or {}),
        **counts,
        "chunks_processed": len(chunks),
        "built_at": _now().isoformat(),
        "error": None,
    }
    for target_doc in target_docs:
        target_doc.graph_status = "ready"
        target_doc.graph_error = None
        target_doc.graph_indexed_at = _now()
        target_doc.graph_version = kb.graph_version
    await index_job_service.update(
        db,
        job,
        status="succeeded",
        stage="completed",
        progress=100,
        meta={"chunks_processed": len(chunks), **counts},
        commit=False,
    )
    await db.commit()
    await refresh_kb_status(db, tenant_id=tenant_id, kb_id=kb_id, commit=True)
    logger.info(
        "图谱构建完成 kb=%s entities=%d relations=%d events=%d communities=%d",
        kb_id, counts["entities"], counts["relations"], counts["events"], counts["communities"],
    )
    return {
        **counts,
    }


async def _extract_in_batches(
    db: AsyncSession,
    kb_id: uuid.UUID,
    tenant_id: str,
    chunks: list[KnowledgeChunk],
) -> list[tuple[list[KnowledgeChunk], ExtractionResult]]:
    """按 GRAPH_BATCH_SIZE 分批合并 chunk 抽取。

    返回 [(batch_chunks, ExtractionResult), ...]，供上层按批落库并锚定来源 chunk。
    """
    batch_size = max(1, settings.GRAPH_BATCH_SIZE)
    batches: list[tuple[list[KnowledgeChunk], ExtractionResult]] = []
    attempted = 0
    failures = 0
    last_error: Exception | None = None
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i : i + batch_size]
        texts = [c.chunk_text for c in batch if c.chunk_text]
        if not texts:
            continue
        attempted += 1
        try:
            result = await extract_batch(
                texts, max_entities=settings.GRAPH_MAX_ENTITIES_PER_CHUNK
            )
        except Exception as exc:  # noqa: BLE001
            failures += 1
            last_error = exc
            logger.warning("分批抽取异常 kb=%s batch=%d: %s", kb_id, i // batch_size, exc)
            continue
        if not result.is_empty():
            batches.append((batch, result))
    if attempted and failures == attempted:
        raise RuntimeError(f"图谱抽取全部失败：{last_error}") from last_error
    return batches


async def _embed_entity_descriptions(
    entities: list[tuple[str, str, str]],
) -> list[list[float]]:
    """实体描述向量化（批量，容忍失败返回空列表）。"""
    if not entities:
        return []
    texts = [(desc or f"{name}：{etype}") for name, etype, desc in entities]
    try:
        return await embed_texts(texts)
    except EmbeddingError as exc:
        logger.warning("实体向量化失败: %s", exc)
        return []


async def _write_relations(
    db: AsyncSession,
    kb_id: uuid.UUID,
    tenant_id: str,
    results: ExtractionResult,
    entity_by_name: dict[str, GraphEntity],
    source_chunk: KnowledgeChunk | None,
) -> int:
    """落库关系，按实体名解析出 id，证据句挂到批内来源 chunk。"""
    written = 0
    for rel in results.relations:
        src = entity_by_name.get(rel.source)
        tgt = entity_by_name.get(rel.target)
        if src is None or tgt is None:
            continue
        if src.id == tgt.id:
            continue
        await repository.upsert_relation(
            db,
            tenant_id=tenant_id,
            kb_id=kb_id,
            source_entity_id=src.id,
            target_entity_id=tgt.id,
            relation_type=rel.relation,
            description=rel.evidence,
            weight=0.5,
            source_chunk_id=source_chunk.id if source_chunk else None,
        )
        written += 1
    return written


async def _write_events(
    db: AsyncSession,
    kb_id: uuid.UUID,
    tenant_id: str,
    results: ExtractionResult,
    entity_by_name: dict[str, GraphEntity],
    source_chunk: KnowledgeChunk | None,
) -> int:
    """落库事件，order_no 全局累加保证时间线可排序。"""
    written = 0
    for order, ev in enumerate(results.events, start=1):
        subj = entity_by_name.get(ev.subject)
        if subj is None:
            continue
        await repository.add_event(
            db,
            tenant_id=tenant_id,
            kb_id=kb_id,
            doc_id=source_chunk.doc_id if source_chunk else None,
            event_type=ev.predicate,
            subject_entity_id=subj.id,
            predicate=ev.predicate,
            object=ev.object,
            order_no=order,
            source_chunk_id=source_chunk.id if source_chunk else None,
            summary=ev.summary,
        )
        written += 1
    return written


async def _build_communities(
    db: AsyncSession, kb_id: uuid.UUID, tenant_id: str
) -> int:
    """社区发现 + LLM 摘要，写入 graph_communities。"""
    # 加载全部实体与关系
    entity_rows = (
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
    if not entity_rows:
        return 0
    entity_ids = [str(e.id) for e in entity_rows]
    name_by_id = {str(e.id): e.canonical_name for e in entity_rows}

    edges: list[tuple[str, str]] = []
    relation_rows = (
        (
            await db.execute(
                select(GraphRelation)
                .where(GraphRelation.kb_id == kb_id, GraphRelation.tenant_id == tenant_id)
            )
        )
        .scalars()
        .all()
    )
    for r in relation_rows:
        edges.append((str(r.source_entity_id), str(r.target_entity_id)))

    candidates = build_communities(entity_ids, edges)
    written = 0
    for cand in candidates:
        members = [name_by_id.get(eid, eid) for eid in cand.member_entity_ids]
        # 只对 level 0 生成 LLM 摘要（控制成本）；level 1 继承拼接
        summary, keywords = await summarize_community(members, [])
        if not summary:
            summary = "、".join(members[:8])
        await repository.upsert_community(
            db,
            tenant_id=tenant_id,
            kb_id=kb_id,
            level=cand.level,
            summary=summary,
            keywords=keywords or members[:5],
            member_entity_ids=[uuid.UUID(eid) for eid in cand.member_entity_ids],
        )
        written += 1
    return written
