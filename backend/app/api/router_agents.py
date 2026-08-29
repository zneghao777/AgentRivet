"""AgentHub 管理 API。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..agents.manager import AgentManagerError, agent_manager
from ..config import settings
from ..observability import audit
from ..storage import repositories as repo
from ..storage.db import get_db
from ..storage.models import (
    AgentKbBinding,
    AgentMcpBinding,
    AgentSkillBinding,
    AgentSpan,
    AgentToolBinding,
    KnowledgeBase,
    McpCapability,
    McpServer,
    Message,
    Session,
    Skill,
)
from ..tools.registry import tool_registry
from .deps import AuthContext, require_auth
from .schemas import (
    AgentBindingIdsRequest,
    AgentCreateRequest,
    AgentUpdateRequest,
    ApiResponse,
)

router = APIRouter(prefix="/agents", tags=["agents"])


def _ensure_enabled() -> None:
    if not settings.AGENT_HUB_ENABLED:
        raise HTTPException(status_code=404, detail="not found")


def _error_status(exc: AgentManagerError) -> int:
    return 404 if exc.code == "AGENT_NOT_FOUND" else 409


async def _require_agent(db: AsyncSession, auth: AuthContext, agent_id: uuid.UUID):
    _ensure_enabled()
    row = await repo.agent_repository.get(
        db, tenant_id=auth.tenant.tenant_id, agent_id=agent_id
    )
    if row is None or row.status == "archived":
        raise HTTPException(status_code=404, detail="Agent 不存在")
    return row


async def _binding_detail(db: AsyncSession, agent_id: uuid.UUID) -> dict:
    skills = list(
        (
            await db.execute(
                select(Skill)
                .join(AgentSkillBinding, AgentSkillBinding.skill_id == Skill.id)
                .where(AgentSkillBinding.agent_id == agent_id)
                .order_by(Skill.name.asc())
            )
        ).scalars().all()
    )
    tools = list(
        (
            await db.execute(
                select(AgentToolBinding.tool_name)
                .where(AgentToolBinding.agent_id == agent_id)
                .order_by(AgentToolBinding.tool_name.asc())
            )
        ).scalars().all()
    )
    mcp_rows = list(
        (
            await db.execute(
                select(McpCapability, McpServer)
                .join(AgentMcpBinding, AgentMcpBinding.capability_id == McpCapability.id)
                .join(McpServer, McpServer.id == McpCapability.server_id)
                .where(AgentMcpBinding.agent_id == agent_id)
                .order_by(McpServer.name.asc(), McpCapability.remote_name.asc())
            )
        ).all()
    )
    kbs = list(
        (
            await db.execute(
                select(KnowledgeBase)
                .join(AgentKbBinding, AgentKbBinding.kb_id == KnowledgeBase.id)
                .where(AgentKbBinding.agent_id == agent_id)
                .order_by(KnowledgeBase.name.asc())
            )
        ).scalars().all()
    )
    return {
        "skills": [
            {"id": item.id, "name": item.name, "enabled": item.enabled, "invalid": not item.enabled}
            for item in skills
        ],
        "tools": tools,
        "mcp_capabilities": [
            {
                "id": str(cap.id),
                "server_id": str(server.id),
                "server_name": server.name,
                "name": cap.remote_name,
                "effective_name": cap.effective_name,
                "enabled": cap.enabled,
                "invalid": not (
                    server.enabled
                    and server.health_status in {"ready", "degraded"}
                    and cap.enabled
                    and cap.review_status == "approved"
                    and cap.removed_at is None
                ),
            }
            for cap, server in mcp_rows
        ],
        "knowledge_bases": [
            {"id": str(item.id), "name": item.name, "enabled": item.status == "active", "invalid": item.status != "active"}
            for item in kbs
        ],
    }


async def _agent_data(db: AsyncSession, row, *, include_prompt: bool = False) -> dict:
    bindings = await _binding_detail(db, row.id)
    session_count = (
        await db.execute(select(func.count(Session.id)).where(Session.agent_id == row.id))
    ).scalar_one()
    last_used_at = (
        await db.execute(select(func.max(Message.created_at)).where(Message.agent_id == row.id))
    ).scalar_one_or_none()
    result = {
        "id": str(row.id),
        "name": row.name,
        "description": row.description,
        "model": row.model,
        "model_base_url": row.model_base_url,
        "memory_enabled": row.memory_enabled,
        "scope": row.scope,
        "status": row.status,
        "bindings": bindings,
        "binding_counts": {
            "skills": len(bindings["skills"]),
            "tools": len(bindings["tools"]),
            "mcp": len(bindings["mcp_capabilities"]),
            "kbs": len(bindings["knowledge_bases"]),
        },
        "session_count": session_count,
        "last_used_at": last_used_at.isoformat() if last_used_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if include_prompt:
        result["system_prompt"] = row.system_prompt
    return result


@router.get("", response_model=ApiResponse)
async def list_agents(
    query: str | None = None,
    status: str | None = Query(default=None, pattern="^(active|disabled)$"),
    scope: str | None = Query(default=None, pattern="^(user|internal)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    _ensure_enabled()
    rows, total = await repo.agent_repository.list(
        db,
        tenant_id=auth.tenant.tenant_id,
        query=query,
        status=status,
        scope=scope,
        page=page,
        page_size=page_size,
    )
    return ApiResponse(
        data={
            "items": [await _agent_data(db, row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    )


@router.get("/options", response_model=ApiResponse)
async def agent_options(
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """返回编辑器所需的能力目录，避免前端拼接跨域事实。"""
    _ensure_enabled()
    skills = list((await db.execute(select(Skill).order_by(Skill.name.asc()))).scalars().all())
    servers = list(
        (
            await db.execute(
                select(McpServer)
                .where(McpServer.tenant_id == auth.tenant.tenant_id)
                .order_by(McpServer.name.asc())
            )
        ).scalars().all()
    )
    mcp_groups = []
    for server in servers:
        capabilities = list(
            (
                await db.execute(
                    select(McpCapability)
                    .where(McpCapability.server_id == server.id)
                    .order_by(McpCapability.remote_name.asc())
                )
            ).scalars().all()
        )
        mcp_groups.append(
            {
                "server_id": str(server.id),
                "server_name": server.name,
                "enabled": server.enabled,
                "health_status": server.health_status,
                "capabilities": [
                    {
                        "id": str(item.id),
                        "name": item.remote_name,
                        "description": item.description,
                        "enabled": item.enabled and item.review_status == "approved" and item.removed_at is None,
                    }
                    for item in capabilities
                ],
            }
        )
    kbs = list(
        (
            await db.execute(
                select(KnowledgeBase)
                .where(
                    KnowledgeBase.tenant_id == auth.tenant.tenant_id,
                    KnowledgeBase.status == "active",
                )
                .order_by(KnowledgeBase.name.asc())
            )
        ).scalars().all()
    )
    return ApiResponse(
        data={
            "skills": [{"id": item.id, "name": item.name, "description": item.description, "enabled": item.enabled} for item in skills],
            "tools": [item.__dict__ for item in tool_registry.snapshot()],
            "mcp_servers": mcp_groups,
            "knowledge_bases": [{"id": str(item.id), "name": item.name, "description": item.description} for item in kbs],
        }
    )


@router.post("", response_model=ApiResponse)
async def create_agent(
    body: AgentCreateRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    _ensure_enabled()
    try:
        row = await agent_manager.create(
            db,
            tenant_id=auth.tenant.tenant_id,
            created_by=auth.user_id,
            values=body.model_dump(),
        )
    except AgentManagerError as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
    await audit.audit(
        auth.tenant.tenant_id,
        "agent.created",
        user_id=auth.user_id,
        resource_id=str(row.id),
        detail={"name": row.name, "scope": row.scope},
    )
    return ApiResponse(data=await _agent_data(db, row, include_prompt=True))


@router.get("/{agent_id}", response_model=ApiResponse)
async def get_agent(
    agent_id: uuid.UUID,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    return ApiResponse(data=await _agent_data(db, await _require_agent(db, auth, agent_id), include_prompt=True))


@router.patch("/{agent_id}", response_model=ApiResponse)
async def update_agent(
    agent_id: uuid.UUID,
    body: AgentUpdateRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    await _require_agent(db, auth, agent_id)
    values = body.model_dump(exclude_unset=True)
    try:
        row = await agent_manager.update(
            db, tenant_id=auth.tenant.tenant_id, agent_id=agent_id, values=values
        )
    except AgentManagerError as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc
    assert row is not None
    await audit.audit(
        auth.tenant.tenant_id,
        "agent.updated",
        user_id=auth.user_id,
        resource_id=str(agent_id),
        detail={"fields": sorted(values)},
    )
    return ApiResponse(data=await _agent_data(db, row, include_prompt=True))


@router.post("/{agent_id}/{action}", response_model=ApiResponse)
async def toggle_agent(
    agent_id: uuid.UUID,
    action: str,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    row = await _require_agent(db, auth, agent_id)
    if action not in {"enable", "disable"}:
        raise HTTPException(status_code=404, detail="not found")
    row = await agent_manager.update(
        db,
        tenant_id=auth.tenant.tenant_id,
        agent_id=agent_id,
        values={"status": "active" if action == "enable" else "disabled"},
    )
    await audit.audit(
        auth.tenant.tenant_id,
        f"agent.{action}d",
        user_id=auth.user_id,
        resource_id=str(agent_id),
    )
    return ApiResponse(data=await _agent_data(db, row))


@router.delete("/{agent_id}", response_model=ApiResponse)
async def delete_agent(
    agent_id: uuid.UUID,
    hard: bool = False,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    await _require_agent(db, auth, agent_id)
    if hard:
        refs = await repo.agent_repository.list_sessions_referencing(db, agent_id)
        if refs:
            raise HTTPException(status_code=409, detail="存在会话引用，不能物理删除")
        await repo.agent_repository.hard_delete(
            db, tenant_id=auth.tenant.tenant_id, agent_id=agent_id
        )
        action = "agent.deleted"
    else:
        await repo.agent_repository.archive(
            db, tenant_id=auth.tenant.tenant_id, agent_id=agent_id
        )
        action = "agent.archived"
    await audit.audit(
        auth.tenant.tenant_id,
        action,
        user_id=auth.user_id,
        resource_id=str(agent_id),
    )
    return ApiResponse(data={"deleted": True, "hard": hard})


async def _replace_binding(
    *,
    kind: str,
    values: list,
    agent_id: uuid.UUID,
    auth: AuthContext,
    db: AsyncSession,
) -> dict:
    await _require_agent(db, auth, agent_id)
    tenant_id = auth.tenant.tenant_id
    if kind == "skills":
        found = set((await db.execute(select(Skill.id).where(Skill.id.in_(values)))).scalars().all()) if values else set()
    elif kind == "tools":
        known = {item.name for item in tool_registry.snapshot()}
        found = set(values) & known
    elif kind == "mcp":
        try:
            parsed = [uuid.UUID(value) for value in values]
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="MCP capability id 无效") from exc
        found = set((await db.execute(select(McpCapability.id).join(McpServer).where(McpCapability.id.in_(parsed), McpServer.tenant_id == tenant_id))).scalars().all()) if parsed else set()
        values = parsed
    else:
        try:
            parsed = [uuid.UUID(value) for value in values]
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="知识库 id 无效") from exc
        found = set((await db.execute(select(KnowledgeBase.id).where(KnowledgeBase.id.in_(parsed), KnowledgeBase.tenant_id == tenant_id))).scalars().all()) if parsed else set()
        values = parsed
    if found != set(values):
        raise HTTPException(status_code=404, detail="部分绑定目标不存在或不属于当前租户")
    await repo.agent_repository.replace_bindings(
        db, agent_id=agent_id, kind=kind, ids=values
    )
    await audit.audit(
        tenant_id,
        "agent.binding.changed",
        user_id=auth.user_id,
        resource_id=str(agent_id),
        detail={"kind": kind, "count": len(values)},
    )
    return await _binding_detail(db, agent_id)


@router.put("/{agent_id}/bindings/{kind}", response_model=ApiResponse)
async def replace_bindings(
    agent_id: uuid.UUID,
    kind: str,
    body: AgentBindingIdsRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    mapping = {
        "skills": body.skill_ids,
        "tools": body.tool_names,
        "mcp": body.capability_ids,
        "kbs": body.kb_ids,
    }
    if kind not in mapping or mapping[kind] is None:
        raise HTTPException(status_code=422, detail="绑定类型与请求字段不匹配")
    return ApiResponse(
        data=await _replace_binding(
            kind=kind,
            values=mapping[kind] or [],
            agent_id=agent_id,
            auth=auth,
            db=db,
        )
    )


@router.get("/{agent_id}/usage", response_model=ApiResponse)
async def agent_usage(
    agent_id: uuid.UUID,
    days: int = Query(14, ge=1, le=90),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    await _require_agent(db, auth, agent_id)
    since = datetime.now(UTC) - timedelta(days=days - 1)
    messages = list(
        (
            await db.execute(
                select(Message)
                .where(Message.agent_id == agent_id, Message.created_at >= since)
                .order_by(Message.created_at.asc())
            )
        ).scalars().all()
    )
    spans = list(
        (
            await db.execute(
                select(AgentSpan).where(
                AgentSpan.agent_id == agent_id,
                AgentSpan.started_at >= since,
            )
            )
        ).scalars().all()
    )
    trend: dict[str, dict] = {}
    for item in messages:
        day = item.created_at.date().isoformat()
        bucket = trend.setdefault(
            day, {"day": day, "messages": 0, "tool_calls": 0, "tokens": 0}
        )
        bucket["messages"] += 1
    for span in spans:
        day = span.started_at.date().isoformat()
        bucket = trend.setdefault(
            day, {"day": day, "messages": 0, "tool_calls": 0, "tokens": 0}
        )
        if span.span_type == "tool":
            bucket["tool_calls"] += 1
        elif span.span_type == "root":
            usage = span.token_usage or {}
            bucket["tokens"] += int(usage.get("total", 0) or 0)
    total_messages = len(messages)
    tool_calls = sum(item["tool_calls"] for item in trend.values())
    total_tokens = sum(item["tokens"] for item in trend.values())
    return ApiResponse(
        data={
            "days": days,
            "messages": total_messages,
            "tool_calls": tool_calls,
            "tokens": total_tokens,
            "trend": [trend[day] for day in sorted(trend)],
        }
    )
