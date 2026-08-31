"""SSE stream admission and per-user stream leases.

The gate is intentionally process-local for the first delivery stage.  The user
lease prefers Redis so the one-stream-per-user rule also works across API
replicas, and falls back to an expiring in-memory lease when Redis is absent or
unavailable.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..config import settings

logger = logging.getLogger(__name__)

PositionCallback = Callable[[int, int, int], Awaitable[None] | None]


class StreamGateError(RuntimeError):
    """Base class for admission failures that are safe to show to a client."""


class StreamQueueFullError(StreamGateError):
    """Raised when no stream slot or queue position is available."""


class StreamQueueTimeoutError(StreamGateError):
    """Raised when a queued request did not reach a stream slot in time."""


@dataclass(frozen=True, slots=True)
class GuardTicket:
    """An admitted stream.  Only the gate that issued it may release it."""

    ticket_id: str
    admitted_at: float
    queued_at: float | None = None

    @property
    def queued(self) -> bool:
        return self.queued_at is not None

    @property
    def wait_ms(self) -> int:
        if self.queued_at is None:
            return 0
        return max(0, int((self.admitted_at - self.queued_at) * 1000))


@dataclass(slots=True)
class _Waiter:
    ticket_id: str
    queued_at: float
    callback: PositionCallback | None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    ticket: GuardTicket | None = None


class StreamGate:
    """FIFO SSE admission: immediate slot, bounded queue, then timeout."""

    def __init__(
        self,
        max_streams: int,
        queue_capacity: int,
        *,
        queue_timeout_seconds: float = 60.0,
        position_push_interval_seconds: float = 5.0,
        initial_service_seconds: float = 6.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_streams < 1:
            raise ValueError("max_streams must be at least 1")
        if queue_capacity < 0:
            raise ValueError("queue_capacity cannot be negative")
        self.max_streams = max_streams
        self.queue_capacity = queue_capacity
        self.queue_timeout_seconds = max(0.0, queue_timeout_seconds)
        self.position_push_interval_seconds = max(
            0.05, position_push_interval_seconds
        )
        self._clock = clock
        self._inflight = 0
        self._waiters: deque[_Waiter] = deque()
        self._active: dict[str, GuardTicket] = {}
        self._average_service_seconds = max(0.05, initial_service_seconds)

    @property
    def inflight(self) -> int:
        return self._inflight

    @property
    def queue_size(self) -> int:
        return len(self._waiters)

    @property
    def utilization(self) -> float:
        return self._inflight / self.max_streams

    async def acquire(
        self,
        on_position_change: PositionCallback | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> GuardTicket:
        """Acquire a stream slot, reporting queue position while waiting."""
        now = self._clock()
        ticket_id = uuid.uuid4().hex
        # No await occurs while the state is changed.  All route calls run on the
        # same event loop, so this block is atomic without an asyncio.Lock.
        if self._inflight < self.max_streams and not self._waiters:
            ticket = GuardTicket(ticket_id=ticket_id, admitted_at=now)
            self._active[ticket_id] = ticket
            self._inflight += 1
            return ticket
        if len(self._waiters) >= self.queue_capacity:
            raise StreamQueueFullError("stream queue is full")

        waiter = _Waiter(
            ticket_id=ticket_id,
            queued_at=now,
            callback=on_position_change,
        )
        self._waiters.append(waiter)
        self._signal_positions()
        timeout = (
            self.queue_timeout_seconds
            if timeout_seconds is None
            else max(0.0, timeout_seconds)
        )
        deadline = now + timeout

        try:
            while waiter.ticket is None:
                await self._report_position(waiter)
                if waiter.ticket is not None:
                    break
                remaining = deadline - self._clock()
                if remaining <= 0:
                    self._remove_waiter(waiter)
                    raise StreamQueueTimeoutError("stream queue wait timed out")
                waiter.changed.clear()
                if waiter.ticket is not None:
                    break
                try:
                    await asyncio.wait_for(
                        waiter.changed.wait(),
                        timeout=min(
                            remaining, self.position_push_interval_seconds
                        ),
                    )
                except TimeoutError:
                    # A periodic position update is required even when the queue
                    # did not move.
                    pass
            if waiter.ticket is None:  # pragma: no cover - defensive invariant
                raise RuntimeError("admitted waiter has no ticket")
            return waiter.ticket
        except BaseException:
            # Cancellation can race with promotion.  If a slot was already
            # granted, release it immediately; otherwise remove the queue entry.
            if waiter.ticket is not None:
                self.release(waiter.ticket)
            else:
                self._remove_waiter(waiter)
            raise

    def release(self, ticket: GuardTicket) -> None:
        """Release an admitted ticket once; stale or duplicate releases are no-ops."""
        active = self._active.pop(ticket.ticket_id, None)
        if active is None:
            return
        self._inflight = max(0, self._inflight - 1)
        elapsed = max(0.0, self._clock() - active.admitted_at)
        if elapsed:
            # Smooth enough for a useful ETA without making one outlier dominate.
            self._average_service_seconds = (
                self._average_service_seconds * 0.8 + elapsed * 0.2
            )
        self._promote_waiters()

    async def _report_position(self, waiter: _Waiter) -> None:
        if waiter.callback is None or waiter.ticket is not None:
            return
        try:
            position = self._waiters.index(waiter) + 1
        except ValueError:
            return
        queue_size = len(self._waiters)
        batches = math.ceil(position / self.max_streams)
        estimated_wait_ms = max(
            1, int(batches * self._average_service_seconds * 1000)
        )
        try:
            result = waiter.callback(position, queue_size, estimated_wait_ms)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001
            logger.exception("SSE queue position callback failed")

    def _remove_waiter(self, waiter: _Waiter) -> None:
        try:
            self._waiters.remove(waiter)
        except ValueError:
            return
        self._signal_positions()

    def _promote_waiters(self) -> None:
        while self._inflight < self.max_streams and self._waiters:
            waiter = self._waiters.popleft()
            ticket = GuardTicket(
                ticket_id=waiter.ticket_id,
                admitted_at=self._clock(),
                queued_at=waiter.queued_at,
            )
            waiter.ticket = ticket
            self._active[ticket.ticket_id] = ticket
            self._inflight += 1
            waiter.changed.set()
        self._signal_positions()

    def _signal_positions(self) -> None:
        for waiter in self._waiters:
            waiter.changed.set()


_RENEW_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_DELETE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class SessionStreamLock:
    """A compare-and-delete session stream lease with Redis/memory fallback."""

    def __init__(
        self,
        *,
        redis_url: str | None = None,
        ttl_seconds: int = 180,
        enabled: bool = True,
        redis_client: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.enabled = enabled
        self.ttl_seconds = max(1, ttl_seconds)
        self.renew_interval_seconds = max(0.05, self.ttl_seconds / 3)
        self._redis_url = redis_url
        self._redis = redis_client
        self._redis_injected = redis_client is not None
        self._redis_failed = False
        self._connect_lock = asyncio.Lock()
        self._memory_lock = asyncio.Lock()
        self._memory: dict[str, tuple[str, float]] = {}
        self._clock = clock

    @staticmethod
    def key(tenant_id: str, user_id: str, session_id: str) -> str:
        return f"session_stream_lock:{tenant_id}:{user_id}:{session_id}"

    async def acquire(
        self, tenant_id: str, user_id: str, session_id: str, request_id: str
    ) -> bool:
        """Claim the session lease, or return ``False`` when another run owns it."""
        if not self.enabled:
            return True
        key = self.key(tenant_id, user_id, session_id)
        redis = await self._redis_client()
        if redis is not None:
            try:
                claimed = bool(
                    await redis.set(
                        key,
                        request_id,
                        nx=True,
                        ex=self.ttl_seconds,
                    )
                )
                if claimed:
                    await self._memory_set(key, request_id)
                return claimed
            except Exception as exc:  # noqa: BLE001
                await self._disable_redis(exc)
        return await self._memory_acquire(key, request_id)

    async def renew(
        self, tenant_id: str, user_id: str, session_id: str, request_id: str
    ) -> bool:
        """Extend only the lease owned by ``request_id``."""
        if not self.enabled:
            return True
        key = self.key(tenant_id, user_id, session_id)
        redis = await self._redis_client()
        if redis is not None:
            try:
                renewed = bool(
                    await redis.eval(
                        _RENEW_SCRIPT,
                        1,
                        key,
                        request_id,
                        self.ttl_seconds,
                    )
                )
                if renewed:
                    await self._memory_set(key, request_id)
                else:
                    await self._memory_release(key, request_id)
                return renewed
            except Exception as exc:  # noqa: BLE001
                await self._disable_redis(exc)
        return await self._memory_renew(key, request_id)

    async def release(
        self, tenant_id: str, user_id: str, session_id: str, request_id: str
    ) -> bool:
        """Delete the lease only when its value still matches ``request_id``."""
        if not self.enabled:
            return True
        key = self.key(tenant_id, user_id, session_id)
        released = False
        redis = await self._redis_client()
        if redis is not None:
            try:
                released = bool(
                    await redis.eval(_DELETE_SCRIPT, 1, key, request_id)
                )
            except Exception as exc:  # noqa: BLE001
                await self._disable_redis(exc)
        memory_released = await self._memory_release(key, request_id)
        return released or memory_released

    async def close(self) -> None:
        redis = self._redis
        self._redis = None
        if redis is not None and not self._redis_injected:
            close = getattr(redis, "aclose", None)
            if close is not None:
                await close()

    async def _redis_client(self) -> Any | None:
        if self._redis_failed:
            return None
        if self._redis is not None:
            return self._redis
        if not self._redis_url:
            return None
        async with self._connect_lock:
            if self._redis is not None:
                return self._redis
            if self._redis_failed:
                return None
            try:
                from redis.asyncio import from_url

                redis = from_url(
                    self._redis_url,
                    decode_responses=True,
                    socket_connect_timeout=0.5,
                    socket_timeout=0.5,
                )
                await redis.ping()
                self._redis = redis
            except Exception as exc:  # noqa: BLE001
                await self._disable_redis(exc)
        return self._redis

    async def _disable_redis(self, exc: Exception) -> None:
        if not self._redis_failed:
            logger.warning("用户流锁 Redis 不可用，降级为进程内租约: %s", exc)
        self._redis_failed = True
        redis = self._redis
        self._redis = None
        if redis is not None and not self._redis_injected:
            close = getattr(redis, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    pass

    async def _memory_acquire(self, key: str, request_id: str) -> bool:
        async with self._memory_lock:
            now = self._clock()
            current = self._memory.get(key)
            if current is not None and current[1] > now:
                return False
            self._memory[key] = (request_id, now + self.ttl_seconds)
            return True

    async def _memory_set(self, key: str, request_id: str) -> None:
        async with self._memory_lock:
            self._memory[key] = (
                request_id,
                self._clock() + self.ttl_seconds,
            )

    async def _memory_renew(self, key: str, request_id: str) -> bool:
        async with self._memory_lock:
            now = self._clock()
            current = self._memory.get(key)
            if current is None or current[0] != request_id or current[1] <= now:
                if current is not None and current[1] <= now:
                    self._memory.pop(key, None)
                return False
            self._memory[key] = (request_id, now + self.ttl_seconds)
            return True

    async def _memory_release(self, key: str, request_id: str) -> bool:
        async with self._memory_lock:
            current = self._memory.get(key)
            if current is None or current[0] != request_id:
                return False
            self._memory.pop(key, None)
            return True


stream_gate = StreamGate(
    max_streams=getattr(settings, "STREAM_MAX_CONCURRENCY", 240),
    queue_capacity=(
        getattr(settings, "QUEUE_CAPACITY", 50)
        if getattr(settings, "QUEUE_ENABLED", True)
        else 0
    ),
    queue_timeout_seconds=getattr(settings, "QUEUE_TIMEOUT_SECONDS", 30.0),
    position_push_interval_seconds=getattr(
        settings, "QUEUE_POSITION_PUSH_INTERVAL_S", 5.0
    ),
)

session_stream_lock = SessionStreamLock(
    redis_url=settings.REDIS_URL,
    ttl_seconds=getattr(settings, "USER_STREAM_LOCK_TTL_S", 180),
    enabled=getattr(settings, "USER_STREAM_LOCK_ENABLED", True),
)

# 一个兼容版本内保留旧导入名；锁语义和键已经是会话级。
UserStreamLock = SessionStreamLock
user_stream_lock = session_stream_lock
