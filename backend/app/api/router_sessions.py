"""会话 CRUD 路由。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..harness.session_manager import session_manager
from ..observability import audit
from ..storage.db import get_db
from .deps import AuthContext, require_auth
from .schemas import ApiResponse, SessionCreateRequest, SessionListData, SessionResponse

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", response_model=ApiResponse)
async def create_session(
    body: SessionCreateRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    data = await session_manager.create_session(
        db, tenant_id=auth.tenant.tenant_id, user_id=auth.user_id, title=body.title
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
    if session is None or session.status == "deleted":
        raise HTTPException(status_code=404, detail="session not found")
    return ApiResponse(
        data=SessionResponse(
            session_id=str(session.id),
            thread_id=session.thread_id,
            title=session.title,
            status=session.status,
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
