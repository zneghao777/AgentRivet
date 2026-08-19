"""知识库（RAG）管理路由：CRUD / 文档上传摄取 / 检索调试。

遵循 router_skills.py 风格：ApiResponse 包装、require_auth 鉴权、audit 审计。
🔒 安全红线：所有操作校验 kb.tenant_id == auth.tenant.tenant_id，越权返回 404（SPEC §3.8）。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from ..knowledge.embedding import EmbeddingError
from ..knowledge.ingest import IngestError
from ..knowledge.service import knowledge_service
from ..observability import audit
from ..storage.db import get_db
from .deps import AuthContext, require_auth
from .schemas import (
    ApiResponse,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseResponse,
    KnowledgeBaseStatusRequest,
    KnowledgeDocResponse,
    KnowledgeHit,
    KnowledgeSearchRequest,
    KnowledgeUploadResponse,
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
    """知识库详情 + 文档列表。"""
    kb = await _require_kb(auth, db, kb_id)
    docs = await knowledge_service.list_documents(
        db, tenant_id=auth.tenant.tenant_id, kb_id=kb_id
    )
    doc_items = [
        KnowledgeDocResponse(
            doc_id=doc["doc_id"],
            source_file=doc["source_file"],
            chunk_count=doc["chunk_count"],
            created_at=doc["created_at"],
        )
        for doc in docs
    ]
    return ApiResponse(
        data={"kb": _to_kb_resp(kb), "documents": doc_items}
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
    """删除知识库（级联删 chunks）。"""
    kb = await _require_kb(auth, db, kb_id)
    await knowledge_service.delete(db, tenant_id=auth.tenant.tenant_id, kb_id=kb_id)
    await audit.audit(
        auth.tenant.tenant_id,
        "knowledge.delete",
        user_id=auth.user_id,
        detail={"kb": kb.name},
    )
    return ApiResponse(data={"kb_id": str(kb_id)})


@router.post("/{kb_id}/documents", response_model=ApiResponse)
async def upload_document(
    kb_id: uuid.UUID,
    file: UploadFile = File(...),
    replace: bool = Form(True),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """上传文档并摄取（multipart）。replace=true 时同文件幂等重写。"""
    kb = await _require_kb(auth, db, kb_id)
    content = await file.read()
    try:
        doc_id, chunk_count = await knowledge_service.upload_document(
            db,
            tenant_id=auth.tenant.tenant_id,
            kb_id=kb_id,
            filename=file.filename or "unnamed",
            content=content,
            replace=replace,
        )
    except IngestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await audit.audit(
        auth.tenant.tenant_id,
        "knowledge.upload",
        user_id=auth.user_id,
        detail={"kb": kb.name, "file": file.filename, "chunks": chunk_count},
    )
    return ApiResponse(
        data=KnowledgeUploadResponse(
            kb_id=str(kb_id),
            doc_id=doc_id,
            source_file=file.filename or "",
            chunk_count=chunk_count,
        )
    )


@router.delete("/{kb_id}/documents/{doc_id}", response_model=ApiResponse)
async def delete_document(
    kb_id: uuid.UUID,
    doc_id: int,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """删除单个文档（重建索引：删除其全部 chunks）。"""
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
    return ApiResponse(
        data=[
            KnowledgeHit(
                text=hit.text,
                source_file=hit.source_file,
                chunk_index=hit.chunk_index,
                score=hit.score,
            )
            for hit in hits
        ]
    )
