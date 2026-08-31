"""LLM upstream concurrency, circuit-breaker, and degradation guardrails.

The guard is intentionally provider-independent.  Every model created through
``agents.base_agent.create_llm`` is wrapped here and shares one of two process
local pools: interactive traffic uses ``online`` and maintenance work uses
``background``.
"""

from __future__ import annotations

import asyncio
import atexit
import math
import threading
import time
import weakref
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any, Literal, NoReturn

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import (
    BaseChatModel,
    agenerate_from_stream,
    generate_from_stream,
)
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from pydantic import ConfigDict, Field

from ..config import settings

CallChannel = Literal["online", "background"]
EndpointKey = tuple[str, str]


class LLMGuardError(RuntimeError):
    """Base exception carrying the stable SSE-facing error code."""

    code: int

    def __init__(
        self,
        message: str,
        *,
        code: int,
        reason: str,
        retry_after_ms: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.retry_after_ms = retry_after_ms
        self.rejection_recorded = False


class LLMBusyError(LLMGuardError):
    """Raised when an LLM pool slot cannot be acquired before the deadline."""

    def __init__(self, message: str = "当前使用人数较多，请稍后重试") -> None:
        super().__init__(
            message,
            code=50400,
            reason="llm_slot_timeout",
            retry_after_ms=5_000,
        )


class LLMOutcomeTimeoutError(LLMGuardError):
    """Raised when no user-visible feedback is produced by the absolute deadline."""

    def __init__(
        self,
        message: str = "请求未能在规定时间内产生可见反馈，请稍后重试",
    ) -> None:
        super().__init__(
            message,
            code=50401,
            reason="first_feedback_timeout",
            retry_after_ms=5_000,
        )


class LLMResponseTimeoutError(LLMGuardError):
    """Raised when an admitted graph stops producing model/runtime events."""

    def __init__(self, message: str = "模型响应超时，请稍后重试") -> None:
        super().__init__(
            message,
            code=50402,
            reason="model_response_timeout",
            retry_after_ms=5_000,
        )


class LLMGuardOpenError(LLMGuardError):
    """Raised when every configured model endpoint is circuit-open."""

    def __init__(
        self,
        message: str = "模型服务暂时不可用，请稍后重试",
        *,
        retry_after_ms: int,
        reason: str = "cb_open",
    ) -> None:
        super().__init__(
            message,
            code=50300,
            reason=reason,
            retry_after_ms=retry_after_ms,
        )


def _metric(method: str, *args: Any) -> None:
    """Call an optional in-process metric without coupling guard startup to metrics."""

    try:
        from ..observability.metrics import metrics

        callback = getattr(metrics, method, None)
        if callback is not None:
            callback(*args)
    except Exception:  # noqa: BLE001 - guardrails must not fail because telemetry failed
        return


@dataclass(frozen=True)
class DegradeSnapshot:
    """Immutable degradation decision captured when a request starts."""

    level: int
    effects: tuple[str, ...]

    @property
    def max_tokens_factor(self) -> float:
        return 0.5 if self.level >= 1 else 1.0

    @property
    def rag_top_k(self) -> int:
        return min(settings.RAG_TOP_K, 3) if self.level >= 2 else settings.RAG_TOP_K


_DEGRADE_EFFECTS: dict[int, tuple[str, ...]] = {
    0: (),
    1: ("thinking_disabled", "max_tokens_reduced"),
    2: ("thinking_disabled", "max_tokens_reduced", "context_trimmed", "rag_top_k_reduced"),
}


class DegradeController:
    """Hysteresis controller for the D0/D1/D2 overload ladder."""

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        high_watermark: float | None = None,
        low_watermark: float | None = None,
        enter_hold_s: float | None = None,
        exit_hold_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._enabled = enabled
        self._high_watermark = high_watermark
        self._low_watermark = low_watermark
        self._enter_hold_s = enter_hold_s
        self._exit_hold_s = exit_hold_s
        self._clock = clock
        self._level = 0
        self._high_since: float | None = None
        self._d2_since: float | None = None
        self._low_since: float | None = None
        self._lock = threading.Lock()

    @property
    def level(self) -> int:
        with self._lock:
            return self._level

    def snapshot(self) -> DegradeSnapshot:
        level = self.level
        return DegradeSnapshot(level=level, effects=_DEGRADE_EFFECTS[level])

    def update(self, utilization: float, *, now: float | None = None) -> DegradeSnapshot:
        """Observe online-pool utilization and return the current stable level."""

        observed_at = self._clock() if now is None else now
        enabled = settings.DEGRADE_ENABLED if self._enabled is None else self._enabled
        high = (
            settings.DEGRADE_HIGH_WATERMARK
            if self._high_watermark is None
            else self._high_watermark
        )
        low = settings.DEGRADE_LOW_WATERMARK if self._low_watermark is None else self._low_watermark
        enter_hold = (
            float(settings.DEGRADE_ENTER_HOLD_S)
            if self._enter_hold_s is None
            else self._enter_hold_s
        )
        exit_hold = (
            float(settings.DEGRADE_EXIT_HOLD_S) if self._exit_hold_s is None else self._exit_hold_s
        )

        changed_level: int | None = None
        with self._lock:
            if not enabled:
                if self._level != 0:
                    self._level = 0
                    changed_level = 0
                self._clear_timers()
            elif self._level == 0:
                self._low_since = None
                if utilization > high:
                    if self._high_since is None:
                        self._high_since = observed_at
                    if observed_at - self._high_since >= enter_hold:
                        self._level = 1
                        changed_level = 1
                        self._high_since = None
                        self._d2_since = observed_at if utilization > 0.9 else None
                else:
                    self._high_since = None
            else:
                if utilization < low:
                    if self._low_since is None:
                        self._low_since = observed_at
                    if observed_at - self._low_since >= exit_hold:
                        self._level = 0
                        changed_level = 0
                        self._clear_timers()
                else:
                    self._low_since = None

                if self._level == 1:
                    if utilization > 0.9:
                        if self._d2_since is None:
                            self._d2_since = observed_at
                        if observed_at - self._d2_since >= enter_hold:
                            self._level = 2
                            changed_level = 2
                            self._d2_since = None
                    else:
                        self._d2_since = None

            level = self._level

        if changed_level is not None:
            _metric("set_degrade_level", changed_level)
        return DegradeSnapshot(level=level, effects=_DEGRADE_EFFECTS[level])

    def reset(self) -> None:
        with self._lock:
            self._level = 0
            self._clear_timers()
        _metric("set_degrade_level", 0)

    def _clear_timers(self) -> None:
        self._high_since = None
        self._d2_since = None
        self._low_since = None


class DegradeMonitor:
    """Closable periodic ticker so hold timers progress without request events."""

    def __init__(
        self,
        controller: DegradeController,
        utilization: Callable[[], float],
        *,
        interval_seconds: float = 1.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._controller = controller
        self._utilization = utilization
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def tick(self) -> DegradeSnapshot:
        """Perform one deterministic observation for tests and eager callers."""

        return self._controller.update(self._utilization())

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="llm-degrade-monitor",
                daemon=True,
            )
            self._thread.start()

    def close(self, timeout: float = 2.0) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        with self._lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - monitoring must never terminate the process
                continue


class CircuitMode(StrEnum):
    CLOSED = "closed"
    HALF_OPEN = "half_open"
    OPEN = "open"


_CB_METRIC_VALUE = {
    CircuitMode.CLOSED: 0,
    CircuitMode.HALF_OPEN: 1,
    CircuitMode.OPEN: 2,
}


@dataclass
class _EndpointState:
    events: deque[tuple[float, bool]] = field(default_factory=deque)
    mode: CircuitMode = CircuitMode.CLOSED
    opened_at: float | None = None
    half_open_permit_id: int | None = None


@dataclass(frozen=True)
class CircuitPermit:
    """Breaker admission token that uniquely owns a half-open probe, if any."""

    endpoint: EndpointKey
    half_open_permit_id: int | None = None

    @property
    def is_half_open_probe(self) -> bool:
        return self.half_open_permit_id is not None


class EndpointCircuitBreaker:
    """Sliding-window endpoint breaker keyed by ``(base_url, model)``."""

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        window_seconds: float | None = None,
        min_samples: int | None = None,
        error_rate_threshold: float | None = None,
        cooldown_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._enabled = enabled
        self._window_seconds = window_seconds
        self._min_samples = min_samples
        self._error_rate_threshold = error_rate_threshold
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._states: dict[EndpointKey, _EndpointState] = {}
        self._lock = threading.Lock()
        self._next_permit_id = 0

    def before_call(self, endpoint: EndpointKey) -> CircuitPermit:
        """Reserve a half-open probe or fail immediately while the circuit is open."""

        if not self._is_enabled():
            return CircuitPermit(endpoint)
        now = self._clock()
        mode_change: CircuitMode | None = None
        retry_after_ms = 0
        permit = CircuitPermit(endpoint)
        with self._lock:
            state = self._states.setdefault(endpoint, _EndpointState())
            self._prune(state, now)
            if state.mode == CircuitMode.OPEN:
                opened_at = now if state.opened_at is None else state.opened_at
                elapsed = now - opened_at
                cooldown = self._cooldown()
                if elapsed < cooldown:
                    retry_after_ms = max(1, math.ceil((cooldown - elapsed) * 1000))
                else:
                    self._next_permit_id += 1
                    state.mode = CircuitMode.HALF_OPEN
                    state.half_open_permit_id = self._next_permit_id
                    permit = CircuitPermit(endpoint, self._next_permit_id)
                    mode_change = CircuitMode.HALF_OPEN
            elif state.mode == CircuitMode.HALF_OPEN:
                retry_after_ms = max(1, math.ceil(self._cooldown() * 1000))

        if mode_change is not None:
            self._publish_state(endpoint, mode_change)
        if retry_after_ms:
            raise LLMGuardOpenError(retry_after_ms=retry_after_ms)
        return permit

    def after_acquire(self, permit: CircuitPermit) -> None:
        """Recheck a queued call after it owns a pool slot."""

        if not self._is_enabled():
            return
        retry_after_ms = 0
        with self._lock:
            state = self._states.setdefault(permit.endpoint, _EndpointState())
            if permit.is_half_open_probe:
                if not (
                    state.mode == CircuitMode.HALF_OPEN
                    and state.half_open_permit_id == permit.half_open_permit_id
                ):
                    retry_after_ms = self._retry_after_locked(state, self._clock())
            elif state.mode != CircuitMode.CLOSED:
                retry_after_ms = self._retry_after_locked(state, self._clock())
        if retry_after_ms:
            raise LLMGuardOpenError(retry_after_ms=retry_after_ms)

    def record_success(self, permit: CircuitPermit | EndpointKey) -> None:
        self._record(self._as_permit(permit), success=True)

    def record_failure(self, permit: CircuitPermit | EndpointKey) -> None:
        self._record(self._as_permit(permit), success=False)

    def record_ignored(self, permit: CircuitPermit | EndpointKey) -> None:
        """Release a half-open probe for non-breaker failures such as HTTP 429."""

        if not self._is_enabled():
            return
        resolved = self._as_permit(permit)
        changed = False
        with self._lock:
            state = self._states.get(resolved.endpoint)
            if (
                state is not None
                and state.mode == CircuitMode.HALF_OPEN
                and resolved.is_half_open_probe
                and state.half_open_permit_id == resolved.half_open_permit_id
            ):
                state.mode = CircuitMode.CLOSED
                state.opened_at = None
                state.half_open_permit_id = None
                state.events.clear()
                changed = True
        if changed:
            self._publish_state(resolved.endpoint, CircuitMode.CLOSED)

    def abandon(self, permit: CircuitPermit) -> None:
        """Return an unused/cancelled half-open permit without declaring health."""

        if not self._is_enabled() or not permit.is_half_open_probe:
            return
        changed = False
        with self._lock:
            state = self._states.get(permit.endpoint)
            if (
                state is not None
                and state.mode == CircuitMode.HALF_OPEN
                and state.half_open_permit_id == permit.half_open_permit_id
            ):
                state.mode = CircuitMode.OPEN
                state.half_open_permit_id = None
                # Preserve opened_at.  Its cooldown already elapsed, so the next
                # caller may become the replacement probe immediately.
                changed = True
        if changed:
            self._publish_state(permit.endpoint, CircuitMode.OPEN)

    def state(self, endpoint: EndpointKey) -> CircuitMode:
        with self._lock:
            state = self._states.get(endpoint)
            return state.mode if state is not None else CircuitMode.CLOSED

    def retry_after_ms(self, endpoint: EndpointKey) -> int:
        now = self._clock()
        with self._lock:
            state = self._states.get(endpoint)
            if state is None or state.mode != CircuitMode.OPEN:
                return 0
            opened_at = now if state.opened_at is None else state.opened_at
            elapsed = now - opened_at
            return max(0, math.ceil((self._cooldown() - elapsed) * 1000))

    def reset(self) -> None:
        with self._lock:
            endpoints = list(self._states)
            self._states.clear()
        for endpoint in endpoints:
            self._publish_state(endpoint, CircuitMode.CLOSED)

    def _record(self, permit: CircuitPermit, *, success: bool) -> None:
        if not self._is_enabled():
            return
        now = self._clock()
        changed: CircuitMode | None = None
        with self._lock:
            state = self._states.setdefault(permit.endpoint, _EndpointState())
            if (
                state.mode == CircuitMode.HALF_OPEN
                and permit.is_half_open_probe
                and state.half_open_permit_id == permit.half_open_permit_id
            ):
                state.half_open_permit_id = None
                state.events.clear()
                if success:
                    state.mode = CircuitMode.CLOSED
                    state.opened_at = None
                    changed = CircuitMode.CLOSED
                else:
                    state.mode = CircuitMode.OPEN
                    state.opened_at = now
                    changed = CircuitMode.OPEN
            elif state.mode == CircuitMode.CLOSED:
                state.events.append((now, success))
                self._prune(state, now)
                samples = len(state.events)
                failures = sum(1 for _, item_success in state.events if not item_success)
                if (
                    samples >= self._minimum_samples()
                    and failures / samples >= self._error_threshold()
                ):
                    state.mode = CircuitMode.OPEN
                    state.opened_at = now
                    state.half_open_permit_id = None
                    changed = CircuitMode.OPEN

        if changed is not None:
            self._publish_state(permit.endpoint, changed)

    def _retry_after_locked(self, state: _EndpointState, now: float) -> int:
        if state.mode == CircuitMode.HALF_OPEN:
            return max(1, math.ceil(self._cooldown() * 1000))
        opened_at = now if state.opened_at is None else state.opened_at
        return max(1, math.ceil((self._cooldown() - (now - opened_at)) * 1000))

    @staticmethod
    def _as_permit(value: CircuitPermit | EndpointKey) -> CircuitPermit:
        return value if isinstance(value, CircuitPermit) else CircuitPermit(value)

    def _prune(self, state: _EndpointState, now: float) -> None:
        cutoff = now - self._window()
        while state.events and state.events[0][0] < cutoff:
            state.events.popleft()

    def _is_enabled(self) -> bool:
        return settings.CB_ENABLED if self._enabled is None else self._enabled

    def _window(self) -> float:
        return (
            float(settings.CB_WINDOW_SECONDS)
            if self._window_seconds is None
            else self._window_seconds
        )

    def _minimum_samples(self) -> int:
        return settings.CB_MIN_SAMPLES if self._min_samples is None else self._min_samples

    def _error_threshold(self) -> float:
        return (
            settings.CB_ERROR_RATE_THRESHOLD
            if self._error_rate_threshold is None
            else self._error_rate_threshold
        )

    def _cooldown(self) -> float:
        return (
            float(settings.CB_COOLDOWN_SECONDS)
            if self._cooldown_seconds is None
            else self._cooldown_seconds
        )

    @staticmethod
    def _publish_state(endpoint: EndpointKey, mode: CircuitMode) -> None:
        _metric("set_cb_state", _endpoint_label(endpoint), _CB_METRIC_VALUE[mode])


@dataclass
class LLMSlotTicket:
    """Idempotently releasable slot; cancellation may release it before completion."""

    pool: LLMPool
    mode: Literal["sync", "async"]
    request_id: str | None
    async_semaphore: asyncio.BoundedSemaphore | None = None
    async_loop: asyncio.AbstractEventLoop | None = None
    _released: bool = False
    _cancelled: bool = False
    _orphan_recorded: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def cancelled(self) -> bool:
        with self._lock:
            locally_cancelled = self._cancelled
        return locally_cancelled or _request_cancelled(self.request_id)

    def cancel(self) -> bool:
        """Mark cancelled and immediately return the capacity slot once."""

        record_orphan = False
        with self._lock:
            self._cancelled = True
            if self._released:
                if self.mode == "sync" and not self._orphan_recorded:
                    self._orphan_recorded = True
                    record_orphan = True
                should_release = False
            else:
                self._released = True
                should_release = True
        if should_release:
            self.pool._return_slot(self)  # noqa: SLF001 - one abstraction
        if record_orphan:
            _metric("record_llm_orphan_call")
        return should_release

    def finish(self, *, count_orphan: bool = True) -> None:
        """Complete naturally, releasing once and counting sync orphan completion."""

        should_release = False
        record_orphan = False
        with self._lock:
            if not self._released:
                self._released = True
                should_release = True
            if (
                count_orphan
                and self.mode == "sync"
                and self._cancelled
                and not self._orphan_recorded
            ):
                self._orphan_recorded = True
                record_orphan = True
        if should_release:
            self.pool._return_slot(self)  # noqa: SLF001 - ticket and pool are one abstraction
        if record_orphan:
            _metric("record_llm_orphan_call")


_active_ticket_lock = threading.Lock()
_active_tickets: dict[str, set[int]] = {}
_tickets_by_id: dict[int, LLMSlotTicket] = {}
_cancelled_request_until: dict[str, float] = {}


def _prune_cancelled_requests_locked(now: float) -> None:
    expired = [
        request_id
        for request_id, expires_at in _cancelled_request_until.items()
        if expires_at <= now
    ]
    for request_id in expired:
        _cancelled_request_until.pop(request_id, None)


def _request_cancelled(request_id: str | None) -> bool:
    if not request_id:
        return False
    with _active_ticket_lock:
        now = time.monotonic()
        _prune_cancelled_requests_locked(now)
        return _cancelled_request_until.get(request_id, 0.0) > now


def _register_ticket(ticket: LLMSlotTicket) -> bool:
    """Register a ticket unless its request was already cancelled.

    The cancellation marker closes the small race between returning one failed
    provider attempt and acquiring a retry/fallback slot after the outer graph
    timeout has already released the request.
    """

    if not ticket.request_id:
        return True
    ticket_id = id(ticket)
    with _active_ticket_lock:
        now = time.monotonic()
        _prune_cancelled_requests_locked(now)
        if _cancelled_request_until.get(ticket.request_id, 0.0) > now:
            return False
        _tickets_by_id[ticket_id] = ticket
        _active_tickets.setdefault(ticket.request_id, set()).add(ticket_id)
    return True


def _unregister_ticket(ticket: LLMSlotTicket) -> None:
    if not ticket.request_id:
        return
    ticket_id = id(ticket)
    with _active_ticket_lock:
        _tickets_by_id.pop(ticket_id, None)
        ids = _active_tickets.get(ticket.request_id)
        if ids is None:
            return
        ids.discard(ticket_id)
        if not ids:
            _active_tickets.pop(ticket.request_id, None)


def cancel_request(request_id: str) -> int:
    """Release active slots and prevent a timed-out request from starting retries."""

    with _active_ticket_lock:
        now = time.monotonic()
        _prune_cancelled_requests_locked(now)
        marker_ttl = max(
            300.0,
            float(settings.LLM_CALL_TIMEOUT_SECONDS)
            + float(settings.LLM_SLOT_WAIT_TIMEOUT_SECONDS)
            + 30.0,
        )
        _cancelled_request_until[request_id] = now + marker_ttl
        tickets = [
            ticket
            for ticket_id in _active_tickets.get(request_id, set())
            if (ticket := _tickets_by_id.get(ticket_id)) is not None
        ]
    return sum(1 for ticket in tickets if ticket.cancel())


class LLMPool:
    """One concurrency pool shared by synchronous and asynchronous model calls."""

    def __init__(
        self,
        name: CallChannel,
        max_concurrency: int,
        *,
        slot_wait_timeout_seconds: float | None = None,
        adaptive_enabled: bool | None = None,
        adaptive_recovery_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        on_utilization: Callable[[float], Any] | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self.name = name
        self.max_concurrency = max_concurrency
        self._effective_max = max_concurrency
        self._slot_wait_timeout_seconds = slot_wait_timeout_seconds
        self._adaptive_enabled = adaptive_enabled
        self._adaptive_recovery_seconds = adaptive_recovery_seconds
        self._clock = clock
        self._on_utilization = on_utilization
        self._inflight = 0
        self._last_429_at: float | None = None
        self._last_increase_at: float | None = None
        self._condition = threading.Condition(threading.Lock())
        self._sync_semaphore = threading.BoundedSemaphore(max_concurrency)
        self._async_semaphores: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, asyncio.BoundedSemaphore
        ] = weakref.WeakKeyDictionary()
        self._async_semaphores_lock = threading.Lock()
        _metric("set_guard_llm_effective_max", self.name, self._effective_max)

    @property
    def inflight(self) -> int:
        with self._condition:
            return self._inflight

    @property
    def effective_max(self) -> int:
        with self._condition:
            self._recover_locked(self._clock())
            return self._effective_max

    @property
    def utilization(self) -> float:
        with self._condition:
            self._recover_locked(self._clock())
            return self._inflight / self._effective_max

    def acquire_sync(
        self,
        *,
        request_id: str | None = None,
        timeout: float | None = None,
    ) -> LLMSlotTicket:
        if _request_cancelled(request_id):
            raise asyncio.CancelledError
        wait_timeout = self._wait_timeout(timeout)
        deadline = self._clock() + wait_timeout
        if not self._sync_semaphore.acquire(timeout=wait_timeout):
            self._raise_busy()
        reserved = False
        try:
            with self._condition:
                while True:
                    now = self._clock()
                    self._recover_locked(now)
                    if self._inflight < self._effective_max:
                        self._inflight += 1
                        reserved = True
                        inflight = self._inflight
                        utilization = inflight / self._effective_max
                        break
                    remaining = deadline - now
                    if remaining <= 0:
                        self._raise_busy()
                    self._condition.wait(timeout=min(remaining, 0.25))
        except BaseException:
            if not reserved:
                self._sync_semaphore.release()
            raise

        ticket = LLMSlotTicket(pool=self, mode="sync", request_id=request_id)
        if not _register_ticket(ticket):
            ticket.cancel()
            raise asyncio.CancelledError
        _metric("record_guard_llm_admitted", self.name)
        self._publish_utilization(inflight, utilization)
        return ticket

    async def acquire_async(
        self,
        *,
        request_id: str | None = None,
        timeout: float | None = None,
    ) -> LLMSlotTicket:
        if _request_cancelled(request_id):
            raise asyncio.CancelledError
        wait_timeout = self._wait_timeout(timeout)
        deadline = self._clock() + wait_timeout
        loop = asyncio.get_running_loop()
        semaphore = self._async_semaphore(loop)
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=wait_timeout)
        except TimeoutError:
            self._raise_busy()

        reserved = False
        try:
            while True:
                with self._condition:
                    now = self._clock()
                    self._recover_locked(now)
                    if self._inflight < self._effective_max:
                        self._inflight += 1
                        reserved = True
                        inflight = self._inflight
                        utilization = inflight / self._effective_max
                        break
                    remaining = deadline - now
                if remaining <= 0:
                    self._raise_busy()
                await asyncio.sleep(min(remaining, 0.02))
        except BaseException:
            if not reserved:
                semaphore.release()
            raise

        ticket = LLMSlotTicket(
            pool=self,
            mode="async",
            request_id=request_id,
            async_semaphore=semaphore,
            async_loop=loop,
        )
        if not _register_ticket(ticket):
            ticket.cancel()
            raise asyncio.CancelledError
        _metric("record_guard_llm_admitted", self.name)
        self._publish_utilization(inflight, utilization)
        return ticket

    def record_429(self) -> int:
        """Multiplicatively reduce effective capacity and return the new ceiling."""

        now = self._clock()
        with self._condition:
            if self._adaptive():
                floor = max(1, self.max_concurrency // 4)
                self._effective_max = max(floor, int(self._effective_max * 0.8))
                self._last_429_at = now
                self._last_increase_at = now
                self._condition.notify_all()
            effective_max = self._effective_max
            inflight = self._inflight
            utilization = inflight / effective_max
        _metric("record_upstream_429")
        self._publish_utilization(inflight, utilization)
        return effective_max

    def reset(self) -> None:
        with self._condition:
            if self._inflight:
                raise RuntimeError("cannot reset a pool with active calls")
            self._effective_max = self.max_concurrency
            self._last_429_at = None
            self._last_increase_at = None
            self._condition.notify_all()
        self._publish_utilization(0, 0.0)

    def _return_slot(self, ticket: LLMSlotTicket) -> None:
        with self._condition:
            if self._inflight <= 0:
                raise RuntimeError("LLM pool slot released more than once")
            self._inflight -= 1
            self._recover_locked(self._clock())
            inflight = self._inflight
            utilization = inflight / self._effective_max
            self._condition.notify_all()

        if ticket.mode == "sync":
            self._sync_semaphore.release()
        else:
            semaphore = ticket.async_semaphore
            loop = ticket.async_loop
            if semaphore is None or loop is None:
                raise RuntimeError("async ticket is missing its event-loop semaphore")
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if running_loop is loop:
                semaphore.release()
            elif not loop.is_closed():
                loop.call_soon_threadsafe(semaphore.release)
        self._publish_utilization(inflight, utilization)

    def _recover_locked(self, now: float) -> None:
        if (
            not self._adaptive()
            or self._effective_max >= self.max_concurrency
            or self._last_increase_at is None
        ):
            return
        elapsed = now - self._last_increase_at
        steps = int(elapsed // self._adaptive_recovery_seconds)
        if steps <= 0:
            return
        for _ in range(steps):
            increase = max(1, math.ceil(self._effective_max * 0.1))
            self._effective_max = min(self.max_concurrency, self._effective_max + increase)
            if self._effective_max >= self.max_concurrency:
                break
        self._last_increase_at += steps * self._adaptive_recovery_seconds
        self._condition.notify_all()

    def _async_semaphore(self, loop: asyncio.AbstractEventLoop) -> asyncio.BoundedSemaphore:
        with self._async_semaphores_lock:
            semaphore = self._async_semaphores.get(loop)
            if semaphore is None:
                semaphore = asyncio.BoundedSemaphore(self.max_concurrency)
                self._async_semaphores[loop] = semaphore
            return semaphore

    def _wait_timeout(self, timeout: float | None) -> float:
        resolved = (
            timeout
            if timeout is not None
            else (
                settings.LLM_SLOT_WAIT_TIMEOUT_SECONDS
                if self._slot_wait_timeout_seconds is None
                else self._slot_wait_timeout_seconds
            )
        )
        return max(0.0, float(resolved))

    def _adaptive(self) -> bool:
        return (
            settings.CB_429_ADAPTIVE_ENABLED
            if self._adaptive_enabled is None
            else self._adaptive_enabled
        )

    def _raise_busy(self) -> None:
        error = LLMBusyError()
        _metric("record_guard_rejected", error.reason)
        error.rejection_recorded = True
        raise error

    def _publish_utilization(self, inflight: int, utilization: float) -> None:
        # A delayed publisher must never overwrite the gauge with an older
        # captured value. Read and publish the current state while holding the
        # pool lock so concurrent reserve/release transitions stay ordered.
        with self._condition:
            inflight = self._inflight
            utilization = inflight / self._effective_max
            _metric("set_guard_llm_inflight", self.name, inflight)
            _metric("set_guard_llm_effective_max", self.name, self._effective_max)
        if self._on_utilization is not None:
            self._on_utilization(utilization)


@dataclass(frozen=True)
class GuardedEndpoint:
    model: Any
    base_url: str
    model_name: str

    @property
    def key(self) -> EndpointKey:
        return (self.base_url, self.model_name)


class GuardedChatModel(BaseChatModel):
    """Wrap a chat model with pool admission, endpoint breaker, AIMD, and fallback."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    inner: Any = Field(exclude=True)
    pool: LLMPool = Field(exclude=True)
    base_url: str
    model_name: str
    request_id: str | None = None
    outcome_deadline_monotonic: float | None = None
    fallback_endpoints: tuple[GuardedEndpoint, ...] = Field(default=(), exclude=True)
    circuit_breaker: EndpointCircuitBreaker = Field(exclude=True)
    rate_limit_max_retries: int = Field(default=2, ge=0)
    sync_retry_sleep: Callable[[float], None] = Field(default=time.sleep, exclude=True)
    async_retry_sleep: Callable[[float], Awaitable[None]] = Field(
        default=asyncio.sleep,
        exclude=True,
    )

    @property
    def _llm_type(self) -> str:
        return "guarded-chat-model"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "model": self.model_name,
            "pool": self.pool.name,
            "fallback_models": [candidate.model_name for candidate in self.fallback_endpoints],
        }

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | Any],
        *,
        tool_choice: dict[str, Any] | str | bool | None = None,
        **kwargs: Any,
    ) -> Runnable[Any, Any]:
        """Reuse the provider's tool formatter, then bind its request kwargs to this guard."""

        bind_kwargs = dict(kwargs)
        if tool_choice is not None:
            bind_kwargs["tool_choice"] = tool_choice
        provider_binding = self.inner.bind_tools(tools, **bind_kwargs)
        provider_kwargs = getattr(provider_binding, "kwargs", None)
        if not isinstance(provider_kwargs, dict):
            raise TypeError("inner model bind_tools must return a RunnableBinding")
        return self.bind(**provider_kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if bool(getattr(self.inner, "streaming", False)):
            return generate_from_stream(
                self._stream(
                    messages,
                    stop=stop,
                    run_manager=run_manager,
                    **kwargs,
                )
            )
        return self._with_sync_fallback(
            lambda candidate: candidate.model._generate(
                messages,
                stop=stop,
                run_manager=run_manager,
                **kwargs,
            )
        )

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if bool(getattr(self.inner, "streaming", False)):
            return await agenerate_from_stream(
                self._astream(
                    messages,
                    stop=stop,
                    run_manager=run_manager,
                    **kwargs,
                )
            )
        return await self._with_async_fallback(
            lambda candidate: candidate.model._agenerate(
                messages,
                stop=stop,
                run_manager=run_manager,
                **kwargs,
            )
        )

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        last_error: BaseException | None = None
        for candidate in self._candidates():
            for attempt in range(self.rate_limit_max_retries + 1):
                try:
                    permit, ticket = self._begin_sync(candidate)
                except LLMGuardOpenError as exc:
                    last_error = exc
                    break
                emitted = False
                retry_delay: float | None = None
                try:
                    if ticket.cancelled:
                        raise asyncio.CancelledError
                    for chunk in candidate.model._stream(
                        messages,
                        stop=stop,
                        run_manager=run_manager,
                        **kwargs,
                    ):
                        if ticket.cancelled:
                            raise asyncio.CancelledError
                        emitted = True
                        yield chunk
                    if ticket.cancelled:
                        raise asyncio.CancelledError
                    self._record_success(permit, ticket)
                    return
                except BaseException as exc:
                    self._record_failure(permit, ticket, exc)
                    if ticket.cancelled:
                        raise asyncio.CancelledError from exc
                    last_error = exc
                    if _is_rate_limit(exc):
                        if emitted or attempt >= self.rate_limit_max_retries:
                            raise self._rate_limit_exhausted(exc, attempt) from exc
                        retry_delay = _rate_limit_delay_seconds(exc, attempt)
                    elif emitted and _is_endpoint_failure(exc):
                        raise self._upstream_failure(exc) from exc
                    elif emitted or not _can_fallback(exc):
                        raise
                    elif attempt < self.rate_limit_max_retries:
                        retry_delay = _rate_limit_delay_seconds(exc, attempt)
                    else:
                        break
                finally:
                    ticket.finish()
                    _unregister_ticket(ticket)
                if retry_delay is not None:
                    if _request_cancelled(self.request_id):
                        raise asyncio.CancelledError
                    self.sync_retry_sleep(self._retry_delay_with_deadline(retry_delay))
        self._raise_fallback_exhausted(last_error)

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        last_error: BaseException | None = None
        for candidate in self._candidates():
            for attempt in range(self.rate_limit_max_retries + 1):
                try:
                    permit, ticket = await self._begin_async(candidate)
                except LLMGuardOpenError as exc:
                    last_error = exc
                    break
                emitted = False
                retry_delay: float | None = None
                try:
                    if ticket.cancelled:
                        raise asyncio.CancelledError
                    async for chunk in candidate.model._astream(
                        messages,
                        stop=stop,
                        run_manager=run_manager,
                        **kwargs,
                    ):
                        if ticket.cancelled:
                            raise asyncio.CancelledError
                        emitted = True
                        yield chunk
                    if ticket.cancelled:
                        raise asyncio.CancelledError
                    self._record_success(permit, ticket)
                    return
                except BaseException as exc:
                    self._record_failure(permit, ticket, exc)
                    if ticket.cancelled:
                        raise asyncio.CancelledError from exc
                    last_error = exc
                    if _is_rate_limit(exc):
                        if emitted or attempt >= self.rate_limit_max_retries:
                            raise self._rate_limit_exhausted(exc, attempt) from exc
                        retry_delay = _rate_limit_delay_seconds(exc, attempt)
                    elif emitted and _is_endpoint_failure(exc):
                        raise self._upstream_failure(exc) from exc
                    elif emitted or not _can_fallback(exc):
                        raise
                    elif attempt < self.rate_limit_max_retries:
                        retry_delay = _rate_limit_delay_seconds(exc, attempt)
                    else:
                        break
                finally:
                    ticket.finish()
                    _unregister_ticket(ticket)
                if retry_delay is not None:
                    if _request_cancelled(self.request_id):
                        raise asyncio.CancelledError
                    await self.async_retry_sleep(self._retry_delay_with_deadline(retry_delay))
        self._raise_fallback_exhausted(last_error)

    def _with_sync_fallback(self, operation: Callable[[GuardedEndpoint], ChatResult]) -> ChatResult:
        last_error: BaseException | None = None
        for candidate in self._candidates():
            try:
                return self._call_sync_candidate(candidate, operation)
            except LLMGuardOpenError as exc:
                if exc.reason == "upstream_429":
                    raise
                last_error = exc
                continue
            except LLMGuardError:
                raise
            except BaseException as exc:
                last_error = exc
                if not _can_fallback(exc):
                    raise
        self._raise_fallback_exhausted(last_error)

    async def _with_async_fallback(
        self,
        operation: Callable[[GuardedEndpoint], Awaitable[ChatResult]],
    ) -> ChatResult:
        last_error: BaseException | None = None
        for candidate in self._candidates():
            try:
                return await self._call_async_candidate(candidate, operation)
            except LLMGuardOpenError as exc:
                if exc.reason == "upstream_429":
                    raise
                last_error = exc
                continue
            except LLMGuardError:
                raise
            except BaseException as exc:
                last_error = exc
                if not _can_fallback(exc):
                    raise
        self._raise_fallback_exhausted(last_error)

    def _call_sync_candidate(
        self,
        candidate: GuardedEndpoint,
        operation: Callable[[GuardedEndpoint], ChatResult],
    ) -> ChatResult:
        for attempt in range(self.rate_limit_max_retries + 1):
            permit, ticket = self._begin_sync(candidate)
            retry_delay: float | None = None
            try:
                result = operation(candidate)
            except BaseException as exc:
                self._record_failure(permit, ticket, exc)
                if ticket.cancelled:
                    raise asyncio.CancelledError from exc
                if _is_rate_limit(exc):
                    if attempt >= self.rate_limit_max_retries:
                        raise self._rate_limit_exhausted(exc, attempt) from exc
                    retry_delay = _rate_limit_delay_seconds(exc, attempt)
                elif _is_endpoint_failure(exc):
                    if attempt >= self.rate_limit_max_retries:
                        raise
                    retry_delay = _rate_limit_delay_seconds(exc, attempt)
                else:
                    raise
            else:
                self._record_success(permit, ticket)
                if ticket.cancelled:
                    raise asyncio.CancelledError
                return result
            finally:
                ticket.finish()
                _unregister_ticket(ticket)
            if retry_delay is not None:
                if _request_cancelled(self.request_id):
                    raise asyncio.CancelledError
                self.sync_retry_sleep(self._retry_delay_with_deadline(retry_delay))
        raise AssertionError("unreachable rate-limit retry loop")

    async def _call_async_candidate(
        self,
        candidate: GuardedEndpoint,
        operation: Callable[[GuardedEndpoint], Awaitable[ChatResult]],
    ) -> ChatResult:
        for attempt in range(self.rate_limit_max_retries + 1):
            permit, ticket = await self._begin_async(candidate)
            retry_delay: float | None = None
            try:
                result = await operation(candidate)
            except BaseException as exc:
                self._record_failure(permit, ticket, exc)
                if ticket.cancelled:
                    raise asyncio.CancelledError from exc
                if _is_rate_limit(exc):
                    if attempt >= self.rate_limit_max_retries:
                        raise self._rate_limit_exhausted(exc, attempt) from exc
                    retry_delay = _rate_limit_delay_seconds(exc, attempt)
                elif _is_endpoint_failure(exc):
                    if attempt >= self.rate_limit_max_retries:
                        raise
                    retry_delay = _rate_limit_delay_seconds(exc, attempt)
                else:
                    raise
            else:
                self._record_success(permit, ticket)
                if ticket.cancelled:
                    raise asyncio.CancelledError
                return result
            finally:
                ticket.finish()
                _unregister_ticket(ticket)
            if retry_delay is not None:
                if _request_cancelled(self.request_id):
                    raise asyncio.CancelledError
                await self.async_retry_sleep(self._retry_delay_with_deadline(retry_delay))
        raise AssertionError("unreachable rate-limit retry loop")

    def _begin_sync(
        self,
        candidate: GuardedEndpoint,
    ) -> tuple[CircuitPermit, LLMSlotTicket]:
        if _request_cancelled(self.request_id):
            raise asyncio.CancelledError
        slot_timeout = self._outcome_slot_timeout()
        permit = self.circuit_breaker.before_call(candidate.key)
        try:
            ticket = self.pool.acquire_sync(
                request_id=self.request_id,
                timeout=slot_timeout,
            )
        except BaseException:
            self.circuit_breaker.abandon(permit)
            raise
        try:
            if ticket.cancelled:
                raise asyncio.CancelledError
            # The semaphore may be granted exactly as the absolute first-outcome
            # budget expires. Recheck after admission before any provider work.
            self._outcome_slot_timeout()
            self.circuit_breaker.after_acquire(permit)
            if ticket.cancelled:
                raise asyncio.CancelledError
        except BaseException:
            self.circuit_breaker.abandon(permit)
            ticket.finish(count_orphan=False)
            _unregister_ticket(ticket)
            raise
        return permit, ticket

    async def _begin_async(
        self,
        candidate: GuardedEndpoint,
    ) -> tuple[CircuitPermit, LLMSlotTicket]:
        if _request_cancelled(self.request_id):
            raise asyncio.CancelledError
        slot_timeout = self._outcome_slot_timeout()
        permit = self.circuit_breaker.before_call(candidate.key)
        try:
            ticket = await self.pool.acquire_async(
                request_id=self.request_id,
                timeout=slot_timeout,
            )
        except BaseException:
            self.circuit_breaker.abandon(permit)
            raise
        try:
            if ticket.cancelled:
                raise asyncio.CancelledError
            # Async admission has the same deadline/grant race as the sync path.
            self._outcome_slot_timeout()
            self.circuit_breaker.after_acquire(permit)
            if ticket.cancelled:
                raise asyncio.CancelledError
        except BaseException:
            self.circuit_breaker.abandon(permit)
            ticket.finish(count_orphan=False)
            _unregister_ticket(ticket)
            raise
        return permit, ticket

    def _outcome_slot_timeout(self) -> float | None:
        """Bound slot admission by the request's first-outcome commitment."""

        if self.outcome_deadline_monotonic is None:
            return None
        remaining = self.outcome_deadline_monotonic - time.monotonic()
        if remaining <= 0:
            self._raise_outcome_deadline()
        return min(float(settings.LLM_SLOT_WAIT_TIMEOUT_SECONDS), remaining)

    def _retry_delay_with_deadline(self, delay_seconds: float) -> float:
        """Never sleep past the point where a stable busy error is owed."""

        if self.outcome_deadline_monotonic is None:
            return delay_seconds
        remaining = self.outcome_deadline_monotonic - time.monotonic()
        if remaining <= 0 or delay_seconds >= remaining:
            self._raise_outcome_deadline()
        return min(delay_seconds, remaining)

    @staticmethod
    def _raise_outcome_deadline() -> NoReturn:
        error = LLMOutcomeTimeoutError()
        _metric("record_guard_rejected", error.reason)
        error.rejection_recorded = True
        raise error

    def _record_success(self, permit: CircuitPermit, ticket: LLMSlotTicket) -> None:
        if ticket.cancelled:
            self.circuit_breaker.abandon(permit)
            return
        self.circuit_breaker.record_success(permit)

    def _record_failure(
        self,
        permit: CircuitPermit,
        ticket: LLMSlotTicket,
        exc: BaseException,
    ) -> None:
        if _is_rate_limit(exc):
            self.pool.record_429()
            if ticket.cancelled:
                self.circuit_breaker.abandon(permit)
            else:
                self.circuit_breaker.record_ignored(permit)
        elif ticket.cancelled or isinstance(exc, asyncio.CancelledError):
            self.circuit_breaker.abandon(permit)
        elif _is_endpoint_failure(exc):
            self.circuit_breaker.record_failure(permit)
        else:
            # A provider response was obtained; parsing and business errors do not
            # indicate endpoint unavailability and must not poison the breaker.
            self.circuit_breaker.record_success(permit)

    def _rate_limit_exhausted(
        self,
        exc: BaseException,
        attempt: int,
    ) -> LLMGuardOpenError:
        retry_after_ms = max(1, math.ceil(_rate_limit_delay_seconds(exc, attempt) * 1000))
        error = LLMGuardOpenError(
            retry_after_ms=retry_after_ms,
            reason="upstream_429",
        )
        # Each individual 429 is already represented by guard_upstream_429_total;
        # do not report it as a circuit-open rejection while the breaker is closed.
        error.rejection_recorded = True
        return error

    def _upstream_failure(self, exc: BaseException) -> LLMGuardOpenError:
        retry_after_ms = max(
            1_000,
            math.ceil(_rate_limit_delay_seconds(exc, 0) * 1000),
        )
        error = LLMGuardOpenError(
            retry_after_ms=retry_after_ms,
            reason="upstream_failure",
        )
        # The request failed, but the endpoint may still be below the breaker
        # threshold. Keep it out of rejected{cb_open} while preserving 50300.
        error.rejection_recorded = True
        return error

    def _candidates(self) -> tuple[GuardedEndpoint, ...]:
        return (
            GuardedEndpoint(self.inner, self.base_url, self.model_name),
            *self.fallback_endpoints,
        )

    def _raise_fallback_exhausted(self, last_error: BaseException | None) -> NoReturn:
        retry_values = [
            self.circuit_breaker.retry_after_ms(candidate.key) for candidate in self._candidates()
        ]
        if isinstance(last_error, LLMGuardOpenError) and last_error.retry_after_ms is not None:
            retry_values.append(last_error.retry_after_ms)
        retry_after_ms = max(retry_values or [settings.CB_COOLDOWN_SECONDS * 1000])
        if retry_after_ms <= 0:
            retry_after_ms = settings.CB_COOLDOWN_SECONDS * 1000
        circuit_open = all(
            self.circuit_breaker.state(candidate.key) != CircuitMode.CLOSED
            for candidate in self._candidates()
        )
        reason = "cb_open" if circuit_open else "upstream_failure"
        error = LLMGuardOpenError(
            retry_after_ms=retry_after_ms,
            reason=reason,
        )
        if circuit_open:
            _metric("record_guard_rejected", error.reason)
        error.rejection_recorded = True
        if last_error is not None:
            raise error from last_error
        raise error


def _status_code(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    response_status = getattr(response, "status_code", None)
    return response_status if isinstance(response_status, int) else None


def _is_rate_limit(exc: BaseException) -> bool:
    return _status_code(exc) == 429 or exc.__class__.__name__ == "RateLimitError"


def _rate_limit_delay_seconds(exc: BaseException, attempt: int) -> float:
    """Resolve Retry-After/Retry-After-Ms, otherwise use 1s/2s/4s backoff."""

    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or getattr(exc, "headers", None)
    if headers is not None:
        retry_after_ms = _header_value(headers, "retry-after-ms")
        if retry_after_ms is not None:
            try:
                return max(0.0, float(retry_after_ms) / 1000.0)
            except (TypeError, ValueError):
                pass

        retry_after = _header_value(headers, "retry-after")
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except (TypeError, ValueError):
                try:
                    retry_at = parsedate_to_datetime(str(retry_after))
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=UTC)
                    return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
                except (AttributeError, TypeError, ValueError, OverflowError):
                    pass
    return float(2 ** min(2, max(0, attempt)))


def _header_value(headers: Any, name: str) -> Any | None:
    try:
        value = headers.get(name)
        if value is None:
            value = headers.get(name.title())
        return value
    except (AttributeError, TypeError):
        return None


def _is_endpoint_failure(exc: BaseException) -> bool:
    status = _status_code(exc)
    if status is not None:
        return 500 <= status < 600
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    name = exc.__class__.__name__.lower()
    return "timeout" in name or "connection" in name


def _can_fallback(exc: BaseException) -> bool:
    return not _is_rate_limit(exc) and _is_endpoint_failure(exc)


def _endpoint_label(endpoint: EndpointKey) -> str:
    return f"{endpoint[0].rstrip('/')}|{endpoint[1]}"


degrade_controller = DegradeController()
endpoint_circuit_breaker = EndpointCircuitBreaker()


def _observe_online_utilization(utilization: float) -> None:
    degrade_controller.update(utilization)


online_pool = LLMPool(
    "online",
    settings.LLM_MAX_CONCURRENCY,
    on_utilization=_observe_online_utilization,
)
background_pool = LLMPool("background", settings.LLM_BG_MAX_CONCURRENCY)
degrade_monitor = DegradeMonitor(
    degrade_controller,
    lambda: online_pool.utilization,
)


def start_degrade_monitor() -> None:
    degrade_monitor.start()


def close_degrade_monitor() -> None:
    degrade_monitor.close()


if settings.GUARD_ENABLED and settings.DEGRADE_ENABLED:
    start_degrade_monitor()
atexit.register(close_degrade_monitor)


def get_llm_pool(channel: CallChannel) -> LLMPool:
    return background_pool if channel == "background" else online_pool


def get_degrade_level() -> int:
    """Return the current process-local degradation level."""

    return get_degrade_snapshot().level


def get_degrade_snapshot() -> DegradeSnapshot:
    """Return a stable degradation snapshot for a new request."""

    # Holds are time-based, so reevaluate even when inflight stayed unchanged
    # during the whole hold interval.
    return degrade_controller.update(online_pool.utilization)


# Provider-specific thinking switches stay centralized here.  Empty mappings
# explicitly mean that D1 only reduces max_tokens for that provider.
PROVIDER_THINKING_PARAM: dict[str, dict[str, Any]] = {
    "deepseek": {},
    "vllm": {"thinking_mode": "none"},
    "generic": {},
}


def provider_name(base_url: str, model_name: str) -> str:
    """Resolve the small provider mapping without leaking policy into callers."""

    identity = f"{base_url} {model_name}".lower()
    if "deepseek" in identity:
        return "deepseek"
    if "vllm" in identity or "qwen" in identity:
        return "vllm"
    return "generic"


def thinking_override(base_url: str, model_name: str, degrade_level: int) -> dict[str, Any]:
    """Return the provider request body used to disable thinking at D1/D2."""

    if degrade_level < 1:
        return {}
    return dict(PROVIDER_THINKING_PARAM[provider_name(base_url, model_name)])
