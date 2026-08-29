"""会话收尾 Worker：记忆提炼、幂等、分级重试与死信。"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from ..config import settings
from ..memory.service import memory_service
from ..mq.connection import (
    SESSION_DLX,
    SESSION_QUEUE,
    mq_connection,
    retry_queue_name,
)
from ..mq.dedupe import deduplicator
from ..mq.envelopes import (
    EnvelopeError,
    MessageEnvelope,
    decode_envelope,
    make_envelope,
    trace_context_from_envelope,
)
from ..observability.metrics import metrics
from ..observability.tracing import set_trace_context
from ..storage import repositories as repo
from ..storage.db import AsyncSessionLocal, create_tables

logger = logging.getLogger(__name__)
_SESSION_REQUIRED_FIELDS = (
    "tenant_id",
    "user_id",
    "session_id",
    "user_msg",
    "assistant_msg",
)


def _validate_session_payload(payload: dict[str, Any]) -> None:
    missing = [field for field in _SESSION_REQUIRED_FIELDS if field not in payload]
    if missing:
        raise EnvelopeError(f"会话消息缺少字段: {', '.join(missing)}")
    try:
        uuid.UUID(str(payload["session_id"]))
    except (TypeError, ValueError) as exc:
        raise EnvelopeError("session_id 不是有效 UUID") from exc


async def _write_memory_span(
    envelope: MessageEnvelope, *, ok: bool, duration_ms: int, error: str | None
) -> None:
    ctx = trace_context_from_envelope(envelope)
    if ctx is None:
        return
    now = datetime.now(UTC)
    payload = envelope.payload
    async with AsyncSessionLocal() as db:
        await repo.bulk_upsert_spans(
            db,
            [
                {
                    "record_type": "span",
                    "trace_id": ctx.trace_id,
                    "span_id": uuid.uuid4().hex[:16],
                    "parent_span_id": ctx.current_span_id,
                    "tenant_id": payload.get("tenant_id", ctx.tenant_id),
                    "session_id": uuid.UUID(str(payload["session_id"])),
                    "user_id": payload.get("user_id"),
                    "span_name": "memory.extract",
                    "span_type": "memory",
                    "status": "ok" if ok else "error",
                    "duration_ms": duration_ms,
                    "started_at": now,
                    "ended_at": now,
                    "error": error,
                    "request_id": ctx.request_id,
                    "retry_count": envelope.retry_count,
                }
            ],
        )


async def _safe_write_memory_span(
    envelope: MessageEnvelope, *, ok: bool, duration_ms: int, error: str | None
) -> None:
    try:
        await _write_memory_span(
            envelope, ok=ok, duration_ms=duration_ms, error=error
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory.extract span 写入失败，不影响消息语义: %s", exc)


async def _send_to_deadletter(message: Any, envelope: MessageEnvelope) -> None:
    try:
        await mq_connection.publish(SESSION_DLX, "deadletter", envelope.to_bytes())
        await message.ack()
    except Exception:  # noqa: BLE001
        logger.exception("显式发布死信失败，交由原队列 DLX")
        await message.reject(requeue=False)
    metrics.record_mq_deadletter(SESSION_QUEUE)


async def _retry_or_deadletter(message: Any, envelope: MessageEnvelope) -> None:
    if envelope.retry_count >= settings.MQ_SESSION_MAX_RETRIES:
        await _send_to_deadletter(message, envelope)
        return
    next_retry = envelope.retry_count + 1
    retry_envelope = make_envelope(
        envelope.type,
        envelope.payload,
        retry_count=next_retry,
        message_id=envelope.message_id,
        trace=envelope.trace,
    )
    index = min(envelope.retry_count, len(settings.mq_retry_ttls_ms_list) - 1)
    try:
        await mq_connection.publish_retry(retry_queue_name(index), retry_envelope.to_bytes())
        await message.ack()
    except Exception:  # noqa: BLE001
        logger.exception("发布重试消息失败，保留原消息")
        await message.nack(requeue=True)


async def process_message(message: Any) -> None:
    try:
        envelope = decode_envelope(message.body)
        if envelope.type != "session.postprocess":
            raise EnvelopeError("消息类型不属于会话收尾队列")
        _validate_session_payload(envelope.payload)
    except EnvelopeError as exc:
        logger.warning("非法会话消息进入死信: %s", exc)
        metrics.record_mq_deadletter(SESSION_QUEUE)
        await message.reject(requeue=False)
        return

    claim = await deduplicator.claim(envelope.message_id, envelope.type)
    if claim == "duplicate":
        metrics.record_mq_consume(SESSION_QUEUE, "duplicate")
        await message.ack()
        return
    if claim == "busy":
        metrics.record_mq_consume(SESSION_QUEUE, "busy")
        await asyncio.sleep(0.1)
        await message.nack(requeue=True)
        return

    payload = envelope.payload
    ctx = trace_context_from_envelope(envelope)
    set_trace_context(ctx)
    started = time.monotonic()
    lease_task = asyncio.create_task(_renew_lease(envelope.message_id))
    try:
        session_id = uuid.UUID(str(payload["session_id"]))
        async with AsyncSessionLocal() as db:
            written = await memory_service.extract_from_conversation(
                db,
                tenant_id=str(payload["tenant_id"]),
                user_id=str(payload["user_id"]),
                user_msg=str(payload["user_msg"]),
                assistant_msg=str(payload["assistant_msg"]),
                session_id=session_id,
                session_title=payload.get("session_title"),
            )
        duration_ms = int((time.monotonic() - started) * 1000)
        await deduplicator.complete(envelope.message_id)
        await _safe_write_memory_span(
            envelope, ok=True, duration_ms=duration_ms, error=None
        )
        metrics.record_mq_consume(SESSION_QUEUE, "success")
        logger.info(
            "会话收尾完成 message_id=%s written=%d retry=%d",
            envelope.message_id,
            written,
            envelope.retry_count,
        )
        await message.ack()
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - started) * 1000)
        logger.warning("会话收尾失败 message_id=%s: %s", envelope.message_id, exc)
        await deduplicator.release(envelope.message_id)
        await _safe_write_memory_span(
            envelope, ok=False, duration_ms=duration_ms, error=str(exc)[:200]
        )
        metrics.record_mq_consume(SESSION_QUEUE, "failed")
        await _retry_or_deadletter(message, envelope)
    finally:
        lease_task.cancel()
        try:
            await lease_task
        except asyncio.CancelledError:
            pass
        set_trace_context(None)


async def _renew_lease(message_id: str) -> None:
    while True:
        await asyncio.sleep(10)
        try:
            await deduplicator.renew(message_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("MQ 幂等租约续期失败 message_id=%s: %s", message_id, exc)


async def _run(stop: asyncio.Event) -> None:
    await create_tables()
    channel = await mq_connection.open_consumer_channel()
    queue = await channel.get_queue(SESSION_QUEUE, ensure=True)
    semaphore = asyncio.Semaphore(3)
    tasks: set[asyncio.Task[None]] = set()

    async def guarded(message: Any) -> None:
        async with semaphore:
            await process_message(message)

    async def on_message(message: Any) -> None:
        task = asyncio.create_task(guarded(message))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    tag = await queue.consume(on_message, no_ack=False)
    logger.info("session-worker 已启动 prefetch=%d", settings.MQ_PREFETCH)
    await stop.wait()
    await queue.cancel(tag)
    if tasks:
        done, pending = await asyncio.wait(tuple(tasks), timeout=10)
        for task in done:
            task.result()
        for task in pending:
            task.cancel()
    await deduplicator.close()
    await mq_connection.close()


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not settings.MQ_ENABLED:
        raise RuntimeError("session-worker 需要 MQ_ENABLED=true")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await _run(stop)


if __name__ == "__main__":
    asyncio.run(main())
