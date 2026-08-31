"""ChatRun 状态机、事件、租约和 Outbox 持久化。"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import ChatRun, ChatRunEvent, ChatRunOutbox, Message, Session

ACTIVE_STATUSES = frozenset({"queued", "running", "pending_approval"})
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "canceled"})
RUNNING_CAPACITY_STATUSES = frozenset({"running", "pending_approval"})
ALLOWED_TRANSITIONS = {
    "queued": frozenset({"running", "canceled", "failed"}),
    "running": frozenset({"pending_approval", "succeeded", "failed", "canceled", "queued"}),
    "pending_approval": frozenset({"queued", "canceled", "failed"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "canceled": frozenset(),
}


class ChatRunConflict(RuntimeError):
    """业务冲突，API 层映射为稳定的 409/429 错误码。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def utcnow() -> datetime:
    return datetime.now(UTC)


def request_fingerprint(payload: dict[str, Any]) -> str:
    """生成不包含密钥的稳定幂等指纹。"""
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_transition(current: str, target: str) -> None:
    if current == target:
        return
    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise ChatRunConflict(40902, f"运行状态 {current} 不允许转换为 {target}")


def _event_payload(
    run: ChatRun,
    sequence: int,
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "run_id": str(run.id),
        "session_id": str(run.session_id),
        "sequence": sequence,
        **(payload or {}),
    }


