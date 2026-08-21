"""知识图谱（Graph RAG）管理 / 查询路由（GRAPH_RAG_SPEC §6）。

- 图谱管理：build / rebuild / status / entities CRUD / merge
- 图谱查询：search / timeline
遵循 router_knowledge.py 风格：ApiResponse 包装、require_auth 鉴权、audit 审计。
🔒 安全红线：所有操作校验 kb 归属当前租户，越权 404（SPEC §3.8）。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..knowledge.graph.service import graph_service
from ..knowledge.policy import IndexPolicyError
from ..observability import audit
from ..storage.db import get_db
from .deps import AuthContext, require_auth
from .schemas import ApiResponse

router = APIRouter(tags=["knowledge-graph"])


# ---------- 请求模型 ----------


class GraphSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    strategy: str = Field(default="auto")  # auto|entity|hop|global|timeline
    hop: int = Field(default=2, ge=1, le=3)


class EntityUpdateRequest(BaseModel):
    name: str | None = None
    canonical_name: str | None = None
    entity_type: str | None = None
    description: str | None = None


class EntityMergeRequest(BaseModel):
    keep_id: uuid.UUID
    remove_id: uuid.UUID


async def _require_graph_kb(auth: AuthContext, db: AsyncSession, kb_id: uuid.UUID):
    """校验知识库归属当前租户，否则 404。"""
    from ..knowledge.service import knowledge_service

    kb = await knowledge_service.get(db, tenant_id=auth.tenant.tenant_id, kb_id=kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    return kb


# ---------- 图谱管理（GRAPH_RAG_SPEC §6.1） ----------


@router.post("/{kb_id}/graph/build", response_model=ApiResponse)
@router.post("/{kb_id}/graph/build-pending", response_model=ApiResponse)
async def trigger_graph_build(
    kb_id: uuid.UUID,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """触发图谱构建（异步任务）。"""
    await _require_graph_kb(auth, db, kb_id)
    try:
        data = await graph_service.trigger_build(
            db, tenant_id=auth.tenant.tenant_id, kb_id=kb_id
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    except IndexPolicyError as exc:
        raise HTTPException(
            status_code=503 if exc.code == "GRAPH_PLATFORM_DISABLED" else 409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    await audit.audit(
        auth.tenant.tenant_id, "knowledge.graph.build",
        user_id=auth.user_id, detail={"kb": str(kb_id)},
    )
    return ApiResponse(data=data)


@router.post("/{kb_id}/graph/rebuild", response_model=ApiResponse)
async def trigger_graph_rebuild(
    kb_id: uuid.UUID,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """全量重建图谱（异步任务）。"""
    await _require_graph_kb(auth, db, kb_id)
    try:
        data = await graph_service.trigger_rebuild(
            db, tenant_id=auth.tenant.tenant_id, kb_id=kb_id
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    except IndexPolicyError as exc:
        raise HTTPException(
            status_code=503 if exc.code == "GRAPH_PLATFORM_DISABLED" else 409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    await audit.audit(
        auth.tenant.tenant_id, "knowledge.graph.rebuild",
        user_id=auth.user_id, detail={"kb": str(kb_id)},
    )
    return ApiResponse(data=data)


@router.get("/{kb_id}/graph/status", response_model=ApiResponse)
async def graph_status(
    kb_id: uuid.UUID,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """查询图谱构建进度/状态。"""
    await _require_graph_kb(auth, db, kb_id)
    try:
        data = await graph_service.status(
            db, tenant_id=auth.tenant.tenant_id, kb_id=kb_id
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    return ApiResponse(data=data)


@router.get("/{kb_id}/graph/entities", response_model=ApiResponse)
async def list_graph_entities(
    kb_id: uuid.UUID,
    type: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """查询实体列表（支持 type / q 过滤 + 分页）。"""
    await _require_graph_kb(auth, db, kb_id)
    data = await graph_service.list_entities(
        db, tenant_id=auth.tenant.tenant_id, kb_id=kb_id,
        entity_type=type, q=q, page=page, page_size=min(page_size, 200),
    )
    return ApiResponse(data=data)


@router.get("/{kb_id}/graph/relations", response_model=ApiResponse)
async def list_graph_relations(
    kb_id: uuid.UUID,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """分页查询知识库中的实体关系。"""
    await _require_graph_kb(auth, db, kb_id)
    data = await graph_service.list_relations(
        db,
        tenant_id=auth.tenant.tenant_id,
        kb_id=kb_id,
        q=q,
        page=max(1, page),
        page_size=max(1, min(page_size, 200)),
    )
    return ApiResponse(data=data)


@router.get("/{kb_id}/graph/events", response_model=ApiResponse)
async def list_graph_events(
    kb_id: uuid.UUID,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """分页查询知识库中的事件时间线。"""
    await _require_graph_kb(auth, db, kb_id)
    data = await graph_service.list_events(
        db,
        tenant_id=auth.tenant.tenant_id,
        kb_id=kb_id,
        q=q,
        page=max(1, page),
        page_size=max(1, min(page_size, 200)),
    )
    return ApiResponse(data=data)


@router.get("/{kb_id}/graph/subgraph", response_model=ApiResponse)
async def graph_subgraph(
    kb_id: uuid.UUID,
    entity_id: uuid.UUID,
    hop: int = 1,
    limit: int = 100,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    await _require_graph_kb(auth, db, kb_id)
    try:
        data = await graph_service.subgraph(
            db,
            tenant_id=auth.tenant.tenant_id,
            kb_id=kb_id,
            entity_id=entity_id,
            hop=max(1, min(hop, 3)),
            limit=max(1, min(limit, 300)),
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="entity not found")
    return ApiResponse(data=data)


@router.delete("/{kb_id}/graph", response_model=ApiResponse)
async def clear_graph_data(
    kb_id: uuid.UUID,
    confirm: bool = False,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    await _require_graph_kb(auth, db, kb_id)
    if not confirm:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DESTRUCTIVE_CONFIRMATION_REQUIRED",
                "message": "删除图谱数据需要显式确认",
            },
        )
    data = await graph_service.clear_graph(
        db, tenant_id=auth.tenant.tenant_id, kb_id=kb_id
    )
    await audit.audit(
        auth.tenant.tenant_id,
        "knowledge.graph_cleanup",
        user_id=auth.user_id,
        detail={"kb_id": str(kb_id)},
    )
    return ApiResponse(data=data)


@router.get("/{kb_id}/graph/entities/{entity_id}", response_model=ApiResponse)
async def graph_entity_detail(
    kb_id: uuid.UUID,
    entity_id: uuid.UUID,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """实体详情 + 邻居 + 关系。"""
    await _require_graph_kb(auth, db, kb_id)
    data = await graph_service.entity_detail(
        db, tenant_id=auth.tenant.tenant_id, kb_id=kb_id, entity_id=entity_id
    )
    if data is None:
        raise HTTPException(status_code=404, detail="entity not found")
    return ApiResponse(data=data)


@router.put("/{kb_id}/graph/entities/{entity_id}", response_model=ApiResponse)
async def update_graph_entity(
    kb_id: uuid.UUID,
    entity_id: uuid.UUID,
    body: EntityUpdateRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """修正 / 合并实体。"""
    await _require_graph_kb(auth, db, kb_id)
    data = await graph_service.update_entity(
        db, tenant_id=auth.tenant.tenant_id, kb_id=kb_id, entity_id=entity_id,
        name=body.name, canonical_name=body.canonical_name,
        entity_type=body.entity_type, description=body.description,
    )
    if data is None:
        raise HTTPException(status_code=404, detail="entity not found")
    await audit.audit(
        auth.tenant.tenant_id, "knowledge.graph.entity.update",
        user_id=auth.user_id, detail={"kb": str(kb_id), "entity": str(entity_id)},
    )
    return ApiResponse(data=data)


@router.post("/{kb_id}/graph/entities/{entity_id}/merge", response_model=ApiResponse)
async def merge_graph_entity(
    kb_id: uuid.UUID,
    entity_id: uuid.UUID,
    body: EntityMergeRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """合并实体：将 remove 合并进 keep（级联更新关系/事件）。"""
    await _require_graph_kb(auth, db, kb_id)
    ok = await graph_service.merge_entities(
        db, tenant_id=auth.tenant.tenant_id, kb_id=kb_id,
        keep_id=entity_id, remove_id=body.remove_id,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="entity not found")
    await audit.audit(
        auth.tenant.tenant_id, "knowledge.graph.entity.merge",
        user_id=auth.user_id, detail={"kb": str(kb_id), "keep": str(entity_id), "remove": str(body.remove_id)},
    )
    return ApiResponse(data={"keep_id": str(entity_id), "remove_id": str(body.remove_id)})


@router.delete("/{kb_id}/graph/entities/{entity_id}", response_model=ApiResponse)
async def delete_graph_entity(
    kb_id: uuid.UUID,
    entity_id: uuid.UUID,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """删除实体（级联关系/事件）。"""
    await _require_graph_kb(auth, db, kb_id)
    ok = await graph_service.delete_entity(
        db, tenant_id=auth.tenant.tenant_id, kb_id=kb_id, entity_id=entity_id
    )
    if not ok:
        raise HTTPException(status_code=404, detail="entity not found")
    await audit.audit(
        auth.tenant.tenant_id, "knowledge.graph.entity.delete",
        user_id=auth.user_id, detail={"kb": str(kb_id), "entity": str(entity_id)},
    )
    return ApiResponse(data={"entity_id": str(entity_id)})


# ---------- 图谱查询（GRAPH_RAG_SPEC §6.2） ----------


@router.post("/{kb_id}/graph/search", response_model=ApiResponse)
async def graph_search(
    kb_id: uuid.UUID,
    body: GraphSearchRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """语义图检索 body: {query, strategy, hop}。"""
    kb = await _require_graph_kb(auth, db, kb_id)
    if kb.index_mode != "graph_enhanced":
        raise HTTPException(
            status_code=409,
            detail={"code": "INDEX_MODE_CONFLICT", "message": "标准知识库不支持图谱检索"},
        )
    if kb.graph_status not in {"ready", "partial"}:
        raise HTTPException(
            status_code=409,
            detail={"code": "GRAPH_NOT_READY", "message": "图谱尚未就绪"},
        )
    data = await graph_service.search(
        db, tenant_id=auth.tenant.tenant_id, kb_id=kb_id,
        query=body.query, strategy=body.strategy, hop=body.hop,
    )
    await audit.audit(
        auth.tenant.tenant_id, "knowledge.graph.search",
        user_id=auth.user_id,
        detail={"kb": str(kb_id), "strategy": body.strategy, "hop": body.hop},
    )
    return ApiResponse(data=data)


@router.get("/{kb_id}/graph/timeline/{entity_name}", response_model=ApiResponse)
async def graph_timeline(
    kb_id: uuid.UUID,
    entity_name: str,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """实体事件时间线。"""
    await _require_graph_kb(auth, db, kb_id)
    events = await graph_service.timeline(
        db, tenant_id=auth.tenant.tenant_id, kb_id=kb_id, entity_name=entity_name
    )
    return ApiResponse(
        data=[
            {
                "event_id": str(e.id),
                "order_no": e.order_no,
                "predicate": e.predicate,
                "object": e.object,
                "summary": e.summary,
                "event_type": e.event_type,
                "source_chunk_id": e.source_chunk_id,
            }
            for e in events
        ]
    )
