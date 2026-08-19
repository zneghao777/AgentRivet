"""管理路由：健康检查、审计查询、指标。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..observability.metrics import metrics
from ..storage import repositories as repo
from ..storage.db import get_db
from .deps import AuthContext, require_auth
from .schemas import ApiResponse, AuditResponse

router = APIRouter(tags=["admin"])


@router.get("/health")
async def health():
    """健康检查（不鉴权）。"""
    return ApiResponse(data={"status": "ok", "app": settings.APP_NAME})


@router.get("/health/ready")
async def health_ready(db: AsyncSession = Depends(get_db)):
    """就绪检查（DB 连接）。"""
    try:
        await db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        return ApiResponse(code=500, message="db not ready", data={"ready": False})
    return ApiResponse(data={"ready": True, "model_configured": bool(settings.LLM_API_KEY)})


@router.get("/audit")
async def list_audit(
    tenant_id: str | None = Query(None),
    action: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    logs, total = await repo.query_audit(
        db, tenant_id=tenant_id, action=action, page=page, page_size=page_size
    )
    items = [
        AuditResponse(
            id=l.id,
            tenant_id=l.tenant_id,
            session_id=str(l.session_id) if l.session_id else None,
            user_id=l.user_id,
            action=l.action,
            detail=l.detail,
            ip=l.ip,
            created_at=l.created_at.isoformat() if l.created_at else None,
        )
        for l in logs
    ]
    return ApiResponse(data={"items": items, "total": total, "page": page, "page_size": page_size})


@router.get("/metrics")
async def get_metrics(
    group_by: str | None = Query(None),
    auth: AuthContext = Depends(require_auth),
):
    return ApiResponse(data=metrics.summary(group_by=group_by))
