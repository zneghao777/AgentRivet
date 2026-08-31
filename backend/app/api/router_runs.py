"""持久化 ChatRun 创建、查询、订阅、取消和恢复 API。"""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..agents.resolver import resolve_agent_capabilities, resolve_orchestration_agents
from ..config import settings
from ..harness.llm_guard import get_degrade_level
from ..harness.run_coordinator import run_coordinator
from ..harness.run_events import encode_sse_event, run_event_notifier
from ..observability import audit
from ..observability.metrics import metrics
from ..security.api_key import hash_api_key
from ..storage import chat_runs
from ..storage.db import get_db
from ..storage.models import ChatRun, Session
from .deps import AuthContext, require_auth
from .schemas import ApiResponse, ChatRunCreateRequest, ChatRunResumeRequest

router = APIRouter(tags=["chat-runs"])


def _run_user_id(request: Request, auth: AuthContext) -> str:
    if auth.user_id != settings.ANONYMOUS_USER_ID:
        return auth.user_id
    api_key = request.headers.get("X-API-Key") or ""
    return hash_api_key(api_key) if api_key else auth.user_id


def _error(status_code: int, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ApiResponse(code=code, message=message, data=None).model_dump(),
    )


def _agent_snapshot(resolved) -> dict[str, Any]:
    return {
        "agent_id": str(resolved.agent_id) if resolved.agent_id else None,
        "agent_name": resolved.agent_name,
        "status": resolved.status,
        "system_prompt_block": resolved.system_prompt_block,
        "allowed_skill_names": list(resolved.allowed_skill_names or []),
        "allowed_tool_names": sorted(resolved.allowed_tool_names or set()),
        "allowed_capability_ids": sorted(
            str(value) for value in resolved.allowed_capability_ids or set()
        ),
        "allowed_kb_ids": [str(value) for value in resolved.allowed_kb_ids or []],
        "model": resolved.model,
        "model_base_url": resolved.model_base_url,
        "memory_enabled": resolved.memory_enabled,
    }


def _orchestration_snapshot(rows) -> list[dict[str, Any]]:
    return [
        {
            "agent_id": str(item.agent_id),
            "tenant_id": item.tenant_id,
            "name": item.name,
            "description": item.description,
            "scope": item.scope,
            "system_prompt": item.system_prompt,
            "model": item.model,
            "model_base_url": item.model_base_url,
            "memory_enabled": item.memory_enabled,
            "allowed_skill_names": list(item.allowed_skill_names or []),
            "allowed_tool_names": sorted(item.allowed_tool_names or set()),
            "allowed_capability_ids": sorted(
                str(value) for value in item.allowed_capability_ids or set()
            ),
            "allowed_kb_ids": [str(value) for value in item.allowed_kb_ids or []],
            "capability_summary": item.capability_summary,
        }
        for item in rows.values()
    ]


def _serialize_run(
    run: ChatRun,
    *,
    content: str = "",
    token_usage: dict[str, int] | None = None,
    pending_approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "run_id": str(run.id),
        "session_id": str(run.session_id),
        "request_id": run.request_id,
        "status": run.status,
        "content": content,
        "trace_id": run.trace_id,
        "token_usage": token_usage,
        "pending_approval": pending_approval if run.status == "pending_approval" else None,
        "attempt": run.attempt,
        "error_code": run.error_code,
        "error_message": run.error_message,
        "last_event_sequence": run.last_event_sequence,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        "events_url": f"{settings.API_PREFIX}/runs/{run.id}/events",
    }


