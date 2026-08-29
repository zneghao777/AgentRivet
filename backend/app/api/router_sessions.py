"""会话 CRUD 路由。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..harness.session_manager import session_manager
from ..observability import audit
from ..storage.db import get_db
from .deps import AuthContext, require_auth
from .schemas import (
    AgentSummaryResponse,
    ApiResponse,
    SessionCreateRequest,
    SessionListData,
    SessionResponse,
    SessionUpdateRequest,
)

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", response_model=ApiResponse)
async def create_session(
    body: SessionCreateRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    agent_id = None
    if body.agent_id:
        try:
            agent_id = uuid.UUID(body.agent_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="agent_id 无效") from exc
        from ..agents.manager import AgentManagerError, agent_manager

        try:
            await agent_manager.require_active_user_agent(
                db, tenant_id=auth.tenant.tenant_id, agent_id=agent_id
            )
        except AgentManagerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    data = await session_manager.create_session(
        db,
        tenant_id=auth.tenant.tenant_id,
        user_id=auth.user_id,
        title=body.title,
        agent_id=agent_id,
    )
    await audit.audit(
        auth.tenant.tenant_id,
        "session.create",
        user_id=auth.user_id,
        session_id=uuid.UUID(data["session_id"]),
        detail={"status": "active"},
    )
    return ApiResponse(data=data)


@router.get("", response_model=ApiResponse)
async def list_sessions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    items, total = await session_manager.list_sessions(
        db, tenant_id=auth.tenant.tenant_id, page=page, page_size=page_size
    )
    return ApiResponse(
        data=SessionListData(items=items, total=total, page=page, page_size=page_size)
    )


@router.get("/{session_id}", response_model=ApiResponse)
async def get_session(
    session_id: uuid.UUID,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    from ..storage import repositories as repo

    session = await repo.get_session(db, session_id)
    if (
        session is None
        or session.tenant_id != auth.tenant.tenant_id
        or session.status == "deleted"
    ):
        raise HTTPException(status_code=404, detail="session not found")
    agent = None
    if session.agent_id:
        row = await repo.agent_repository.get(
            db, tenant_id=auth.tenant.tenant_id, agent_id=session.agent_id
        )
        if row is not None:
            agent = AgentSummaryResponse(id=str(row.id), name=row.name, status=row.status)
    return ApiResponse(
        data=SessionResponse(
            session_id=str(session.id),
            thread_id=session.thread_id,
            title=session.title,
            status=session.status,
            agent_id=str(session.agent_id) if session.agent_id else None,
            agent=agent,
            created_at=session.created_at.isoformat() if session.created_at else None,
            updated_at=session.updated_at.isoformat() if session.updated_at else None,
        )
    )


@router.patch("/{session_id}", response_model=ApiResponse)
async def update_session(
    session_id: uuid.UUID,
    body: SessionUpdateRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """更新会话标题或 Agent 绑定；Agent 切换从下一条消息生效。"""
    from ..agents.manager import AgentManagerError, agent_manager
    from ..storage import repositories as repo

    session = await repo.get_session(db, session_id)
    if session is None or session.tenant_id != auth.tenant.tenant_id or session.status == "deleted":
        raise HTTPException(status_code=404, detail="session not found")
    if "title" in body.model_fields_set and body.title is not None:
        await repo.set_session_title(db, session_id, body.title)
    if "agent_id" in body.model_fields_set:
        agent_id = None
        if body.agent_id:
            try:
                agent_id = uuid.UUID(body.agent_id)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="agent_id 无效") from exc
            try:
                await agent_manager.require_active_user_agent(
                    db, tenant_id=auth.tenant.tenant_id, agent_id=agent_id
                )
            except AgentManagerError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        session = await repo.set_session_agent(
            db,
            session_id=session_id,
            tenant_id=auth.tenant.tenant_id,
            agent_id=agent_id,
        )
        await audit.audit(
            auth.tenant.tenant_id,
            "session.agent.changed",
            user_id=auth.user_id,
            session_id=session_id,
            detail={"agent_id": str(agent_id) if agent_id else None},
        )
    return ApiResponse(
        data=SessionResponse(
            session_id=str(session.id),
            thread_id=session.thread_id,
            title=body.title if "title" in body.model_fields_set else session.title,
            status=session.status,
            agent_id=str(session.agent_id) if session.agent_id else None,
            created_at=session.created_at.isoformat() if session.created_at else None,
            updated_at=session.updated_at.isoformat() if session.updated_at else None,
        )
    )


@router.delete("/{session_id}", response_model=ApiResponse)
async def delete_session(
    session_id: uuid.UUID,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    ok = await session_manager.delete_session(db, session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="session not found")
    await audit.audit(
        auth.tenant.tenant_id,
        "session.delete",
        user_id=auth.user_id,
        session_id=session_id,
    )
    return ApiResponse()


@router.get("/{session_id}/messages", response_model=ApiResponse)
async def get_messages(
    session_id: uuid.UUID,
    limit: int = Query(100, ge=1, le=500),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    msgs = await session_manager.get_history(db, session_id, limit=limit)
    return ApiResponse(data=msgs)


@router.get("/{session_id}/tasks", response_model=ApiResponse)
async def get_session_tasks(
    session_id: uuid.UUID,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """按创建时间返回会话 TaskCard 回放数据。"""
    from ..storage import repositories as repo

    session = await repo.get_session(db, session_id)
    if (
        session is None
        or session.tenant_id != auth.tenant.tenant_id
        or session.status == "deleted"
    ):
        raise HTTPException(status_code=404, detail="session not found")
    rows = await repo.list_agent_tasks(db, session_id=session_id)
    return ApiResponse(
        data={
            "items": [
                {
                    "task_id": row.task_id,
                    "parent_task_id": row.parent_task_id,
                    "target_agent_id": str(row.target_agent_id) if row.target_agent_id else None,
                    "target_agent_name": row.target_agent_name,
                    "goal": row.goal,
                    "context_brief": row.context_brief,
                    "constraints": row.constraints or [],
                    "expected_output": row.expected_output,
                    "artifacts": row.artifacts or [],
                    "status": row.status,
                    "result": row.result,
                    "error": row.error,
                    "duration_ms": row.duration_ms,
                    "trace_id": row.trace_id,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in rows
            ]
        }
    )
