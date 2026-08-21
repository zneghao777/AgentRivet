"""MCP Server 与能力快照管理 API。"""

from __future__ import annotations

import uuid
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..mcp.adapter import McpAdapterError, validate_endpoint
from ..mcp.manager import mcp_manager
from ..mcp.secrets import McpSecretError, encrypt_headers
from ..observability import audit
from ..storage.db import get_db
from ..storage.models import McpCapability, McpServer
from .deps import AuthContext, require_auth
from .schemas import (
    ApiResponse,
    McpCapabilityResponse,
    McpCapabilityUpdateRequest,
    McpServerCreateRequest,
    McpServerResponse,
    McpServerUpdateRequest,
)

router = APIRouter(prefix="/mcp", tags=["mcp"])
capability_router = APIRouter(prefix="/capabilities", tags=["capabilities"])


def _auth_headers(body) -> tuple[str, str | None]:
    if body.type == "none":
        return "none", None
    if body.type == "bearer":
        if not body.secret_value:
            raise HTTPException(status_code=422, detail="Bearer token 不能为空")
        return "bearer", encrypt_headers({"Authorization": f"Bearer {body.secret_value}"})
    headers = body.headers or {}
    forbidden = {"host", "origin", "x-forwarded-for", "x-forwarded-host"}
    if any(key.lower() in forbidden for key in headers):
        raise HTTPException(status_code=422, detail="认证 Header 包含禁止字段")
    if not headers:
        raise HTTPException(status_code=422, detail="自定义认证 Header 不能为空")
    return "headers", encrypt_headers(headers)


async def _server_response(db: AsyncSession, server: McpServer) -> McpServerResponse:
    counts = (
        await db.execute(
            select(
                func.count(McpCapability.id),
                func.count(McpCapability.id).filter(McpCapability.enabled.is_(True)),
                func.count(McpCapability.id).filter(McpCapability.review_status != "approved"),
            ).where(McpCapability.server_id == server.id, McpCapability.removed_at.is_(None))
        )
    ).one()
    parsed = urlsplit(server.endpoint_url)
    port = f":{parsed.port}" if parsed.port else ""
    endpoint = f"{parsed.scheme}://{parsed.hostname or ''}{port}{parsed.path}"
    return McpServerResponse(
        id=str(server.id),
        name=server.name,
        description=server.description,
        transport=server.transport,
        endpoint_url=endpoint,
        auth_type=server.auth_type,
        credential_configured=bool(server.secret_ciphertext),
        enabled=server.enabled,
        health_status=server.health_status,
        protocol_version=server.protocol_version,
        capability_revision=server.capability_revision,
        tool_count=counts[0] or 0,
        enabled_tool_count=counts[1] or 0,
        pending_tool_count=counts[2] or 0,
        last_checked_at=server.last_checked_at.isoformat() if server.last_checked_at else None,
        last_error_code=server.last_error_code,
        last_error_message=server.last_error_message,
        created_at=server.created_at.isoformat() if server.created_at else None,
        updated_at=server.updated_at.isoformat() if server.updated_at else None,
    )


def _capability_response(row: McpCapability) -> McpCapabilityResponse:
    return McpCapabilityResponse(
        id=str(row.id),
        server_id=str(row.server_id),
        kind=row.kind,
        remote_name=row.remote_name,
        effective_name=row.effective_name,
        title=row.title,
        description=row.description,
        input_schema=row.input_schema or {},
        annotations=row.annotations or {},
        enabled=row.enabled,
        risk_level=row.risk_level,
        approval_policy=row.approval_policy,
        review_status=row.review_status,
        removed_at=row.removed_at.isoformat() if row.removed_at else None,
    )


async def _require_server(db: AsyncSession, auth: AuthContext, server_id: uuid.UUID) -> McpServer:
    server = await mcp_manager.get_server(db, tenant_id=auth.tenant.tenant_id, server_id=server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="MCP Server 不存在")
    return server


async def _discover(db: AsyncSession, server: McpServer) -> list[McpCapability]:
    try:
        return await mcp_manager.discover(db, server)
    except McpAdapterError as exc:
        raise HTTPException(
            status_code=502, detail={"code": exc.code, "message": str(exc)}
        ) from exc


@router.get("/servers", response_model=ApiResponse)
async def list_servers(
    auth: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)
):
    rows = await mcp_manager.list_servers(db, tenant_id=auth.tenant.tenant_id)
    return ApiResponse(data=[await _server_response(db, row) for row in rows])


