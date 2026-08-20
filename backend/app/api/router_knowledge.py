"""知识库（RAG）管理路由：CRUD / 文档解析任务 / 分块治理 / 测试检索。

遵循 router_skills.py 风格：ApiResponse 包装、require_auth 鉴权、audit 审计。
🔒 安全红线：所有操作校验 kb.tenant_id == auth.tenant.tenant_id，越权返回 404（SPEC §3.8）。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from ..knowledge.embedder import EmbeddingError
from ..knowledge.ingest import IngestError
from ..knowledge.service import knowledge_service
from ..observability import audit
from ..storage.db import get_db
from .deps import AuthContext, require_auth
from .schemas import (
    ApiResponse,
    ChunkEnabledRequest,
    ChunkListResponse,
    ChunkPreviewItem,
    ChunkPreviewRequest,
    ChunkUpdateRequest,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseResponse,
    KnowledgeBaseStatusRequest,
    KnowledgeChunkItem,
    KnowledgeDocResponse,
    KnowledgeHit,
    KnowledgeSearchRequest,
    KnowledgeUploadResponse,
    TestSearchRequest,
)

router = APIRouter(prefix="/knowledge-bases", tags=["knowledge"])


def _to_kb_resp(kb) -> KnowledgeBaseResponse:
    return KnowledgeBaseResponse(
        kb_id=str(kb.id),
        name=kb.name,
        description=kb.description,
        doc_count=kb.doc_count,
        chunk_count=kb.chunk_count,
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
        parse_source=doc.get("parse_source"),
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
    return ApiResponse(data=[_to_kb_resp(kb) for kb in items])


@router.post("", response_model=ApiResponse)
async def create_knowledge_base(
    body: KnowledgeBaseCreateRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """创建知识库。"""
    kb = await knowledge_service.create(
        db,
        tenant_id=auth.tenant.tenant_id,
        name=body.name,
        description=body.description,
    )
    if kb is None:
        raise HTTPException(status_code=409, detail="知识库名称已存在")
    await audit.audit(
        auth.tenant.tenant_id,
        "knowledge.create",
        user_id=auth.user_id,
        detail={"kb": kb.name},
    )
    return ApiResponse(data=_to_kb_resp(kb))


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
            replace=replace,
            mime_type=file.content_type,
        )
    except IngestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await audit.audit(
        auth.tenant.tenant_id,
        "knowledge.upload",
        user_id=auth.user_id,
        detail={"kb": str(kb_id), "file": file.filename, "doc_id": doc.id},
    )
    return ApiResponse(
        data=KnowledgeUploadResponse(
            kb_id=str(kb_id),
            doc_id=doc.id,
            source_file=doc.source_file,
            parse_status=doc.parse_status,
            chunk_count=0,
        )
    )


@router.post("/{kb_id}/documents/upload", response_model=ApiResponse)
async def upload_document(
    kb_id: uuid.UUID,
    file: UploadFile = File(...),
    replace: bool = Form(True),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """上传文档并触发异步解析（multipart）。返回 pending 状态，解析后台进行。"""
    return await _do_upload(kb_id, file, replace, auth, db)


@router.post("/{kb_id}/documents", response_model=ApiResponse)
async def upload_document_legacy(
    kb_id: uuid.UUID,
    file: UploadFile = File(...),
    replace: bool = Form(True),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """兼容旧路径 /documents：等价于 /documents/upload。"""
    return await _do_upload(kb_id, file, replace, auth, db)


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
    """测试检索（跨库检索调试）：返回命中 + 来源 + 溯源主键。"""
    await _require_kb(auth, db, kb_id)
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
        "knowledge.test_search",
        user_id=auth.user_id,
        detail={"kb": str(kb_id), "hits": len(hits)},
    )
    return ApiResponse(data=[_hit_resp(h) for h in hits])


def _hit_resp(hit) -> KnowledgeHit:
    return KnowledgeHit(
        text=hit.text,
        source_file=hit.source_file,
        chunk_index=hit.chunk_index,
        score=hit.score,
        kb_id=str(hit.kb_id),
        chunk_id=hit.chunk_id,
    )
