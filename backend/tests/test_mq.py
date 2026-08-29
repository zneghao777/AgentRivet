"""RabbitMQ 接入单元测试（MQ_INTEGRATION_SPEC §9.1）。"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest

from app.config import settings
from app.mq.envelopes import (
    EnvelopeError,
    decode_envelope,
    make_envelope,
    restore_payload_types,
)


def test_envelope_roundtrip_datetime_uuid_and_none():
    session_id = uuid.uuid4()
    started_at = datetime.now(UTC)
    envelope = make_envelope(
        "obs.span",
        {
            "session_id": session_id,
            "started_at": started_at,
            "optional": None,
        },
    )

    decoded = decode_envelope(envelope.to_bytes())
    payload = restore_payload_types(decoded.payload)

    assert decoded.message_id == envelope.message_id
    assert payload["session_id"] == session_id
    assert payload["started_at"] == started_at
    assert payload["optional"] is None


def test_decode_envelope_rejects_invalid_json():
    with pytest.raises(EnvelopeError):
        decode_envelope(b"not-json")


def test_obs_payload_validation_rejects_missing_span_identity():
    from app.workers.obs_worker import _validate_obs_payload

    with pytest.raises(EnvelopeError, match="trace_id"):
        _validate_obs_payload(make_envelope("obs.span", {"span_id": "span-1"}))


@pytest.mark.asyncio
async def test_obs_consumer_checks_target_table_after_ack_gap(monkeypatch):
    from sqlalchemy import delete, func, select

    from app.storage import repositories as repo
    from app.storage.db import AsyncSessionLocal
    from app.storage.models import ObservabilityLog
    from app.workers import obs_worker

    message_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    async with AsyncSessionLocal() as db:
        await repo.bulk_add_observability_logs(
            db,
            [
                {
                    "record_type": "log",
                    "mq_message_id": message_id,
                    "timestamp": now,
                    "observed_at": now,
                    "body": "already committed",
                    "tenant_id": "mq-test",
                }
            ],
        )

    async def claimed(*args, **kwargs):
        return "claimed"

    async def complete(*args, **kwargs):
        return None

    monkeypatch.setattr(obs_worker.deduplicator, "claim", claimed)
    monkeypatch.setattr(obs_worker.deduplicator, "complete", complete)
    envelope = make_envelope(
        "obs.log",
        {
            "timestamp": now,
            "observed_at": now,
            "body": "already committed",
            "tenant_id": "mq-test",
        },
        message_id=message_id,
    )
    message = _FakeMessage(envelope.to_bytes())

    await obs_worker._persist_batch([("obs.spans.q", message, envelope)])

    async with AsyncSessionLocal() as db:
        count = (
            await db.execute(
                select(func.count()).select_from(ObservabilityLog).where(
                    ObservabilityLog.mq_message_id == message_id
                )
            )
        ).scalar_one()
        await db.execute(
            delete(ObservabilityLog).where(
                ObservabilityLog.mq_message_id == message_id
            )
        )
        await db.commit()
    assert count == 1
    assert message.acked == 1


@pytest.mark.asyncio
async def test_publish_failure_runs_fallback(monkeypatch):
    from app.mq import publisher

    monkeypatch.setattr(settings, "MQ_ENABLED", True)
    monkeypatch.setattr(settings, "MQ_OBS_ENABLED", True)
    fallback_called = asyncio.Event()

    async def fail_publish(_envelope):
        raise TimeoutError("confirm timeout")

    monkeypatch.setattr(publisher, "publish_envelope", fail_publish)
    assert publisher.publish_nowait(
        "obs.span",
        {"trace_id": "t", "span_id": "s"},
        fallback=fallback_called.set,
    )
    await publisher.flush_publish_tasks(timeout=1)

    assert fallback_called.is_set()


@pytest.mark.asyncio
async def test_connection_failures_use_backoff(monkeypatch):
    import aio_pika

    from app.mq.connection import MQConnectionManager

    monkeypatch.setattr(settings, "MQ_ENABLED", True)
    calls = 0

    async def fail_connect(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise ConnectionError("broker down")

    monkeypatch.setattr(aio_pika, "connect_robust", fail_connect)
    manager = MQConnectionManager()
    with pytest.raises(ConnectionError):
        await manager.connect()
    with pytest.raises(RuntimeError, match="重连退避"):
        await manager.connect()

    assert calls == 1


def test_log_export_ignores_amqp_client_logs(monkeypatch):
    import logging

    from app.observability import log_export

    exported = 0

    def capture(_record):
        nonlocal exported
        exported += 1

    monkeypatch.setattr(log_export, "_export_record", capture)
    handler = log_export.ObservabilityLogHandler(logging.WARNING)
    handler.emit(logging.LogRecord("aiormq.connection", logging.ERROR, "", 0, "down", (), None))

    assert exported == 0


class _FakeMessage:
    def __init__(self, body: bytes):
        self.body = body
        self.acked = 0
        self.rejected = 0
        self.nacked = 0

    async def ack(self):
        self.acked += 1

    async def reject(self, *, requeue: bool):
        assert requeue is False
        self.rejected += 1

    async def nack(self, *, requeue: bool):
        assert requeue is True
        self.nacked += 1


@pytest.mark.asyncio
async def test_session_consumer_deduplicates_message(monkeypatch):
    from app.workers import session_worker

    message_id = str(uuid.uuid4())
    envelope = make_envelope(
        "session.postprocess",
        {
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "session_id": str(uuid.uuid4()),
            "session_title": "test",
            "user_msg": "hello",
            "assistant_msg": "world",
        },
        message_id=message_id,
    )
    seen: set[str] = set()
    extracted = 0

    async def claim(mid: str, _message_type: str) -> str:
        if mid in seen:
            return "duplicate"
        seen.add(mid)
        return "claimed"

    async def complete(_mid: str) -> None:
        return None

    async def release(_mid: str) -> None:
        return None

    async def extract(*args, **kwargs):
        nonlocal extracted
        extracted += 1
        return 1

    async def no_span(*args, **kwargs):
        return None

    monkeypatch.setattr(session_worker.deduplicator, "claim", claim)
    monkeypatch.setattr(session_worker.deduplicator, "complete", complete)
    monkeypatch.setattr(session_worker.deduplicator, "release", release)
    monkeypatch.setattr(session_worker.memory_service, "extract_from_conversation", extract)
    monkeypatch.setattr(session_worker, "_write_memory_span", no_span)

    first = _FakeMessage(envelope.to_bytes())
    duplicate = _FakeMessage(envelope.to_bytes())
    await session_worker.process_message(first)
    await session_worker.process_message(duplicate)

    assert extracted == 1
    assert first.acked == 1
    assert duplicate.acked == 1


@pytest.mark.asyncio
async def test_session_consumer_requeues_processing_lease(monkeypatch):
    from app.workers import session_worker

    async def busy(*args, **kwargs):
        return "busy"

    monkeypatch.setattr(session_worker.deduplicator, "claim", busy)
    envelope = make_envelope("session.postprocess", _session_payload())
    message = _FakeMessage(envelope.to_bytes())

    await session_worker.process_message(message)

    assert message.acked == 0
    assert message.nacked == 1


@pytest.mark.asyncio
async def test_session_consumer_deadletters_missing_payload_fields():
    from app.workers import session_worker

    envelope = make_envelope(
        "session.postprocess", {"session_id": str(uuid.uuid4())}
    )
    message = _FakeMessage(envelope.to_bytes())

    await session_worker.process_message(message)

    assert message.rejected == 1


def _session_payload() -> dict[str, str]:
    return {
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "session_id": str(uuid.uuid4()),
        "user_msg": "hello",
        "assistant_msg": "world",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retry_count", "expected_queue"),
    [(0, "session.retry.0.q"), (1, "session.retry.1.q"), (2, "session.retry.2.q")],
)
async def test_session_retry_uses_tiered_queue(
    monkeypatch, retry_count: int, expected_queue: str
):
    from app.workers import session_worker

    monkeypatch.setattr(settings, "MQ_SESSION_MAX_RETRIES", 3)
    monkeypatch.setattr(settings, "MQ_RETRY_TTLS_MS", "5000,30000,120000")
    published: list[tuple[str, bytes]] = []

    async def publish_retry(queue_name: str, body: bytes):
        published.append((queue_name, body))

    monkeypatch.setattr(session_worker.mq_connection, "publish_retry", publish_retry)
    envelope = make_envelope(
        "session.postprocess", {"session_id": str(uuid.uuid4())}, retry_count=retry_count
    )
    message = _FakeMessage(envelope.to_bytes())

    await session_worker._retry_or_deadletter(message, envelope)

    assert published[0][0] == expected_queue
    assert decode_envelope(published[0][1]).retry_count == retry_count + 1
    assert message.acked == 1


@pytest.mark.asyncio
async def test_retry_limit_routes_to_deadletter(monkeypatch):
    from app.workers import session_worker

    monkeypatch.setattr(settings, "MQ_SESSION_MAX_RETRIES", 3)
    called = asyncio.Event()

    async def deadletter(_message, _envelope):
        called.set()

    monkeypatch.setattr(session_worker, "_send_to_deadletter", deadletter)
    envelope = make_envelope(
        "session.postprocess", {"session_id": str(uuid.uuid4())}, retry_count=3
    )

    await session_worker._retry_or_deadletter(
        _FakeMessage(envelope.to_bytes()), envelope
    )

    assert called.is_set()