def _append_locked(
    db: AsyncSession,
    run: ChatRun,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> ChatRunEvent:
    sequence = int(run.last_event_sequence or 0) + 1
    event = ChatRunEvent(
        run_id=run.id,
        sequence=sequence,
        event_type=event_type,
        payload=_event_payload(run, sequence, payload),
    )
    run.last_event_sequence = sequence
    run.updated_at = utcnow()
    db.add(event)
    return event


async def get_owned_run(
    db: AsyncSession,
    run_id: uuid.UUID,
    *,
    tenant_id: str,
    user_id: str | None = None,
    for_update: bool = False,
) -> ChatRun | None:
    query = select(ChatRun).where(
        ChatRun.id == run_id,
        ChatRun.tenant_id == tenant_id,
    )
    if user_id is not None:
        query = query.where(ChatRun.user_id == user_id)
    if for_update:
        query = query.with_for_update()
    return (await db.execute(query)).scalar_one_or_none()


async def get_active_run(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    tenant_id: str,
    user_id: str,
) -> ChatRun | None:
    return (
        await db.execute(
            select(ChatRun)
            .where(
                ChatRun.session_id == session_id,
                ChatRun.tenant_id == tenant_id,
                ChatRun.user_id == user_id,
                ChatRun.status.in_(ACTIVE_STATUSES),
            )
            .order_by(ChatRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def list_session_runs(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    tenant_id: str,
    user_id: str,
    page: int,
    page_size: int,
) -> tuple[list[ChatRun], int]:
    filters = (
        ChatRun.session_id == session_id,
        ChatRun.tenant_id == tenant_id,
        ChatRun.user_id == user_id,
    )
    total = (
        await db.execute(select(func.count(ChatRun.id)).where(*filters))
    ).scalar_one()
    rows = await db.execute(
        select(ChatRun)
        .where(*filters)
        .order_by(ChatRun.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(rows.scalars().all()), int(total)


async def create_run(
    db: AsyncSession,
    *,
    session: Session,
    tenant_id: str,
    user_id: str,
    request_id: str,
    client_request_id: str,
    request_payload: dict[str, Any],
    max_queued_per_user: int,
    write_outbox: bool,
    max_queued_global: int | None = None,
) -> tuple[ChatRun, bool]:
    """在调用方事务内幂等创建运行、用户消息、初始事件和 Outbox。"""
    fingerprint = request_fingerprint(request_payload)
    existing = (
        await db.execute(
            select(ChatRun).where(
                ChatRun.tenant_id == tenant_id,
                ChatRun.user_id == user_id,
                ChatRun.client_request_id == client_request_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if (existing.request_payload or {}).get("request_fingerprint") != fingerprint:
            raise ChatRunConflict(40903, "幂等键已用于不同的请求内容")
        return existing, False

    active = await get_active_run(
        db,
        session_id=session.id,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    if active is not None:
        raise ChatRunConflict(40901, "当前会话已有任务正在进行中")

    queued_count = (
        await db.execute(
            select(func.count(ChatRun.id)).where(
                ChatRun.tenant_id == tenant_id,
                ChatRun.user_id == user_id,
                ChatRun.status == "queued",
            )
        )
    ).scalar_one()
    if max_queued_per_user >= 0 and int(queued_count) >= max_queued_per_user:
        raise ChatRunConflict(42902, "当前用户排队任务已达到限制")
    if max_queued_global is not None:
        global_queued = (
            await db.execute(
                select(func.count(ChatRun.id)).where(ChatRun.status == "queued")
            )
        ).scalar_one()
        if int(global_queued) >= max_queued_global:
            raise ChatRunConflict(50310, "运行调度器排队容量已满")

    run_id = uuid.uuid4()
    message = Message(
        session_id=session.id,
        role="user",
        content=str(request_payload["message"]),
        request_id=request_id,
        run_id=None,
        step_index=1,
        agent_id=(
            uuid.UUID(str(request_payload["agent_id"]))
            if request_payload.get("agent_id")
            else None
        ),
    )
    db.add(message)
    await db.flush()

    stored_payload = {
        **request_payload,
        "request_fingerprint": fingerprint,
    }
    run = ChatRun(
        id=run_id,
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session.id,
        request_id=request_id,
        client_request_id=client_request_id,
        user_message_id=message.id,
        status="queued",
        request_payload=stored_payload,
    )
    db.add(run)
    await db.flush()
    message.run_id = run.id
    _append_locked(db, run, "run_status", {"status": "queued"})
    if write_outbox:
        db.add(
            ChatRunOutbox(
                run_id=run.id,
                command_type="run.requested",
                payload={"run_id": str(run.id), "command_type": "run.requested"},
            )
        )
    session.updated_at = utcnow()
    await db.flush()
    return run, True


async def append_event(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> ChatRunEvent:
    run = (
        await db.execute(
            select(ChatRun).where(ChatRun.id == run_id).with_for_update()
        )
    ).scalar_one()
    event = _append_locked(db, run, event_type, payload)
    await db.commit()
    await db.refresh(event)
    return event


async def list_events(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    after_sequence: int = 0,
    limit: int = 1000,
) -> list[ChatRunEvent]:
    rows = await db.execute(
        select(ChatRunEvent)
        .where(
            ChatRunEvent.run_id == run_id,
            ChatRunEvent.sequence > max(0, after_sequence),
        )
        .order_by(ChatRunEvent.sequence.asc())
        .limit(limit)
    )
    return list(rows.scalars().all())


async def aggregate_run_events(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
) -> tuple[str, dict[str, int] | None]:
    rows = await db.execute(
        select(ChatRunEvent.event_type, ChatRunEvent.payload)
        .where(ChatRunEvent.run_id == run_id)
        .order_by(ChatRunEvent.sequence.asc())
    )
    text_parts: list[str] = []
    token_usage: dict[str, int] | None = None
    for event_type, payload in rows.all():
        if event_type == "message_delta":
            text_parts.append(str((payload or {}).get("content") or ""))
        elif event_type == "message_end" and isinstance((payload or {}).get("token_usage"), dict):
            token_usage = dict(payload["token_usage"])
    return "".join(text_parts), token_usage


async def latest_event_payload(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    event_type: str,
) -> dict[str, Any] | None:
    payload = (
        await db.execute(
            select(ChatRunEvent.payload)
            .where(
                ChatRunEvent.run_id == run_id,
                ChatRunEvent.event_type == event_type,
            )
            .order_by(ChatRunEvent.sequence.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return dict(payload) if payload else None


async def claim_run(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    worker_id: str,
    lease_seconds: int,
    max_active_per_user: int,
    max_active_per_tenant: int,
    max_attempts: int,
) -> ChatRun | None:
    """按用户和租户容量原子领取一个 queued run。"""
    run = (
        await db.execute(
            select(ChatRun).where(ChatRun.id == run_id).with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if run is None or run.status != "queued":
        await db.rollback()
        return None
    if run.attempt >= max_attempts:
        validate_transition(run.status, "failed")
        run.status = "failed"
        run.error_code = "CHAT_RUN_MAX_ATTEMPTS"
        run.error_message = "运行尝试次数已耗尽"
        run.finished_at = utcnow()
        _append_locked(db, run, "run_status", {"status": "failed"})
        _append_locked(
            db,
            run,
            "run_end",
            {"status": "failed", "duration_ms": 0, "tool_calls": 0},
        )
        await db.commit()
        return None
    user_active = (
        await db.execute(
            select(func.count(ChatRun.id)).where(
                ChatRun.tenant_id == run.tenant_id,
                ChatRun.user_id == run.user_id,
                ChatRun.status.in_(RUNNING_CAPACITY_STATUSES),
            )
        )
    ).scalar_one()
    tenant_active = (
        await db.execute(
            select(func.count(ChatRun.id)).where(
                ChatRun.tenant_id == run.tenant_id,
                ChatRun.status.in_(RUNNING_CAPACITY_STATUSES),
            )
        )
    ).scalar_one()
    if int(user_active) >= max_active_per_user or int(tenant_active) >= max_active_per_tenant:
        await db.rollback()
        return None
    validate_transition(run.status, "running")
    now = utcnow()
    run.status = "running"
    run.attempt += 1
    run.lease_owner = worker_id
    run.lease_expires_at = now + timedelta(seconds=lease_seconds)
    run.started_at = run.started_at or now
    run.updated_at = now
    _append_locked(db, run, "run_status", {"status": "running", "attempt": run.attempt})
    await db.commit()
    return run


async def renew_lease(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    worker_id: str,
    lease_seconds: int,
) -> bool:
    result = await db.execute(
        update(ChatRun)
        .where(
            ChatRun.id == run_id,
            ChatRun.status == "running",
            ChatRun.lease_owner == worker_id,
            ChatRun.cancel_requested_at.is_(None),
        )
        .values(
            lease_expires_at=utcnow() + timedelta(seconds=lease_seconds),
            updated_at=utcnow(),
        )
    )
    await db.commit()
    return bool(result.rowcount)


async def set_trace_id(
    db: AsyncSession, *, run_id: uuid.UUID, trace_id: str
) -> None:
    normalized = trace_id[:64]
    await db.execute(
        update(ChatRun)
        .where(ChatRun.id == run_id, ChatRun.trace_id.is_(None))
        .values(trace_id=normalized, updated_at=utcnow())
    )
    await db.execute(
        update(Message)
        .where(Message.run_id == run_id, Message.trace_id.is_(None))
        .values(trace_id=normalized)
    )
    await db.commit()


async def mark_pending_approval(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    worker_id: str,
) -> ChatRun:
    run = (
        await db.execute(
            select(ChatRun).where(ChatRun.id == run_id).with_for_update()
        )
    ).scalar_one()
    if run.status == "pending_approval":
        await db.rollback()
        return run
    if run.lease_owner != worker_id:
        await db.rollback()
        raise ChatRunConflict(40902, "运行租约已失效")
    validate_transition(run.status, "pending_approval")
    run.status = "pending_approval"
    run.lease_owner = None
    run.lease_expires_at = None
    _append_locked(db, run, "run_status", {"status": "pending_approval"})
    await db.commit()
    return run


async def finish_succeeded(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    worker_id: str,
    content: str,
    trace_id: str | None,
    token_usage: dict[str, int] | None,
    agent_id: uuid.UUID | None,
    duration_ms: int,
    tool_calls: int,
) -> ChatRun:
    run = (
        await db.execute(
            select(ChatRun).where(ChatRun.id == run_id).with_for_update()
        )
    ).scalar_one()
    if run.status == "succeeded":
        await db.rollback()
        return run
    if run.status in TERMINAL_STATUSES:
        await db.rollback()
        raise ChatRunConflict(40902, "运行已进入终态")
    if run.lease_owner != worker_id:
        await db.rollback()
        raise ChatRunConflict(40902, "运行租约已失效")
    validate_transition(run.status, "succeeded")
    if not content.strip():
        await db.rollback()
        raise ValueError("succeeded run requires a non-empty assistant message")
    if run.assistant_message_id is None:
        message = Message(
            session_id=run.session_id,
            role="assistant",
            content=content,
            token_usage=token_usage,
            trace_id=trace_id,
            request_id=run.request_id,
            run_id=run.id,
            step_index=2,
            agent_id=agent_id,
        )
        db.add(message)
        await db.flush()
        run.assistant_message_id = message.id
    now = utcnow()
    run.status = "succeeded"
    run.trace_id = trace_id or run.trace_id
    run.finished_at = now
    run.updated_at = now
    run.lease_owner = None
    run.lease_expires_at = None
    _append_locked(db, run, "run_status", {"status": "succeeded"})
    _append_locked(
        db,
        run,
        "run_end",
        {
            "status": "success",
            "duration_ms": duration_ms,
            "tool_calls": tool_calls,
        },
    )
    await db.commit()
    return run


async def finish_failed(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    error_code: str,
    error_message: str,
    duration_ms: int = 0,
) -> ChatRun:
    run = (
        await db.execute(
            select(ChatRun).where(ChatRun.id == run_id).with_for_update()
        )
    ).scalar_one()
    if run.status == "failed":
        await db.rollback()
        return run
    if run.status in TERMINAL_STATUSES:
        await db.rollback()
        return run
    validate_transition(run.status, "failed")
    run.status = "failed"
    run.error_code = error_code[:64]
    run.error_message = error_message[:2000]
    run.finished_at = utcnow()
    run.lease_owner = None
    run.lease_expires_at = None
    _append_locked(
        db,
        run,
        "error",
        {"code": error_code[:64], "message": error_message[:2000]},
    )
    _append_locked(db, run, "run_status", {"status": "failed"})
    _append_locked(
        db,
        run,
        "run_end",
        {"status": "failed", "duration_ms": duration_ms, "tool_calls": 0},
    )
    await db.commit()
    return run


async def request_cancel(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
) -> tuple[ChatRun, bool]:
    run = (
        await db.execute(
            select(ChatRun).where(ChatRun.id == run_id).with_for_update()
        )
    ).scalar_one()
    if run.status in TERMINAL_STATUSES:
        await db.rollback()
        return run, False
    now = utcnow()
    run.cancel_requested_at = run.cancel_requested_at or now
    if run.status in {"queued", "pending_approval"}:
        validate_transition(run.status, "canceled")
        run.status = "canceled"
        run.finished_at = now
        run.lease_owner = None
        run.lease_expires_at = None
        _append_locked(db, run, "run_status", {"status": "canceled"})
        _append_locked(
            db,
            run,
            "run_end",
            {"status": "canceled", "duration_ms": 0, "tool_calls": 0},
        )
    else:
        _append_locked(db, run, "run_status", {"status": "running", "cancel_requested": True})
    await db.commit()
    return run, True


async def finish_canceled(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    duration_ms: int,
) -> ChatRun:
    run = (
        await db.execute(
            select(ChatRun).where(ChatRun.id == run_id).with_for_update()
        )
    ).scalar_one()
    if run.status == "canceled":
        await db.rollback()
        return run
    if run.status in TERMINAL_STATUSES:
        await db.rollback()
        return run
    validate_transition(run.status, "canceled")
    run.status = "canceled"
    run.finished_at = utcnow()
    run.lease_owner = None
    run.lease_expires_at = None
    _append_locked(db, run, "run_status", {"status": "canceled"})
    _append_locked(
        db,
        run,
        "run_end",
        {"status": "canceled", "duration_ms": duration_ms, "tool_calls": 0},
    )
    await db.commit()
    return run


async def queue_resume(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    command: dict[str, Any],
    write_outbox: bool,
) -> ChatRun:
    run = (
        await db.execute(
            select(ChatRun).where(ChatRun.id == run_id).with_for_update()
        )
    ).scalar_one()
    if run.status != "pending_approval":
        if run.status in TERMINAL_STATUSES:
            await db.rollback()
            return run
        await db.rollback()
        raise ChatRunConflict(40902, "当前运行不处于待审批状态")
    validate_transition(run.status, "queued")
    run.status = "queued"
    run.request_payload = {**(run.request_payload or {}), "resume_command": command}
    run.lease_owner = None
    run.lease_expires_at = None
    _append_locked(db, run, "run_status", {"status": "queued", "resuming": True})
    if write_outbox:
        db.add(
            ChatRunOutbox(
                run_id=run.id,
                command_type="run.resume",
                payload={"run_id": str(run.id), "command_type": "run.resume"},
            )
        )
    await db.commit()
    return run


async def recover_expired_leases(
    db: AsyncSession,
    *,
    max_attempts: int,
    write_outbox: bool = False,
) -> tuple[int, int]:
    """把过期租约重新排队；耗尽尝试的运行进入 failed。"""
    now = utcnow()
    rows = list(
        (
            await db.execute(
                select(ChatRun)
                .where(
                    ChatRun.status == "running",
                    ChatRun.lease_expires_at.is_not(None),
                    ChatRun.lease_expires_at < now,
                )
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    requeued = failed = 0
    for run in rows:
        run.lease_owner = None
        run.lease_expires_at = None
        if run.attempt >= max_attempts:
            validate_transition(run.status, "failed")
            run.status = "failed"
            run.error_code = "CHAT_RUN_MAX_ATTEMPTS"
            run.error_message = "运行恢复尝试次数已耗尽"
            run.finished_at = now
            _append_locked(db, run, "run_status", {"status": "failed", "recovered": False})
            _append_locked(
                db,
                run,
                "run_end",
                {"status": "failed", "duration_ms": 0, "tool_calls": 0},
            )
            failed += 1
        else:
            validate_transition(run.status, "queued")
            run.status = "queued"
            _append_locked(db, run, "run_status", {"status": "queued", "recovered": True})
            if write_outbox:
                db.add(
                    ChatRunOutbox(
                        run_id=run.id,
                        command_type="run.requested",
                        payload={
                            "run_id": str(run.id),
                            "command_type": "run.requested",
                            "recovered": True,
                        },
                    )
                )
            requeued += 1
    await db.commit()
    return requeued, failed


async def list_queued_run_ids(db: AsyncSession, limit: int = 100) -> list[uuid.UUID]:
    rows = await db.execute(
        select(ChatRun.id, ChatRun.tenant_id, ChatRun.user_id)
        .where(ChatRun.status == "queued")
        .order_by(ChatRun.created_at.asc())
        .limit(max(limit, 1000))
    )
    # 租户 + 用户轮转，避免一个主体的长队列遮住其他用户。
    buckets: dict[tuple[str, str], list[uuid.UUID]] = {}
    for run_id, tenant_id, user_id in rows.all():
        buckets.setdefault((tenant_id, user_id), []).append(run_id)
    ordered: list[uuid.UUID] = []
    while buckets and len(ordered) < limit:
        for key in list(buckets):
            ordered.append(buckets[key].pop(0))
            if not buckets[key]:
                buckets.pop(key)
            if len(ordered) >= limit:
                break
    return ordered


async def has_tool_events(db: AsyncSession, *, run_id: uuid.UUID) -> bool:
    value = (
        await db.execute(
            select(func.count(ChatRunEvent.id)).where(
                ChatRunEvent.run_id == run_id,
                ChatRunEvent.event_type.in_({"tool_start", "tool_end"}),
            )
        )
    ).scalar_one()
    return bool(value)


@dataclass(frozen=True)
class OutboxClaim:
    id: int
    run_id: uuid.UUID
    command_type: str
    message_id: uuid.UUID
    payload: dict[str, Any]


async def claim_outbox_batch(db: AsyncSession, limit: int = 100) -> list[OutboxClaim]:
    rows = list(
        (
            await db.execute(
                select(ChatRunOutbox)
                .where(ChatRunOutbox.published_at.is_(None))
                .order_by(ChatRunOutbox.created_at.asc())
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.attempt += 1
    await db.commit()
    return [
        OutboxClaim(
            id=row.id,
            run_id=row.run_id,
            command_type=row.command_type,
            message_id=row.message_id,
            payload=dict(row.payload or {}),
        )
        for row in rows
    ]


async def mark_outbox_published(db: AsyncSession, outbox_id: int) -> None:
    await db.execute(
        update(ChatRunOutbox)
        .where(ChatRunOutbox.id == outbox_id)
        .values(published_at=utcnow(), last_error=None)
    )
    await db.commit()


async def mark_outbox_error(db: AsyncSession, outbox_id: int, error: str) -> None:
    await db.execute(
        update(ChatRunOutbox)
        .where(ChatRunOutbox.id == outbox_id)
        .values(last_error=error[:2000])
    )
    await db.commit()


async def cleanup_terminal_events(db: AsyncSession, *, retention_days: int) -> int:
    cutoff = utcnow() - timedelta(days=retention_days)
    terminal_ids = select(ChatRun.id).where(
        ChatRun.status.in_(TERMINAL_STATUSES),
        ChatRun.finished_at < cutoff,
    )
    result = await db.execute(
        delete(ChatRunEvent).where(ChatRunEvent.run_id.in_(terminal_ids))
    )
    await db.commit()
    return int(result.rowcount or 0)


async def expire_pending_approvals(
    db: AsyncSession, *, timeout_seconds: int
) -> int:
    cutoff = utcnow() - timedelta(seconds=timeout_seconds)
    rows = list(
        (
            await db.execute(
                select(ChatRun)
                .where(
                    ChatRun.status == "pending_approval",
                    ChatRun.updated_at < cutoff,
                )
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for run in rows:
        validate_transition(run.status, "failed")
        run.status = "failed"
        run.error_code = "CHAT_RUN_APPROVAL_EXPIRED"
        run.error_message = "工具审批等待已过期"
        run.finished_at = utcnow()
        _append_locked(
            db,
            run,
            "error",
            {"code": run.error_code, "message": run.error_message},
        )
        _append_locked(db, run, "run_status", {"status": "failed"})
        _append_locked(
            db,
            run,
            "run_end",
            {"status": "failed", "duration_ms": 0, "tool_calls": 0},
        )
    await db.commit()
    return len(rows)


async def expire_queued_runs(db: AsyncSession, *, timeout_seconds: int) -> int:
    if timeout_seconds <= 0:
        return 0
    cutoff = utcnow() - timedelta(seconds=timeout_seconds)
    rows = list(
        (
            await db.execute(
                select(ChatRun)
                .where(ChatRun.status == "queued", ChatRun.created_at < cutoff)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for run in rows:
        validate_transition(run.status, "failed")
        run.status = "failed"
        run.error_code = "CHAT_RUN_QUEUE_TIMEOUT"
        run.error_message = "运行排队等待已超时"
        run.finished_at = utcnow()
        _append_locked(db, run, "run_status", {"status": "failed"})
        _append_locked(
            db,
            run,
            "run_end",
            {"status": "failed", "duration_ms": 0, "tool_calls": 0},
        )
    await db.commit()
    return len(rows)
