"""知识库（RAG）管理路由：CRUD / 文档解析任务 / 分块治理 / 测试检索。

遵循 router_skills.py 风格：ApiResponse 包装、require_auth 鉴权、audit 审计。
🔒 安全红线：所有操作校验 kb.tenant_id == auth.tenant.tenant_id，越权返回 404（SPEC §3.8）。
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..knowledge.embedder import EmbeddingError
from ..knowledge.ingest import IngestError
from ..knowledge.jobs import index_job_service, job_to_dict
from ..knowledge.policy import IndexPolicyError
from ..knowledge.service import knowledge_service
from ..observability import audit
from ..storage.db import get_db
from ..storage.models import EvaluationRun
from .deps import AuthContext, require_auth
from .schemas import (
    ApiResponse,
    ChunkEnabledRequest,
    ChunkListResponse,
    ChunkPreviewItem,
    ChunkPreviewRequest,
    ChunkUpdateRequest,
    DocumentGraphOverrideRequest,
    EvaluateRequest,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseResponse,
    KnowledgeBaseStatusRequest,
    KnowledgeChunkItem,
    KnowledgeDocResponse,
    KnowledgeHit,
    KnowledgeIndexModeTransitionRequest,
    KnowledgeIndexSettingsRequest,
    KnowledgeSearchRequest,
    KnowledgeUploadResponse,
    TestSearchRequest,
)

router = APIRouter(prefix="/knowledge-bases", tags=["knowledge"])


def _to_kb_resp(kb, *, active_job_count: int = 0) -> KnowledgeBaseResponse:
    graph_meta = kb.graph_meta or {}
    return KnowledgeBaseResponse(
        kb_id=str(kb.id),
        name=kb.name,
        description=kb.description,
        doc_count=kb.doc_count,
        chunk_count=kb.chunk_count,
        index_mode=kb.index_mode,
        graph_build_policy=kb.graph_build_policy,
        index_status=kb.index_status,
        graph_status=kb.graph_status,
        graph_version=kb.graph_version,
        graph_meta=graph_meta,
        settings=kb.settings or {},
        entity_count=int(graph_meta.get("entities", 0) or 0),
        active_job_count=active_job_count,
        status=kb.status,
        created_at=kb.created_at.isoformat() if kb.created_at else None,
        updated_at=kb.updated_at.isoformat() if kb.updated_at else None,
    )


def _to_doc_resp(doc: dict) -> KnowledgeDocResponse:
    return KnowledgeDocResponse(
        doc_id=doc["doc_id"],
        kb_id=doc.get("kb_id"),
        source_file=doc["source_file"],
        file_size=doc.get("file_size", 0),
        mime_type=doc.get("mime_type"),
        parse_status=doc.get("parse_status", "pending"),
        parse_method=doc.get("parse_method", "text"),
        parse_source=doc.get("parse_source"),
        graph_override=doc.get("graph_override", "inherit"),
        vector_status=doc.get("vector_status", "pending"),
        graph_status=doc.get("graph_status", "excluded"),
        vector_error=doc.get("vector_error"),
        graph_error=doc.get("graph_error"),
        vector_indexed_at=doc.get("vector_indexed_at"),
        graph_indexed_at=doc.get("graph_indexed_at"),
        graph_version=doc.get("graph_version"),
        content_hash=doc.get("content_hash"),
        chunk_count=doc.get("chunk_count", 0),
        error=doc.get("error"),
        created_at=doc.get("created_at"),
        updated_at=doc.get("updated_at"),
    )


def _to_chunk_resp(chunk: dict) -> KnowledgeChunkItem:
    return KnowledgeChunkItem(
        chunk_id=chunk["chunk_id"],
        kb_id=chunk["kb_id"],
        doc_id=chunk.get("doc_id"),
        source_file=chunk["source_file"],
        heading=chunk.get("heading"),
        text=chunk["text"],
        char_count=chunk["char_count"],
        enabled=chunk["enabled"],
        chunk_index=chunk["chunk_index"],
        has_embedding=chunk.get("has_embedding", False),
        metadata=chunk.get("metadata") or {},
        created_at=chunk.get("created_at"),
        updated_at=chunk.get("updated_at"),
    )


async def _require_kb(auth: AuthContext, db: AsyncSession, kb_id: uuid.UUID):
    """校验知识库存在且属于当前租户；否则 404（不泄露存在性）。"""
    kb = await knowledge_service.get(db, tenant_id=auth.tenant.tenant_id, kb_id=kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    return kb


@router.get("", response_model=ApiResponse)
async def list_knowledge_bases(
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """知识库列表（含 doc_count / chunk_count）。"""
    items = await knowledge_service.list(db, tenant_id=auth.tenant.tenant_id)
    responses = []
    for kb in items:
        active = await index_job_service.list(
            db, tenant_id=auth.tenant.tenant_id, kb_id=kb.id, status="active"
        )
        responses.append(_to_kb_resp(kb, active_job_count=len(active)))
    return ApiResponse(data=responses)


@router.post("", response_model=ApiResponse)
async def create_knowledge_base(
    body: KnowledgeBaseCreateRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """创建知识库。"""
    if body.index_mode == "graph_enhanced" and not settings.GRAPH_ENABLED:
        raise HTTPException(
            status_code=503,
            detail={"code": "GRAPH_PLATFORM_DISABLED", "message": "当前环境未启用图谱服务"},
        )
    try:
        kb = await knowledge_service.create(
            db,
            tenant_id=auth.tenant.tenant_id,
            name=body.name,
            description=body.description,
            index_mode=body.index_mode,
            graph_build_policy=body.graph_build_policy,
        )
    except IndexPolicyError as exc:
        raise HTTPException(
            status_code=409, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    if kb is None:
        raise HTTPException(status_code=409, detail="知识库名称已存在")
    await audit.audit(
        auth.tenant.tenant_id,
        "knowledge.create",
        user_id=auth.user_id,
        detail={
            "kb": kb.name,
            "kb_id": str(kb.id),
            "index_mode": kb.index_mode,
            "graph_build_policy": kb.graph_build_policy,
        },
    )
    return ApiResponse(data=_to_kb_resp(kb))


@router.get("/capabilities", response_model=ApiResponse)
async def knowledge_capabilities(
    auth: AuthContext = Depends(require_auth),
):
    return ApiResponse(
        data={
            "index_mode_enabled": settings.KB_INDEX_MODE_ENABLED,
            "index_jobs_enabled": settings.KB_INDEX_JOBS_ENABLED,
            "unified_retrieval_enabled": settings.KB_UNIFIED_RETRIEVAL_ENABLED,
            "graph_workspace_enabled": settings.KB_GRAPH_WORKSPACE_ENABLED,
            "graph_enabled": settings.GRAPH_ENABLED,
        }
    )


@router.get("/{kb_id}", response_model=ApiResponse)
async def get_knowledge_base(
    kb_id: uuid.UUID,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """知识库详情 + 文档列表（含 parse_status）。"""
    kb = await _require_kb(auth, db, kb_id)
    docs = await knowledge_service.list_documents(
        db, tenant_id=auth.tenant.tenant_id, kb_id=kb_id
    )
    return ApiResponse(
        data={
            "kb": _to_kb_resp(kb),
            "documents": [_to_doc_resp(doc) for doc in docs],
        }
    )


@router.patch("/{kb_id}/index-settings", response_model=ApiResponse)
async def update_index_settings(
    kb_id: uuid.UUID,
    body: KnowledgeIndexSettingsRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    await _require_kb(auth, db, kb_id)
    try:
        kb = await knowledge_service.update_index_settings(
            db,
            tenant_id=auth.tenant.tenant_id,
            kb_id=kb_id,
            graph_build_policy=body.graph_build_policy,
            settings_patch=body.settings,
        )
    except IndexPolicyError as exc:
        raise HTTPException(
            status_code=409, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    await audit.audit(
        auth.tenant.tenant_id,
        "knowledge.index_settings_update",
        user_id=auth.user_id,
        detail={"kb_id": str(kb_id), "new": body.model_dump(exclude_none=True)},
    )
    return ApiResponse(data=_to_kb_resp(kb))


@router.post("/{kb_id}/index-mode-transition", response_model=ApiResponse)
async def transition_index_mode(
    kb_id: uuid.UUID,
    body: KnowledgeIndexModeTransitionRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    kb = await _require_kb(auth, db, kb_id)
    if body.target_mode == "graph_enhanced" and not settings.GRAPH_ENABLED:
        raise HTTPException(
            status_code=503,
            detail={"code": "GRAPH_PLATFORM_DISABLED", "message": "当前环境未启用图谱服务"},
        )
    try:
        data = await knowledge_service.transition_index_mode(
            db,
            tenant_id=auth.tenant.tenant_id,
            kb_id=kb_id,
            target_mode=body.target_mode,
            graph_build_policy=body.graph_build_policy,
            build_existing_documents=body.build_existing_documents,
        )
    except IndexPolicyError as exc:
        raise HTTPException(
            status_code=409, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    await audit.audit(
        auth.tenant.tenant_id,
        "knowledge.index_mode_transition",
        user_id=auth.user_id,
        detail={"kb_id": str(kb_id), "old": kb.index_mode, "new": body.target_mode, **(data or {})},
    )
    return ApiResponse(data=data)


@router.patch("/{kb_id}/status", response_model=ApiResponse)
async def update_knowledge_base_status(
    kb_id: uuid.UUID,
    body: KnowledgeBaseStatusRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """Enable or disable a knowledge base for Agent retrieval."""
    kb = await knowledge_service.set_enabled(
        db,
        tenant_id=auth.tenant.tenant_id,
        kb_id=kb_id,
        enabled=body.enabled,
    )
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    await audit.audit(
        auth.tenant.tenant_id,
        "knowledge.enable" if body.enabled else "knowledge.disable",
        user_id=auth.user_id,
        detail={"kb": kb.name},
    )
    return ApiResponse(data=_to_kb_resp(kb))


@router.delete("/{kb_id}", response_model=ApiResponse)
async def delete_knowledge_base(
    kb_id: uuid.UUID,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """删除知识库（级联删文档/chunks/文件）。"""
    kb = await _require_kb(auth, db, kb_id)
    await knowledge_service.delete(db, tenant_id=auth.tenant.tenant_id, kb_id=kb_id)
    await audit.audit(
        auth.tenant.tenant_id,
        "knowledge.delete",
        user_id=auth.user_id,
        detail={"kb": kb.name},
    )
    return ApiResponse(data={"kb_id": str(kb_id)})


# ---------- 文档（DocumentMeta / 解析任务） ----------


async def _do_upload(
    kb_id: uuid.UUID,
    file: UploadFile,
    replace: bool,
    parse_method: Literal["text", "ocr"],
    graph_override: str,
    auth: AuthContext,
    db: AsyncSession,
):
    """上传文档公共逻辑：落盘 + 建 DocumentMeta(pending) + 调度异步解析。"""
    await _require_kb(auth, db, kb_id)
    content = await file.read()
    try:
        doc = await knowledge_service.upload_document(
            db,
            tenant_id=auth.tenant.tenant_id,
            kb_id=kb_id,
            filename=file.filename or "unnamed",
            content=content,
            parse_method=parse_method,
            replace=replace,
            mime_type=file.content_type,
            graph_override=graph_override,
        )
    except IndexPolicyError as exc:
        raise HTTPException(
            status_code=409, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    except IngestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await audit.audit(
        auth.tenant.tenant_id,
        "knowledge.upload",
        user_id=auth.user_id,
        detail={"kb": str(kb_id), "file": file.filename, "doc_id": doc.id, "parse_method": parse_method},
    )
    jobs = await index_job_service.list(
        db, tenant_id=auth.tenant.tenant_id, kb_id=kb_id, status="active"
    )
    doc_jobs = [job for job in jobs if job.doc_id == doc.id]
    return ApiResponse(
        data=KnowledgeUploadResponse(
            kb_id=str(kb_id),
            doc_id=doc.id,
            source_file=doc.source_file,
            parse_status=doc.parse_status,
            parse_method=doc.parse_method,
            vector_status=doc.vector_status,
            graph_status=doc.graph_status,
            chunk_count=0,
            job_ids=[str(job.id) for job in doc_jobs],
        )
    )


@router.post("/{kb_id}/documents/upload", response_model=ApiResponse)
async def upload_document(
    kb_id: uuid.UUID,
    file: UploadFile = File(...),
    replace: bool = Form(True),
    parse_method: Literal["text", "ocr"] = Form(...),
    graph_override: str = Form("inherit"),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """上传文档并触发异步解析（multipart）。返回 pending 状态，解析后台进行。"""
    return await _do_upload(kb_id, file, replace, parse_method, graph_override, auth, db)


@router.post("/{kb_id}/documents", response_model=ApiResponse)
async def upload_document_legacy(
    kb_id: uuid.UUID,
    file: UploadFile = File(...),
    replace: bool = Form(True),
    parse_method: Literal["text", "ocr"] = Form(...),
    graph_override: str = Form("inherit"),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """兼容旧路径 /documents：等价于 /documents/upload。"""
    return await _do_upload(kb_id, file, replace, parse_method, graph_override, auth, db)


@router.post("/{kb_id}/documents/{doc_id}/reparse", response_model=ApiResponse)
async def reparse_document(
    kb_id: uuid.UUID,
    doc_id: int,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """重新解析文档：清旧 chunks 后重跑解析（GLM 或本地降级）。"""
    await _require_kb(auth, db, kb_id)
    doc = await knowledge_service.reparse_document(
        db, tenant_id=auth.tenant.tenant_id, kb_id=kb_id, doc_id=doc_id
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    await audit.audit(
        auth.tenant.tenant_id,
        "knowledge.reparse",
        user_id=auth.user_id,
        detail={"kb": str(kb_id), "doc_id": doc_id, "file": doc.source_file},
    )
    return ApiResponse(data={"kb_id": str(kb_id), "doc_id": doc_id, "parse_status": doc.parse_status})


@router.get("/{kb_id}/documents/{doc_id}/index-status", response_model=ApiResponse)
async def document_index_status(
    kb_id: uuid.UUID,
    doc_id: int,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    await _require_kb(auth, db, kb_id)
    doc = await knowledge_service.get_document(
        db, tenant_id=auth.tenant.tenant_id, kb_id=kb_id, doc_id=doc_id
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    jobs = await index_job_service.list(
        db, tenant_id=auth.tenant.tenant_id, kb_id=kb_id, limit=100
    )
    return ApiResponse(
        data={
            "doc_id": doc.id,
            "parse_status": doc.parse_status,
            "vector_status": doc.vector_status,
            "graph_status": doc.graph_status,
            "vector_error": doc.vector_error,
            "graph_error": doc.graph_error,
            "vector_indexed_at": doc.vector_indexed_at.isoformat() if doc.vector_indexed_at else None,
            "graph_indexed_at": doc.graph_indexed_at.isoformat() if doc.graph_indexed_at else None,
            "jobs": [job_to_dict(job) for job in jobs if job.doc_id == doc.id],
        }
    )


@router.patch("/{kb_id}/documents/{doc_id}/graph-override", response_model=ApiResponse)
async def update_document_graph_override(
    kb_id: uuid.UUID,
    doc_id: int,
    body: DocumentGraphOverrideRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    await _require_kb(auth, db, kb_id)
    try:
        doc = await knowledge_service.update_graph_override(
            db,
            tenant_id=auth.tenant.tenant_id,
            kb_id=kb_id,
            doc_id=doc_id,
            graph_override=body.graph_override,
        )
    except IndexPolicyError as exc:
        raise HTTPException(
            status_code=409, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    await audit.audit(
        auth.tenant.tenant_id,
        "knowledge.document_graph_override",
        user_id=auth.user_id,
        detail={"kb_id": str(kb_id), "doc_id": doc_id, "new": body.graph_override},
    )
    return ApiResponse(
        data={
            "doc_id": doc.id,
            "graph_override": doc.graph_override,
            "graph_status": doc.graph_status,
        }
    )


@router.post("/{kb_id}/documents/{doc_id}/graph/build", response_model=ApiResponse)
async def build_document_graph(
    kb_id: uuid.UUID,
    doc_id: int,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    from ..knowledge.graph.service import graph_service

    await _require_kb(auth, db, kb_id)
    try:
        data = await graph_service.trigger_document_build(
            db, tenant_id=auth.tenant.tenant_id, kb_id=kb_id, doc_id=doc_id
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="document not found")
    except IndexPolicyError as exc:
        status_code = 503 if exc.code == "GRAPH_PLATFORM_DISABLED" else 409
        raise HTTPException(
            status_code=status_code, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    return ApiResponse(data=data)


@router.post("/{kb_id}/documents/{doc_id}/retry", response_model=ApiResponse)
async def retry_document_index(
    kb_id: uuid.UUID,
    doc_id: int,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    from ..knowledge.graph.service import graph_service

    await _require_kb(auth, db, kb_id)
    doc = await knowledge_service.get_document(
        db, tenant_id=auth.tenant.tenant_id, kb_id=kb_id, doc_id=doc_id
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    if doc.graph_status == "failed":
        data = await graph_service.trigger_document_build(
            db, tenant_id=auth.tenant.tenant_id, kb_id=kb_id, doc_id=doc_id
        )
    elif doc.parse_status == "failed" or doc.vector_status == "failed":
        retried = await knowledge_service.reparse_document(
            db, tenant_id=auth.tenant.tenant_id, kb_id=kb_id, doc_id=doc_id
        )
        data = {"doc_id": doc_id, "stage": "parse" if doc.parse_status == "failed" else "vectorize", "status": retried.parse_status}
    else:
        raise HTTPException(
            status_code=409,
            detail={"code": "DOCUMENT_INDEX_NOT_FAILED", "message": "当前文档没有可重试的失败阶段"},
        )
    return ApiResponse(data=data)


@router.get("/{kb_id}/jobs", response_model=ApiResponse)
async def list_index_jobs(
    kb_id: uuid.UUID,
    status: str | None = None,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    await _require_kb(auth, db, kb_id)
    jobs = await index_job_service.list(
        db, tenant_id=auth.tenant.tenant_id, kb_id=kb_id, status=status
    )
    return ApiResponse(data=[job_to_dict(job) for job in jobs])


@router.post("/{kb_id}/jobs/{job_id}/cancel", response_model=ApiResponse)
async def cancel_index_job(
    kb_id: uuid.UUID,
    job_id: uuid.UUID,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    await _require_kb(auth, db, kb_id)
    job = await index_job_service.get(
        db, tenant_id=auth.tenant.tenant_id, kb_id=kb_id, job_id=job_id
    )
    if job is None:
        raise HTTPException(status_code=404, detail="index job not found")
    if not await index_job_service.cancel(db, job):
        raise HTTPException(
            status_code=409,
            detail={"code": "INDEX_JOB_NOT_CANCELLABLE", "message": "只有排队中的任务可以取消"},
        )
    return ApiResponse(data=job_to_dict(job))


@router.delete("/{kb_id}/documents/{doc_id}", response_model=ApiResponse)
async def delete_document(
    kb_id: uuid.UUID,
    doc_id: int,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """删除单个文档（文件 + 分块 + 元数据）。"""
    kb = await _require_kb(auth, db, kb_id)
    ok = await knowledge_service.delete_document(
        db, tenant_id=auth.tenant.tenant_id, kb_id=kb_id, doc_id=doc_id
    )
    if not ok:
        raise HTTPException(status_code=404, detail="document not found")
    await audit.audit(
        auth.tenant.tenant_id,
        "knowledge.delete",
        user_id=auth.user_id,
        detail={"kb": kb.name, "doc_id": doc_id},
    )
    return ApiResponse(data={"kb_id": str(kb_id), "doc_id": doc_id})


# ---------- 分块治理（KB_MANAGEMENT_SPEC §3.5） ----------


@router.get("/{kb_id}/chunks", response_model=ApiResponse)
async def list_chunks(
    kb_id: uuid.UUID,
    doc_id: int | None = None,
    q: str | None = None,
    enabled: bool | None = None,
    page: int = 1,
    page_size: int = 20,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """分块列表（预览文本、分页、文档/关键词/启停过滤）。"""
    await _require_kb(auth, db, kb_id)
    data = await knowledge_service.list_chunks(
        db,
        tenant_id=auth.tenant.tenant_id,
        kb_id=kb_id,
        doc_id=doc_id,
        q=q,
        enabled=enabled,
        page=page,
        page_size=page_size,
    )
    return ApiResponse(
        data=ChunkListResponse(
            items=[_to_chunk_resp(c) for c in data["items"]],
            total=data["total"],
            page=data["page"],
            page_size=data["page_size"],
        )
    )


@router.post("/{kb_id}/documents/{doc_id}/preview", response_model=ApiResponse)
async def preview_document_chunks(
    kb_id: uuid.UUID,
    doc_id: int,
    body: ChunkPreviewRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """分块预览：对文档原始文本按 chunk_size 预演分块（不落库）。"""
    await _require_kb(auth, db, kb_id)
    doc = await knowledge_service.get_document(
        db, tenant_id=auth.tenant.tenant_id, kb_id=kb_id, doc_id=doc_id
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    previews = knowledge_service.preview_document(text=body.text, chunk_size=body.chunk_size)
    return ApiResponse(
        data=[ChunkPreviewItem(**p) for p in previews]
    )


@router.put("/{kb_id}/chunks/{chunk_id}", response_model=ApiResponse)
async def update_chunk(
    kb_id: uuid.UUID,
    chunk_id: int,
    body: ChunkUpdateRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """手动编辑分块（text 变更自动重新向量化）+ 启停。"""
    await _require_kb(auth, db, kb_id)
    try:
        chunk = await knowledge_service.update_chunk(
            db,
            tenant_id=auth.tenant.tenant_id,
            kb_id=kb_id,
            chunk_id=chunk_id,
            text=body.text,
            enabled=body.enabled,
        )
    except IngestError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if chunk is None:
        raise HTTPException(status_code=404, detail="chunk not found")
    await audit.audit(
        auth.tenant.tenant_id,
        "knowledge.chunk.edit",
        user_id=auth.user_id,
        detail={"kb": str(kb_id), "chunk_id": chunk_id},
    )
    return ApiResponse(data=_to_chunk_resp(knowledge_service._chunk_to_dict(chunk)))


@router.patch("/{kb_id}/chunks/{chunk_id}/enabled", response_model=ApiResponse)
async def toggle_chunk_enabled(
    kb_id: uuid.UUID,
    chunk_id: int,
    body: ChunkEnabledRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """启用 / 停用分块（不重算向量）。"""
    await _require_kb(auth, db, kb_id)
    chunk = await knowledge_service.toggle_chunk(
        db,
        tenant_id=auth.tenant.tenant_id,
        kb_id=kb_id,
        chunk_id=chunk_id,
        enabled=body.enabled,
    )
    if chunk is None:
        raise HTTPException(status_code=404, detail="chunk not found")
    await audit.audit(
        auth.tenant.tenant_id,
        "knowledge.chunk.toggle",
        user_id=auth.user_id,
        detail={"kb": str(kb_id), "chunk_id": chunk_id, "enabled": body.enabled},
    )
    return ApiResponse(data=_to_chunk_resp(knowledge_service._chunk_to_dict(chunk)))


@router.post("/{kb_id}/chunks/{chunk_id}/re-embed", response_model=ApiResponse)
async def reembed_chunk(
    kb_id: uuid.UUID,
    chunk_id: int,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """重新向量化该分块。"""
    await _require_kb(auth, db, kb_id)
    try:
        chunk = await knowledge_service.reembed_chunk(
            db,
            tenant_id=auth.tenant.tenant_id,
            kb_id=kb_id,
            chunk_id=chunk_id,
        )
    except IngestError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if chunk is None:
        raise HTTPException(status_code=404, detail="chunk not found")
    await audit.audit(
        auth.tenant.tenant_id,
        "knowledge.chunk.reembed",
        user_id=auth.user_id,
        detail={"kb": str(kb_id), "chunk_id": chunk_id},
    )
    return ApiResponse(data=_to_chunk_resp(knowledge_service._chunk_to_dict(chunk)))


# ---------- 检索 ----------


@router.post("/{kb_id}/search", response_model=ApiResponse)
async def search_knowledge_base(
    kb_id: uuid.UUID,
    body: KnowledgeSearchRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """检索调试：在单个知识库内检索，返回 top-k 片段。"""
    kb = await _require_kb(auth, db, kb_id)
    try:
        hits = await knowledge_service.search(
            db,
            tenant_id=auth.tenant.tenant_id,
            kb_id=kb_id,
            query=body.query,
            top_k=body.top_k,
        )
    except EmbeddingError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    await audit.audit(
        auth.tenant.tenant_id,
        "knowledge.search",
        user_id=auth.user_id,
        detail={"kb": kb.name, "hits": len(hits)},
    )
    return ApiResponse(data=[_hit_resp(h) for h in hits])


@router.post("/{kb_id}/test-search", response_model=ApiResponse)
async def test_search_knowledge_base(
    kb_id: uuid.UUID,
    body: TestSearchRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """测试检索（跨库检索调试）：返回命中 + 来源 + 溯源主键 + 策略标注。

    AGENTIC_RAG_UPGRADE_SPEC §3.8：body 增加 strategy / rerank / expand_query
    （默认值 auto/True/False 兼容旧前端）。
    """
    kb = await _require_kb(auth, db, kb_id)
    if body.mode in {"graph", "fused"} and kb.index_mode == "standard":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "INDEX_MODE_CONFLICT",
                "message": "标准知识库仅支持文本检索，可在设置中升级索引模式",
            },
        )
    try:
        if body.mode is not None:
            result = await knowledge_service.retrieve_unified(
                db,
                tenant_id=auth.tenant.tenant_id,
                query=body.query,
                kb_ids=[kb_id],
                mode=body.mode,
                top_k=body.top_k,
                rerank=body.rerank,
                expand_query=body.expand_query,
                graph_hop=body.graph_hop,
            )
            await audit.audit(
                auth.tenant.tenant_id,
                "knowledge.retrieve",
                user_id=auth.user_id,
                detail={
                    "kb_id": str(kb_id),
                    "mode": body.mode,
                    "resolved_mode": result["resolved_mode"],
                    "degradations": result["degradations"],
                },
            )
            return ApiResponse(data=result)
        hits = await knowledge_service.retrieve_v2(
            db,
            tenant_id=auth.tenant.tenant_id,
            query=body.query,
            kb_ids=[kb_id],
            top_k=body.top_k,
            strategy=body.strategy,
            rerank=body.rerank,
            expand_query=body.expand_query,
        )
    except EmbeddingError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    await audit.audit(
        auth.tenant.tenant_id,
        "knowledge.test_search",
        user_id=auth.user_id,
        detail={
            "kb": kb.name,
            "hits": len(hits),
            "strategy": body.strategy,
            "rerank": body.rerank,
        },
    )
    return ApiResponse(data=[_hit_resp(h) for h in hits])


# ---------- 检索评测（AGENTIC_RAG_UPGRADE_SPEC §3.8） ----------


def _eval_run_resp(run: EvaluationRun) -> dict:
    return {
        "evaluation_id": run.id,
        "name": run.name,
        "status": run.status,
        "variants": run.variants,
        "results": run.results,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


@router.post("/{kb_id}/evaluate", response_model=ApiResponse)
async def evaluate_knowledge_base(
    kb_id: uuid.UUID,
    body: EvaluateRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """触发检索评测：对 dataset 逐用例跑 variants 检索，计算 hit_rate / mrr。

    judge=True 时追加 LLM-as-judge 端到端评分（faithfulness / relevancy），写 TraceScore。
    """
    kb = await _require_kb(auth, db, kb_id)
    try:
        run = await knowledge_service.evaluate(
            db,
            tenant_id=auth.tenant.tenant_id,
            kb_id=kb_id,
            name=body.name,
            dataset=body.dataset,
            variants=body.variants,
            judge=body.judge,
        )
    except EmbeddingError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if run is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    await audit.audit(
        auth.tenant.tenant_id,
        "knowledge.evaluate",
        user_id=auth.user_id,
        detail={
            "kb": kb.name,
            "run_id": run.id,
            "cases": len(body.dataset),
            "variants": body.variants,
            "judge": body.judge,
        },
    )
    return ApiResponse(data=_eval_run_resp(run))


@router.get("/{kb_id}/evaluations", response_model=ApiResponse)
async def list_evaluations(
    kb_id: uuid.UUID,
    limit: int = 20,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """列出该知识库的评测记录（复用 EvaluationRun，按 dataset.kb_id 过滤）。"""
    await _require_kb(auth, db, kb_id)
    result = await db.execute(
        select(EvaluationRun)
        .where(EvaluationRun.tenant_id == auth.tenant.tenant_id)
        .order_by(EvaluationRun.created_at.desc())
        .limit(max(1, min(limit, 50)))
    )
    runs = [
        row
        for row in result.scalars().all()
        if (row.dataset or {}).get("kb_id") == str(kb_id)
    ]
    return ApiResponse(data=[_eval_run_resp(run) for run in runs])


def _hit_resp(hit) -> KnowledgeHit:
    return KnowledgeHit(
        text=hit.text,
        source_file=hit.source_file,
        chunk_index=hit.chunk_index,
        score=hit.score,
        kb_id=str(hit.kb_id),
        chunk_id=hit.chunk_id,
        strategy=hit.strategy,
        rerank_score=hit.rerank_score,
    )
