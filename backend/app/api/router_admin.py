"""管理路由：健康检查、审计查询、指标。"""

from __future__ import annotations

import time
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..observability.metrics import metrics
from ..storage import repositories as repo
from ..storage.db import get_db
from .deps import AuthContext, require_auth
from .schemas import ApiResponse, AuditResponse, ModelConnectionTestRequest

router = APIRouter(tags=["admin"])


def _allowed_model_base_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    parsed = urlparse(normalized)
    configured_host = urlparse(settings.LLM_BASE_URL).hostname
    allowed_hosts = {
        host.strip().lower()
        for host in settings.LLM_ALLOWED_BASE_HOSTS.split(",")
        if host.strip()
    }
    if configured_host:
        allowed_hosts.add(configured_host.lower())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=422, detail="Base URL 必须是有效的 HTTP(S) 地址")
    if parsed.hostname.lower() not in allowed_hosts:
        raise HTTPException(status_code=422, detail="Base URL 主机不在允许列表中")
    return normalized


async def _call_model_connection(base_url: str, model: str) -> dict:
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "temperature": 0,
                "max_tokens": 1,
                "stream": False,
            },
        )
    if response.is_error:
        detail = response.text.replace(settings.LLM_API_KEY, "***")[:300]
        raise HTTPException(
            status_code=502,
            detail=f"模型服务返回 HTTP {response.status_code}: {detail}",
        )
    payload = response.json()
    return {"resolved_model": payload.get("model") or model}


@router.post("/admin/test-connection", response_model=ApiResponse)
async def test_model_connection(
    body: ModelConnectionTestRequest,
    auth: AuthContext = Depends(require_auth),
):
    """使用后端已配置的供应商密钥验证指定模型。"""
    if not settings.LLM_API_KEY:
        raise HTTPException(status_code=503, detail="后端尚未配置 LLM_API_KEY")
    base_url = _allowed_model_base_url(body.base_url or settings.LLM_BASE_URL)
    started = time.monotonic()
    result = await _call_model_connection(base_url, body.model)
    return ApiResponse(
        data={
            "connected": True,
            "model": body.model,
            "resolved_model": result["resolved_model"],
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    )


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
