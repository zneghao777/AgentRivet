"""长期记忆管理路由（MEMORY_SPEC §7）。

遵循 router_skills.py 风格：ApiResponse 包装、require_auth 鉴权、audit 审计。
🔒 安全红线（用户级）：所有 /memory 接口操作对象为「当前 AuthContext 对应用户」的记忆，
tenant_id 与 user_id 均取自鉴权上下文，不接受客户端传参覆盖；越权返回 404。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.service import MemoryError, memory_service
from ..observability import audit
from ..storage.db import get_db
from .deps import AuthContext, require_auth
from .schemas import (
    ApiResponse,
    MemoryGraphResponse,
    MemoryItem,
    MemoryListResponse,
    MemoryOverview,
    MemorySettings,
    MemorySettingsUpdateRequest,
    MemoryTimelineGroup,
    MemoryTimelineResponse,
    MemoryUpdateRequest,
)

router = APIRouter(prefix="/memory", tags=["memory"])


def _to_memory_item(entry: dict) -> MemoryItem:
    return MemoryItem(
        memory_id=entry["memory_id"],
        memory_type=entry["memory_type"],
        content=entry["content"],
        status=entry["status"],
        confidence=entry["confidence"],
        source_session_id=entry.get("source_session_id"),
        source_summary=entry.get("source_summary"),
        access_count=entry.get("access_count", 0),
        last_accessed_at=entry.get("last_accessed_at"),
        created_at=entry.get("created_at"),
        updated_at=entry.get("updated_at"),
    )


# ---------- 记忆条目 ----------


@router.get("", response_model=ApiResponse)
async def list_memories(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    memory_type: str | None = None,
    status: str | None = None,
    q: str | None = None,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """记忆列表（当前用户）：类型 / 状态 / 关键词过滤 + 分页。"""
    data = await memory_service.list_entries(
        db,
        tenant_id=auth.tenant.tenant_id,
        user_id=auth.user_id,
        memory_type=memory_type,
        status=status,
        q=q,
        page=page,
        page_size=page_size,
    )
    return ApiResponse(
        data=MemoryListResponse(
            items=[_to_memory_item(c) for c in data["items"]],
            total=data["total"],
            page=data["page"],
            page_size=data["page_size"],
        )
    )


@router.get("/timeline", response_model=ApiResponse)
async def memory_timeline(
    days: int = Query(30, ge=1, le=365),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """按天分组的记忆时间线（当前用户）。"""
    groups = await memory_service.timeline(
        db, tenant_id=auth.tenant.tenant_id, user_id=auth.user_id, days=days
    )
    return ApiResponse(
        data=MemoryTimelineResponse(
            groups=[MemoryTimelineGroup(**g) for g in groups]
        )
    )


@router.get("/overview", response_model=ApiResponse)
async def memory_overview(
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """记忆统计（当前用户）：总数 / 类型 / 状态 / 近7日新增 / 命中 top5。"""
    data = await memory_service.overview(
        db, tenant_id=auth.tenant.tenant_id, user_id=auth.user_id
    )
    return ApiResponse(
        data=MemoryOverview(
            total=data["total"],
            semantic_count=data["semantic_count"],
            episodic_count=data["episodic_count"],
            active_count=data["active_count"],
            pending_count=data["pending_count"],
            disabled_count=data["disabled_count"],
            added_7d=data["added_7d"],
            top_accessed=[_to_memory_item(c) for c in data["top_accessed"]],
        )
    )


@router.get("/graph", response_model=ApiResponse)
async def memory_graph(
    memory_type: str | None = None,
    status: str | None = "active",
    q: str | None = None,
    limit: int = Query(80, ge=10, le=120),
    similarity_threshold: float = Query(0.72, ge=0.5, le=0.95),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """当前用户的记忆关系图谱：同源会话 + 向量语义相似关系。"""
    data = await memory_service.graph(
        db,
        tenant_id=auth.tenant.tenant_id,
        user_id=auth.user_id,
        memory_type=memory_type,
        status=status,
        q=q,
        limit=limit,
        similarity_threshold=similarity_threshold,
    )
    return ApiResponse(data=MemoryGraphResponse(**data))


# 注意：/settings 固定路径必须声明在 /{memory_id} 之前，否则会被路径参数吞掉。


@router.get("/settings", response_model=ApiResponse)
async def get_memory_settings(
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """读取当前用户的长期记忆开关 / TTL。"""
    data = await memory_service.get_settings(
        db, tenant_id=auth.tenant.tenant_id, user_id=auth.user_id
    )
    return ApiResponse(data=MemorySettings(**data))


@router.patch("/settings", response_model=ApiResponse)
async def update_memory_settings(
    body: MemorySettingsUpdateRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """更新当前用户的长期记忆设置。"""
    data = await memory_service.update_settings(
        db,
        tenant_id=auth.tenant.tenant_id,
        user_id=auth.user_id,
        long_term_enabled=body.long_term_enabled,
        auto_extract=body.auto_extract,
        ttl_days=body.ttl_days,
    )
    await audit.audit(
        auth.tenant.tenant_id,
        "memory.settings",
        user_id=auth.user_id,
        detail={k: v for k, v in body.model_dump().items() if v is not None},
    )
    return ApiResponse(data=MemorySettings(**data))


@router.patch("/{memory_id}", response_model=ApiResponse)
async def update_memory(
    memory_id: int,
    body: MemoryUpdateRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """编辑记忆内容（自动重新向量化）/ 改状态（active/disabled/pending）。"""
    try:
        entry = await memory_service.update_entry(
            db,
            tenant_id=auth.tenant.tenant_id,
            user_id=auth.user_id,
            memory_id=memory_id,
            content=body.content,
            status=body.status,
        )
    except MemoryError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if entry is None:
        raise HTTPException(status_code=404, detail="memory not found")
    await audit.audit(
        auth.tenant.tenant_id,
        "memory.update",
        user_id=auth.user_id,
        detail={"memory_id": memory_id, "status": entry.status},
    )
    return ApiResponse(data=_to_memory_item(memory_service._entry_to_dict(entry)))


@router.delete("/{memory_id}", response_model=ApiResponse)
async def delete_memory(
    memory_id: int,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """单条记忆物理删除（行 + 向量，forget 语义即时生效）。"""
    ok = await memory_service.delete_entry(
        db, tenant_id=auth.tenant.tenant_id, user_id=auth.user_id, memory_id=memory_id
    )
    if not ok:
        raise HTTPException(status_code=404, detail="memory not found")
    await audit.audit(
        auth.tenant.tenant_id,
        "memory.delete",
        user_id=auth.user_id,
        detail={"memory_id": memory_id},
    )
    return ApiResponse(data={"id": memory_id})


@router.delete("", response_model=ApiResponse)
async def clear_memories(
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """清空当前用户全部记忆（物理删除，前端二次确认）。"""
    deleted = await memory_service.clear_entries(
        db, tenant_id=auth.tenant.tenant_id, user_id=auth.user_id
    )
    await audit.audit(
        auth.tenant.tenant_id,
        "memory.clear",
        user_id=auth.user_id,
        detail={"deleted": deleted},
    )
    return ApiResponse(data={"deleted": deleted})
