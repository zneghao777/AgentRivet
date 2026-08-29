"""管理路由：健康检查、审计查询、指标。"""

from __future__ import annotations

import time
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..agents.model_endpoint import ModelEndpointError, validate_model_base_url
from ..agents.resolver import resolve_orchestration_agents
from ..config import settings
from ..harness.planner import create_plan
from ..harness.router import decide_route
from ..observability.metrics import metrics
from ..storage import repositories as repo
from ..storage.db import get_db
from .deps import AuthContext, require_auth
from .schemas import (
    ApiResponse,
    AuditResponse,
    ModelConnectionTestRequest,
    RouteDryRunRequest,
)

router = APIRouter(tags=["admin"])


def _allowed_model_base_url(base_url: str) -> str:
    try:
        return validate_model_base_url(base_url)
    except ModelEndpointError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _call_model_connection(base_url: str, model: str, api_key: str) -> dict:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json={
                "model": model,
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "temperature": 0,
                "max_tokens": 1,
                "stream": False,
            },
        )
    if response.is_error:
        detail = response.text.replace(api_key, "***")[:300] if api_key else response.text[:300]
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
    """使用请求级凭据或后端默认凭据验证指定模型。"""
    custom_credential_supplied = "api_key" in body.model_fields_set
    if body.api_key is not None:
        provider_api_key = body.api_key.get_secret_value()
    elif custom_credential_supplied:
        provider_api_key = ""
    else:
        provider_api_key = settings.LLM_API_KEY
    if not provider_api_key and not custom_credential_supplied:
        raise HTTPException(status_code=503, detail="后端尚未配置 LLM_API_KEY")
    base_url = _allowed_model_base_url(body.base_url or settings.LLM_BASE_URL)
    started = time.monotonic()
    result = await _call_model_connection(base_url, body.model, provider_api_key)
    return ApiResponse(
        data={
            "connected": True,
            "model": body.model,
            "resolved_model": result["resolved_model"],
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    )


@router.post("/admin/agents/{agent_id}/route-dry-run", response_model=ApiResponse)
async def route_dry_run(
    agent_id: uuid.UUID,
    body: RouteDryRunRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """只执行 Router（以及编排模式的 Plan 预览），不执行任何 Worker。"""
    row = await repo.agent_repository.get(
        db, tenant_id=auth.tenant.tenant_id, agent_id=agent_id
    )
    if row is None or row.status == "archived":
        raise HTTPException(status_code=404, detail="Agent 不存在")
    agents = await resolve_orchestration_agents(
        db, tenant_id=auth.tenant.tenant_id
    )
    decision = await decide_route(
        body.message,
        tenant_id=auth.tenant.tenant_id,
        agents=agents,
    )
    data = decision.model_dump()
    if decision.mode == "orchestrate" and not decision.needs_clarification:
        plan = await create_plan(body.message, agents=agents)
        data["plan"] = plan.model_dump() if plan else None
    return ApiResponse(data=data)


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