@router.post("/servers", response_model=ApiResponse)
async def create_server(
    body: McpServerCreateRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    try:
        validate_endpoint(body.endpoint_url)
        auth_type, ciphertext = _auth_headers(body.auth)
    except (McpAdapterError, McpSecretError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    server = McpServer(
        tenant_id=auth.tenant.tenant_id,
        name=body.name,
        description=body.description,
        transport=body.transport,
        endpoint_url=body.endpoint_url,
        auth_type=auth_type,
        secret_ciphertext=ciphertext,
        created_by=auth.user_id,
    )
    db.add(server)
    try:
        await db.commit()
        await db.refresh(server)
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="MCP Server 名称已存在") from exc
    await audit.audit(
        auth.tenant.tenant_id,
        "mcp.server.create",
        user_id=auth.user_id,
        detail={"server_id": str(server.id)},
    )
    return ApiResponse(data=await _server_response(db, server))


@router.get("/servers/{server_id}", response_model=ApiResponse)
async def get_server(
    server_id: uuid.UUID,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    return ApiResponse(data=await _server_response(db, await _require_server(db, auth, server_id)))


@router.patch("/servers/{server_id}", response_model=ApiResponse)
async def update_server(
    server_id: uuid.UUID,
    body: McpServerUpdateRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    server = await _require_server(db, auth, server_id)
    if body.name is not None:
        server.name = body.name
    if body.description is not None:
        server.description = body.description
    if body.endpoint_url is not None:
        try:
            validate_endpoint(body.endpoint_url)
        except McpAdapterError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        server.endpoint_url = body.endpoint_url
        server.enabled = False
        server.health_status = "unknown"
    if body.auth is not None:
        try:
            server.auth_type, server.secret_ciphertext = _auth_headers(body.auth)
        except McpSecretError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        server.enabled = False
        server.health_status = "unknown"
    await db.commit()
    await db.refresh(server)
    await audit.audit(
        auth.tenant.tenant_id,
        "mcp.server.update",
        user_id=auth.user_id,
        detail={"server_id": str(server.id)},
    )
    return ApiResponse(data=await _server_response(db, server))


@router.post("/servers/{server_id}/test", response_model=ApiResponse)
async def test_server(
    server_id: uuid.UUID,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    server = await _require_server(db, auth, server_id)
    rows = await _discover(db, server)
    await audit.audit(
        auth.tenant.tenant_id,
        "mcp.server.test",
        user_id=auth.user_id,
        detail={"server_id": str(server.id), "tools": len(rows)},
    )
    return ApiResponse(
        data={
            "server": await _server_response(db, server),
            "capabilities": [_capability_response(row) for row in rows],
        }
    )


@router.post("/servers/{server_id}/discover", response_model=ApiResponse)
async def discover_capabilities(
    server_id: uuid.UUID,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    server = await _require_server(db, auth, server_id)
    rows = await _discover(db, server)
    await audit.audit(
        auth.tenant.tenant_id,
        "mcp.server.discover",
        user_id=auth.user_id,
        detail={"server_id": str(server.id), "tools": len(rows)},
    )
    return ApiResponse(data=[_capability_response(row) for row in rows])


@router.post("/servers/{server_id}/{action}", response_model=ApiResponse)
async def toggle_server(
    server_id: uuid.UUID,
    action: str,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    if action not in {"enable", "disable"}:
        raise HTTPException(status_code=404, detail="not found")
    server = await _require_server(db, auth, server_id)
    if action == "enable" and server.health_status not in {"ready", "degraded"}:
        raise HTTPException(status_code=409, detail="请先测试连接并发现工具")
    server.enabled = action == "enable"
    await db.commit()
    await db.refresh(server)
    await audit.audit(
        auth.tenant.tenant_id,
        f"mcp.server.{action}",
        user_id=auth.user_id,
        detail={"server_id": str(server.id)},
    )
    return ApiResponse(data=await _server_response(db, server))


@router.delete("/servers/{server_id}", response_model=ApiResponse)
async def delete_server(
    server_id: uuid.UUID,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    if not await mcp_manager.delete_server(
        db, tenant_id=auth.tenant.tenant_id, server_id=server_id
    ):
        raise HTTPException(status_code=404, detail="MCP Server 不存在")
    await audit.audit(
        auth.tenant.tenant_id,
        "mcp.server.delete",
        user_id=auth.user_id,
        detail={"server_id": str(server_id)},
    )
    return ApiResponse(data={"id": str(server_id)})


@router.get("/servers/{server_id}/capabilities", response_model=ApiResponse)
async def list_capabilities(
    server_id: uuid.UUID,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    await _require_server(db, auth, server_id)
    rows = await mcp_manager.list_capabilities(
        db, tenant_id=auth.tenant.tenant_id, server_id=server_id
    )
    return ApiResponse(data=[_capability_response(row) for row in rows])


@router.patch("/servers/{server_id}/capabilities/{capability_id}", response_model=ApiResponse)
async def update_capability(
    server_id: uuid.UUID,
    capability_id: uuid.UUID,
    body: McpCapabilityUpdateRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    await _require_server(db, auth, server_id)
    row = (
        await db.execute(
            select(McpCapability).where(
                McpCapability.id == capability_id, McpCapability.server_id == server_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="MCP Tool 不存在")
    if body.enabled is not None:
        row.enabled = body.enabled
        if body.enabled:
            row.review_status = "approved"
    if body.approval_policy is not None:
        row.approval_policy = body.approval_policy
    await db.commit()
    await db.refresh(row)
    await audit.audit(
        auth.tenant.tenant_id,
        "mcp.tool.review",
        user_id=auth.user_id,
        detail={"server_id": str(server_id), "tool": row.remote_name, "enabled": row.enabled},
    )
    return ApiResponse(data=_capability_response(row))


@router.get("/summary", response_model=ApiResponse)
@capability_router.get("/summary", response_model=ApiResponse)
async def capability_summary(
    auth: AuthContext = Depends(require_auth), db: AsyncSession = Depends(get_db)
):
    servers = await mcp_manager.list_servers(db, tenant_id=auth.tenant.tenant_id)
    from ..skills.manager import skill_manager

    skills = skill_manager.all()
    return ApiResponse(
        data={
            "skills": {
                "total": len(skills),
                "enabled": sum(item.enabled for item in skills),
                "with_scripts": sum(item.has_scripts for item in skills),
            },
            "mcp_servers": {
                "total": len(servers),
                "ready": sum(item.health_status == "ready" for item in servers),
                "degraded": sum(item.health_status == "degraded" for item in servers),
            },
            "needs_attention": sum(item.health_status in {"error", "degraded"} for item in servers),
        }
    )
