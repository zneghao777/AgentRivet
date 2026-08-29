"""观测 Worker：批量消费 span/log/audit 并在成功落库后手动 ack。"""

from __future__ import annotations

import asyncio
import logging
import signal
import uuid
from datetime import datetime
from typing import Any

from ..config import settings
from ..mq.connection import OBS_AUDIT_QUEUE, OBS_SPANS_QUEUE, mq_connection
from ..mq.dedupe import deduplicator
from ..mq.envelopes import EnvelopeError, decode_envelope, restore_payload_types
from ..observability.metrics import metrics
from ..storage import repositories as repo
from ..storage.db import AsyncSessionLocal, create_tables
from ..storage.models import AuditLog, ObservabilityLog

logger = logging.getLogger(__name__)


def _validate_obs_payload(envelope: Any) -> None:
    payload = envelope.payload
    required: tuple[str, ...] = ()
    if envelope.type == "obs.span":
        required = ("trace_id", "span_id")
    elif envelope.type == "obs.audit":
        required = ("tenant_id", "action")
    missing = [field for field in required if not payload.get(field)]
    if missing:
        raise EnvelopeError(f"观测消息缺少字段: {', '.join(missing)}")
    session_id = payload.get("session_id")
    if session_id is not None:
        try:
            uuid.UUID(str(session_id))
        except (TypeError, ValueError) as exc:
            raise EnvelopeError("session_id 不是有效 UUID") from exc
    for field in ("started_at", "ended_at", "timestamp", "observed_at"):
        value = payload.get(field)
        if isinstance(value, str):
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise EnvelopeError(f"{field} 不是有效 ISO8601 时间") from exc


async def _persist_batch(entries: list[tuple[str, Any, Any]]) -> None:
    claimed: list[tuple[str, Any, Any]] = []
    for queue_name, message, envelope in entries:
        claim = await deduplicator.claim(envelope.message_id, envelope.type)
        if claim == "duplicate":
            metrics.record_mq_consume(queue_name, "duplicate")
            await message.ack()
            continue
        if claim == "busy":
            metrics.record_mq_consume(queue_name, "busy")
            await asyncio.sleep(0.05)
            await message.nack(requeue=True)
            continue
        claimed.append((queue_name, message, envelope))
    if not claimed:
        return

    spans: list[dict[str, Any]] = []
    logs: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    for _, _, envelope in claimed:
        payload = restore_payload_types(envelope.payload)
        if envelope.type == "obs.span":
            spans.append({"record_type": "span", **payload})
        elif envelope.type == "obs.log":
            logs.append(
                {
                    "record_type": "log",
                    **payload,
                    "mq_message_id": envelope.message_id,
                }
            )
        elif envelope.type == "obs.audit":
            audits.append({**payload, "mq_message_id": envelope.message_id})

    try:
        async with AsyncSessionLocal() as db:
            from sqlalchemy import select

            candidate_ids = [envelope.message_id for _, _, envelope in claimed]
            existing_ids = set(
                (
                    await db.execute(
                        select(ObservabilityLog.mq_message_id).where(
                            ObservabilityLog.mq_message_id.in_(candidate_ids)
                        )
                    )
                ).scalars().all()
            )
            existing_ids.update(
                (
                    await db.execute(
                        select(AuditLog.mq_message_id).where(
                            AuditLog.mq_message_id.in_(candidate_ids)
                        )
                    )
                ).scalars().all()
            )
            logs = [item for item in logs if item["mq_message_id"] not in existing_ids]
            audits = [item for item in audits if item["mq_message_id"] not in existing_ids]
            if spans:
                await repo.bulk_upsert_spans(db, spans)
            if logs:
                await repo.bulk_add_observability_logs(db, logs)
            for item in audits:
                await repo.add_audit(db, **item)
    except Exception:  # noqa: BLE001
        logger.exception("观测批次写入失败，消息重新入队")
        for queue_name, message, envelope in claimed:
            await deduplicator.release(envelope.message_id)
            metrics.record_mq_consume(queue_name, "failed")
            await message.nack(requeue=True)
        return

    for queue_name, message, envelope in claimed:
        await deduplicator.complete(envelope.message_id)
        result = "duplicate" if envelope.message_id in existing_ids else "success"
        metrics.record_mq_consume(queue_name, result)
        await message.ack()


async def _run(stop: asyncio.Event) -> None:
    await create_tables()
    channel = await mq_connection.open_consumer_channel()
    incoming: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    consumers: list[tuple[Any, str]] = []

    async def bind(queue_name: str) -> None:
        queue = await channel.get_queue(queue_name, ensure=True)

        async def on_message(message: Any) -> None:
            await incoming.put((queue_name, message))

        tag = await queue.consume(on_message, no_ack=False)
        consumers.append((queue, tag))

    await bind(OBS_SPANS_QUEUE)
    await bind(OBS_AUDIT_QUEUE)
    logger.info("obs-worker 已启动 prefetch=%d", settings.MQ_PREFETCH)

    try:
        while not stop.is_set():
            try:
                first = await asyncio.wait_for(incoming.get(), timeout=0.5)
            except TimeoutError:
                continue
            raw: list[tuple[str, Any]] = [first]
            deadline = asyncio.get_running_loop().time() + settings.OBS_WRITE_BATCH_MS / 1000
            while len(raw) < settings.OBS_WRITE_BATCH_MAX:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    raw.append(await asyncio.wait_for(incoming.get(), timeout=remaining))
                except TimeoutError:
                    break

            valid: list[tuple[str, Any, Any]] = []
            for queue_name, message in raw:
                try:
                    envelope = decode_envelope(message.body)
                    if envelope.type not in {"obs.span", "obs.log", "obs.audit"}:
                        raise EnvelopeError("消息类型不属于观测队列")
                    _validate_obs_payload(envelope)
                except EnvelopeError as exc:
                    logger.warning("非法观测消息进入死信 queue=%s: %s", queue_name, exc)
                    metrics.record_mq_deadletter(queue_name)
                    await message.reject(requeue=False)
                    continue
                valid.append((queue_name, message, envelope))
            if valid:
                await _persist_batch(valid)
    finally:
        for queue, tag in consumers:
            await queue.cancel(tag)
        if not incoming.empty():
            pending: list[tuple[str, Any, Any]] = []
            while not incoming.empty():
                queue_name, message = incoming.get_nowait()
                try:
                    pending.append((queue_name, message, decode_envelope(message.body)))
                except EnvelopeError:
                    await message.reject(requeue=False)
            if pending:
                await _persist_batch(pending)
        await deduplicator.close()
        await mq_connection.close()


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not settings.MQ_ENABLED:
        raise RuntimeError("obs-worker 需要 MQ_ENABLED=true")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await _run(stop)


if __name__ == "__main__":
    asyncio.run(main())