@router.post("/sessions/{session_id}/runs", status_code=202)
async def create_run(
    session_id: uuid.UUID,
    body: ChatRunCreateRequest,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=64),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    if not settings.CHAT_RUN_ENABLED:
        return _error(503, 50310, "异步运行调度器未启用")
    if body.provider_config is not None and body.provider_config.api_key is not None:
        return _error(422, 42200, "持久化运行不接受明文 Provider 密钥，请使用受控配置引用")

    run_user_id = _run_user_id(request, auth)
    try:
        async with db.begin():
            session = (
                await db.execute(
                    select(Session)
                    .where(
                        Session.id == session_id,
                        Session.tenant_id == auth.tenant.tenant_id,
                        Session.user_id == auth.user_id,
                        Session.status != "deleted",
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if session is None:
                return _error(404, 40410, "会话不存在或不属于当前租户")

            requested_agent_id = None
            if body.agent_id:
                try:
                    requested_agent_id = uuid.UUID(body.agent_id)
                except ValueError:
                    return _error(422, 42200, "agent_id 无效")
            resolved_agent = await resolve_agent_capabilities(
                db,
                tenant_id=auth.tenant.tenant_id,
                agent_id=requested_agent_id or session.agent_id,
            )
            requested_skills = list(body.skills or [])
            if resolved_agent.active:
                allowed_skills = resolved_agent.allowed_skill_names or []
                requested_skills = (
                    allowed_skills
                    if body.skills is None
                    else [name for name in allowed_skills if name in set(body.skills)]
                )

            requested_mcp: set[uuid.UUID] = set()
            for ref in body.capabilities or []:
                if ref.kind != "mcp_tool":
                    continue
                try:
                    requested_mcp.add(uuid.UUID(ref.id))
                except ValueError:
                    return _error(422, 42200, "MCP capability id 无效")
            if resolved_agent.active:
                bound = resolved_agent.allowed_capability_ids or set()
                requested_mcp = bound if not requested_mcp else bound & requested_mcp

            from ..knowledge.service import knowledge_service

            knowledge_ids = await knowledge_service.resolve_ids_by_names(
                db,
                tenant_id=auth.tenant.tenant_id,
                names=list(body.knowledge_bases or []),
            )
            if resolved_agent.active and resolved_agent.allowed_kb_ids:
                bound_kbs = set(resolved_agent.allowed_kb_ids)
                knowledge_ids = (
                    list(resolved_agent.allowed_kb_ids)
                    if body.knowledge_bases is None
                    else [value for value in knowledge_ids if value in bound_kbs]
                )

            orchestration_agents = (
                await resolve_orchestration_agents(
                    db, tenant_id=auth.tenant.tenant_id
                )
                if settings.MULTI_AGENT_ENABLED
                else {}
            )
            model = body.model or resolved_agent.model or settings.LLM_MODEL
            model_base_url = (
                body.provider_config.base_url
                if body.provider_config is not None
                else resolved_agent.model_base_url
            )
            request_payload = {
                "message": body.message,
                "approval_mode": body.approval_mode,
                "model": model,
                "model_base_url": model_base_url,
                "agent_id": str(resolved_agent.agent_id) if resolved_agent.agent_id else None,
                "agent_snapshot": _agent_snapshot(resolved_agent),
                "skills": requested_skills,
                "knowledge_bases": list(body.knowledge_bases or []),
                "knowledge_base_ids": [str(value) for value in knowledge_ids],
                "mcp_capability_ids": sorted(str(value) for value in requested_mcp),
                "attachment_ids": list(body.attachment_ids or []),
                "orchestration_agents": _orchestration_snapshot(orchestration_agents),
                "degrade_level": get_degrade_level(),
            }
            request_id = f"run-req-{uuid.uuid4().hex}"
            run, created = await chat_runs.create_run(
                db,
                session=session,
                tenant_id=auth.tenant.tenant_id,
                user_id=run_user_id,
                request_id=request_id,
                client_request_id=idempotency_key,
                request_payload=request_payload,
                max_queued_per_user=settings.CHAT_RUN_MAX_QUEUED_PER_USER,
                write_outbox=(
                    settings.CHAT_RUN_DURABLE_ENABLED and settings.MQ_ENABLED
                ),
                max_queued_global=(
                    settings.CHAT_RUN_MAX_QUEUED_GLOBAL
                    if settings.CHAT_RUN_DURABLE_ENABLED and settings.MQ_ENABLED
                    else None
                ),
            )
            if not session.title or session.title in {"新对话", "新会话"}:
                from ..harness.session_manager import derive_session_title

                session.title = derive_session_title(body.message)
    except chat_runs.ChatRunConflict as exc:
        status_code = 503 if exc.code == 50310 else 429 if exc.code == 42902 else 409
        return _error(status_code, exc.code, exc.message)
    except IntegrityError:
        await db.rollback()
        return _error(409, 40901, "当前会话已有任务正在进行中")

    if created:
        metrics.record_chat_run_status("queued", 1)
        await audit.audit(
            auth.tenant.tenant_id,
            "chat.run.create",
            user_id=run_user_id,
            session_id=session_id,
            request_id=run.request_id,
            resource_id=str(run.id),
            detail={"run_id": str(run.id), "status": run.status, "attempt": run.attempt},
        )
        await run_event_notifier.publish(run.id)
        await run_coordinator.start()
        await run_coordinator.enqueue(run.id)
    return ApiResponse(data=_serialize_run(run))


@router.get("/runs/active")
async def list_active_runs(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    rows = list(
        (
            await db.execute(
                select(ChatRun)
                .where(
                    ChatRun.tenant_id == auth.tenant.tenant_id,
                    ChatRun.user_id == _run_user_id(request, auth),
                    ChatRun.status.in_(chat_runs.ACTIVE_STATUSES),
                )
                .order_by(ChatRun.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return ApiResponse(data={"items": [_serialize_run(run) for run in rows]})


@router.get("/runs/{run_id}")
async def get_run(
    run_id: uuid.UUID,
    request: Request,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    run = await chat_runs.get_owned_run(
        db,
        run_id,
        tenant_id=auth.tenant.tenant_id,
        user_id=_run_user_id(request, auth),
    )
    if run is None:
        return _error(404, 40410, "运行不存在或不属于当前租户")
    content, token_usage = await chat_runs.aggregate_run_events(db, run_id=run.id)
    pending = await chat_runs.latest_event_payload(
        db, run_id=run.id, event_type="interrupt_requested"
    )
    return ApiResponse(data=_serialize_run(
        run, content=content, token_usage=token_usage, pending_approval=pending
    ))


@router.get("/sessions/{session_id}/active-run")
async def get_active_run(
    session_id: uuid.UUID,
    request: Request,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    run = await chat_runs.get_active_run(
        db,
        session_id=session_id,
        tenant_id=auth.tenant.tenant_id,
        user_id=_run_user_id(request, auth),
    )
    if run is None:
        return ApiResponse(data=None)
    content, token_usage = await chat_runs.aggregate_run_events(db, run_id=run.id)
    pending = await chat_runs.latest_event_payload(
        db, run_id=run.id, event_type="interrupt_requested"
    )
    return ApiResponse(data=_serialize_run(
        run, content=content, token_usage=token_usage, pending_approval=pending
    ))


@router.get("/sessions/{session_id}/runs")
async def list_runs(
    session_id: uuid.UUID,
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    session = await db.get(Session, session_id)
    if (
        session is None
        or session.tenant_id != auth.tenant.tenant_id
        or session.user_id != auth.user_id
        or session.status == "deleted"
    ):
        return _error(404, 40410, "会话不存在或不属于当前租户")
    rows, total = await chat_runs.list_session_runs(
        db,
        session_id=session_id,
        tenant_id=auth.tenant.tenant_id,
        user_id=_run_user_id(request, auth),
        page=page,
        page_size=page_size,
    )
    return ApiResponse(
        data={
            "items": [_serialize_run(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    )


@router.get("/runs/{run_id}/events")
async def subscribe_events(
    run_id: uuid.UUID,
    request: Request,
    after_sequence: int = Query(0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    run_user_id = _run_user_id(request, auth)
    run = await chat_runs.get_owned_run(
        db,
        run_id,
        tenant_id=auth.tenant.tenant_id,
        user_id=run_user_id,
    )
    if run is None:
        return _error(404, 40410, "运行不存在或不属于当前租户")
    try:
        header_sequence = max(0, int(last_event_id or 0))
    except ValueError:
        header_sequence = 0
    cursor = max(after_sequence, header_sequence)

    async def event_stream():
        nonlocal cursor
        metrics.change_chat_run_subscribers(1)
        replayed = 0
        try:
            async with AsyncSessionLocal() as snapshot_db:
                current = await chat_runs.get_owned_run(
                    snapshot_db,
                    run_id,
                    tenant_id=auth.tenant.tenant_id,
                    user_id=run_user_id,
                )
                if current is None:
                    return
                content, token_usage = await chat_runs.aggregate_run_events(
                    snapshot_db, run_id=run_id
                )
                pending = await chat_runs.latest_event_payload(
                    snapshot_db,
                    run_id=run_id,
                    event_type="interrupt_requested",
                )
            snapshot = _serialize_run(
                current,
                content=content,
                token_usage=token_usage,
                pending_approval=pending,
            )
            yield encode_sse_event(
                "run_snapshot", snapshot, cursor
            )
            poll_seconds = settings.CHAT_RUN_EVENT_POLL_INTERVAL_MS / 1000
            reconnected_sent = False
            last_emit = time.monotonic()
            while True:
                async with AsyncSessionLocal() as event_db:
                    events = await chat_runs.list_events(
                        event_db,
                        run_id=run_id,
                        after_sequence=cursor,
                    )
                    latest = await chat_runs.get_owned_run(
                        event_db,
                        run_id,
                        tenant_id=auth.tenant.tenant_id,
                        user_id=run_user_id,
                    )
                for event in events:
                    yield encode_sse_event(
                        event.event_type,
                        dict(event.payload or {}),
                        event.sequence,
                    )
                    cursor = event.sequence
                    replayed += 1
                    last_emit = time.monotonic()
                if not reconnected_sent:
                    reconnect_sequence = int(latest.last_event_sequence if latest else cursor)
                    yield encode_sse_event(
                        "run_reconnected",
                        {
                            "run_id": str(run_id),
                            "session_id": str(run.session_id),
                            "sequence": reconnect_sequence,
                        },
                        reconnect_sequence,
                    )
                    reconnected_sent = True
                    last_emit = time.monotonic()
                if latest is None:
                    return
                metrics.set_chat_run_event_lag(
                    max(0, int(latest.last_event_sequence or 0) - cursor)
                )
                if latest.status in chat_runs.TERMINAL_STATUSES and cursor >= int(
                    latest.last_event_sequence or 0
                ):
                    return
                if await request.is_disconnected():
                    return
                await run_event_notifier.wait(run_id, poll_seconds)
                if time.monotonic() - last_emit >= 15:
                    yield ": keep-alive\n\n"
                    last_emit = time.monotonic()
        finally:
            metrics.change_chat_run_subscribers(-1)
            metrics.record_chat_run_reconnect(replayed)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: uuid.UUID,
    request: Request,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    run_user_id = _run_user_id(request, auth)
    run = await chat_runs.get_owned_run(
        db,
        run_id,
        tenant_id=auth.tenant.tenant_id,
        user_id=run_user_id,
    )
    if run is None:
        return _error(404, 40410, "运行不存在或不属于当前租户")
    await audit.audit(
        auth.tenant.tenant_id,
        "chat.run.cancel_request",
        user_id=auth.user_id,
        session_id=run.session_id,
        request_id=run.request_id,
        resource_id=str(run.id),
        detail={"run_id": str(run.id), "status": run.status, "attempt": run.attempt},
    )
    await run_coordinator.request_cancel(run.id)
    async with AsyncSessionLocal() as fresh_db:
        current = await chat_runs.get_owned_run(
            fresh_db,
            run.id,
            tenant_id=auth.tenant.tenant_id,
            user_id=run_user_id,
        )
    metrics.record_chat_run_cancel(current.status if current else "not_found")
    return ApiResponse(data=_serialize_run(current or run))


@router.post("/runs/{run_id}/resume", status_code=202)
async def resume_run(
    run_id: uuid.UUID,
    body: ChatRunResumeRequest,
    request: Request,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    run = await chat_runs.get_owned_run(
        db,
        run_id,
        tenant_id=auth.tenant.tenant_id,
        user_id=_run_user_id(request, auth),
    )
    if run is None:
        return _error(404, 40410, "运行不存在或不属于当前租户")
    if run.status != "pending_approval":
        if run.status in chat_runs.TERMINAL_STATUSES:
            return ApiResponse(data=_serialize_run(run))
        return _error(409, 40902, "当前运行不处于待审批状态")
    if settings.CHAT_RUN_DURABLE_ENABLED:
        from ..harness.app_runtime import harness_app

        if harness_app.runtime is None or harness_app.runtime.checkpointer is None:
            return _error(503, 50310, "审批恢复需要可用的会话检查点服务")
    await run_coordinator.resume(
        run.id,
        {"decision": body.decision, "input": body.input},
    )
    await audit.audit(
        auth.tenant.tenant_id,
        "chat.run.resume",
        user_id=auth.user_id,
        session_id=run.session_id,
        request_id=run.request_id,
        resource_id=str(run.id),
        detail={"run_id": str(run.id), "status": "queued", "attempt": run.attempt},
    )
    return ApiResponse(data={**_serialize_run(run), "status": "queued"})


@router.get("/admin/chat-runs/queue")
async def chat_run_queue_status(
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import func

    from ..storage.models import ChatRunOutbox

    queued_count, oldest = (
        await db.execute(
            select(func.count(ChatRun.id), func.min(ChatRun.created_at)).where(
                ChatRun.status == "queued",
                ChatRun.tenant_id == auth.tenant.tenant_id,
            )
        )
    ).one()
    unpublished, last_error = (
        await db.execute(
            select(
                func.count(ChatRunOutbox.id),
                func.max(ChatRunOutbox.last_error),
            )
            .join(ChatRun, ChatRun.id == ChatRunOutbox.run_id)
            .where(
                ChatRun.tenant_id == auth.tenant.tenant_id,
                ChatRunOutbox.published_at.is_(None),
            )
        )
    ).one()
    now = chat_runs.utcnow()
    return ApiResponse(data={
        "queued": int(queued_count or 0),
        "oldest_age_seconds": (
            max(0, int((now - oldest).total_seconds())) if oldest else 0
        ),
        "unpublished_outbox": int(unpublished or 0),
        "last_publish_error": last_error,
    })


# 避免在路由定义前引入 DB session 工厂形成循环。
from ..storage.db import AsyncSessionLocal  # noqa: E402
