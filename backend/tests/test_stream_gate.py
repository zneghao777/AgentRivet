"""SSE admission gate and per-user stream lease tests."""

from __future__ import annotations

import asyncio

import pytest

from app.harness import events
from app.harness.stream_gate import (
    StreamGate,
    StreamQueueFullError,
    StreamQueueTimeoutError,
    SessionStreamLock,
)


def test_sse_error_includes_optional_retry_delay():
    assert '"retry_after_ms": 30000' in events.error(
        50300,
        "模型服务暂时不可用，请稍后重试",
        retry_after_ms=30000,
    )
    assert "retry_after_ms" not in events.error(50000, "stream error")


@pytest.mark.asyncio
async def test_stream_gate_admits_immediately_and_releases_once():
    gate = StreamGate(max_streams=1, queue_capacity=1)

    ticket = await gate.acquire()

    assert gate.inflight == 1
    assert not ticket.queued
    gate.release(ticket)
    gate.release(ticket)
    assert gate.inflight == 0


@pytest.mark.asyncio
async def test_stream_gate_reports_fifo_positions_and_promotes_waiters():
    gate = StreamGate(
        max_streams=1,
        queue_capacity=2,
        queue_timeout_seconds=1,
        position_push_interval_seconds=0.5,
    )
    first = await gate.acquire()
    first_positions: list[tuple[int, int, int]] = []
    second_positions: list[tuple[int, int, int]] = []

    async def record_first(position: int, size: int, wait_ms: int) -> None:
        first_positions.append((position, size, wait_ms))

    async def record_second(position: int, size: int, wait_ms: int) -> None:
        second_positions.append((position, size, wait_ms))

    first_waiter = asyncio.create_task(gate.acquire(record_first))
    await asyncio.sleep(0)
    second_waiter = asyncio.create_task(gate.acquire(record_second))
    await asyncio.sleep(0.01)

    assert first_positions[0][:2] == (1, 1)
    assert second_positions[0][:2] == (2, 2)
    assert first_positions[0][2] > 0

    gate.release(first)
    admitted_first = await asyncio.wait_for(first_waiter, timeout=0.5)
    await asyncio.sleep(0.01)
    assert any(position == 1 for position, _, _ in second_positions)

    gate.release(admitted_first)
    admitted_second = await asyncio.wait_for(second_waiter, timeout=0.5)
    assert admitted_first.queued and admitted_second.queued
    assert admitted_first.wait_ms >= 0
    gate.release(admitted_second)
    assert gate.inflight == 0
    assert gate.queue_size == 0


@pytest.mark.asyncio
async def test_stream_gate_rejects_full_queue_and_times_out_cleanly():
    gate = StreamGate(
        max_streams=1,
        queue_capacity=1,
        queue_timeout_seconds=0.02,
        position_push_interval_seconds=0.01,
    )
    active = await gate.acquire()
    queued = asyncio.create_task(gate.acquire())
    await asyncio.sleep(0)

    with pytest.raises(StreamQueueFullError):
        await gate.acquire()
    with pytest.raises(StreamQueueTimeoutError):
        await queued

    assert gate.queue_size == 0
    gate.release(active)
    assert gate.inflight == 0


@pytest.mark.asyncio
async def test_stream_gate_cancelled_waiter_does_not_consume_slot():
    gate = StreamGate(max_streams=1, queue_capacity=1)
    active = await gate.acquire()
    queued = asyncio.create_task(gate.acquire())
    await asyncio.sleep(0)

    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued

    assert gate.queue_size == 0
    gate.release(active)
    assert gate.inflight == 0


@pytest.mark.asyncio
async def test_user_stream_lock_memory_fallback_expires_and_compares_owner():
    now = 10.0
    lock = SessionStreamLock(ttl_seconds=3, clock=lambda: now)

    assert await lock.acquire("tenant", "user", "session-a", "request-a")
    assert await lock.acquire("tenant", "user", "session-b", "request-b")
    assert not await lock.acquire("tenant", "user", "session-a", "request-b")
    assert not await lock.release("tenant", "user", "session-a", "request-b")
    assert await lock.renew("tenant", "user", "session-a", "request-a")

    now = 14.0
    assert await lock.acquire("tenant", "user", "session-a", "request-b")
    assert not await lock.release("tenant", "user", "session-a", "request-a")
    assert await lock.release("tenant", "user", "session-a", "request-b")


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}
        self.fail = False

    async def set(self, key, value, *, nx=False, ex=None):
        if self.fail:
            raise ConnectionError("redis unavailable")
        if nx and key in self.values:
            return False
        self.values[key] = value
        self.expirations[key] = ex
        return True

    async def eval(self, script, _key_count, key, request_id, *args):
        if self.fail:
            raise ConnectionError("redis unavailable")
        if self.values.get(key) != request_id:
            return 0
        if "EXPIRE" in script:
            self.expirations[key] = int(args[0])
            return 1
        self.values.pop(key, None)
        self.expirations.pop(key, None)
        return 1


@pytest.mark.asyncio
async def test_user_stream_lock_redis_uses_compare_renew_and_delete():
    redis = _FakeRedis()
    lock = SessionStreamLock(redis_client=redis, ttl_seconds=180)
    key = lock.key("tenant", "user", "session-a")

    assert await lock.acquire("tenant", "user", "session-a", "request-a")
    assert redis.values[key] == "request-a"
    assert not await lock.acquire("tenant", "user", "session-a", "request-b")
    assert not await lock.renew("tenant", "user", "session-a", "request-b")
    assert not await lock.release("tenant", "user", "session-a", "request-b")
    assert redis.values[key] == "request-a"
    assert await lock.renew("tenant", "user", "session-a", "request-a")
    assert redis.expirations[key] == 180
    assert await lock.release("tenant", "user", "session-a", "request-a")
    assert key not in redis.values


@pytest.mark.asyncio
async def test_user_stream_lock_falls_back_when_redis_operation_fails():
    redis = _FakeRedis()
    redis.fail = True
    lock = SessionStreamLock(redis_client=redis, ttl_seconds=180)

    assert await lock.acquire("tenant", "user", "session-a", "request-a")
    assert not await lock.acquire("tenant", "user", "session-a", "request-b")
    assert await lock.release("tenant", "user", "session-a", "request-a")
