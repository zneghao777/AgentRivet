from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import ConfigDict, Field

from app.harness.llm_guard import (
    CircuitMode,
    DegradeController,
    DegradeMonitor,
    EndpointCircuitBreaker,
    GuardedChatModel,
    GuardedEndpoint,
    LLMBusyError,
    LLMGuardOpenError,
    LLMOutcomeTimeoutError,
    LLMPool,
    LLMResponseTimeoutError,
    LLMSlotTicket,
    cancel_request,
)


class StubHTTPError(RuntimeError):
    def __init__(self, status_code: int, headers: dict[str, str] | None = None) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.response = type(
            "StubResponse",
            (),
            {"status_code": status_code, "headers": headers or {}},
        )()


class StubChatModel(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    response: str = "ok"
    failure_status: int | None = None
    failure_statuses: list[int | None] = Field(default_factory=list)
    response_headers: dict[str, str] = Field(default_factory=dict)
    started: Any = None
    blocker: Any = None
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "stub-chat-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.calls += 1
        if self.started is not None:
            self.started.set()
        if self.blocker is not None:
            self.blocker.wait(timeout=2)
        status = self.failure_statuses.pop(0) if self.failure_statuses else self.failure_status
        if status is not None:
            raise StubHTTPError(status, self.response_headers)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.response))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.calls += 1
        status = self.failure_statuses.pop(0) if self.failure_statuses else self.failure_status
        if status is not None:
            raise StubHTTPError(status, self.response_headers)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.response))])


class PartialFailStreamingModel(StubChatModel):
    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        yield ChatGenerationChunk(message=AIMessageChunk(content="partial"))
        raise StubHTTPError(500)


class BlockingFailStreamingModel(StubChatModel):
    streaming: bool = True

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        self.started.set()
        self.blocker.wait(timeout=2)
        raise StubHTTPError(500)
        yield  # pragma: no cover - make this a generator


def _guard(
    inner: BaseChatModel,
    *,
    pool: LLMPool | None = None,
    breaker: EndpointCircuitBreaker | None = None,
    request_id: str | None = None,
    fallbacks: tuple[GuardedEndpoint, ...] = (),
    **guard_kwargs: Any,
) -> GuardedChatModel:
    return GuardedChatModel(
        inner=inner,
        pool=pool or LLMPool("online", 2, adaptive_enabled=False),
        base_url="https://models.example.test/v1",
        model_name="primary",
        request_id=request_id,
        fallback_endpoints=fallbacks,
        circuit_breaker=breaker or EndpointCircuitBreaker(enabled=True),
        **guard_kwargs,
    )


@pytest.mark.asyncio
async def test_pool_enforces_one_limit_across_sync_and_async_paths():
    from app.observability.metrics import metrics

    pool = LLMPool(
        "online",
        1,
        slot_wait_timeout_seconds=0.02,
        adaptive_enabled=False,
    )
    sync_ticket = pool.acquire_sync()
    slot_before = metrics.guard_rejected_total.get("llm_slot_timeout", 0)
    queue_before = metrics.guard_rejected_total.get("queue_timeout", 0)

    with pytest.raises(LLMBusyError) as error:
        await pool.acquire_async()
    assert error.value.code == 50400
    assert error.value.reason == "llm_slot_timeout"
    assert error.value.rejection_recorded is True
    assert metrics.guard_rejected_total.get("llm_slot_timeout", 0) == slot_before + 1
    assert metrics.guard_rejected_total.get("queue_timeout", 0) == queue_before

    sync_ticket.finish()
    async_ticket = await pool.acquire_async()
    assert pool.inflight == 1
    async_ticket.finish()
    assert pool.inflight == 0


def test_pool_records_one_admission_per_successful_reserve(monkeypatch):
    calls: list[tuple[str, tuple[Any, ...]]] = []
    monkeypatch.setattr(
        "app.harness.llm_guard._metric",
        lambda name, *args: calls.append((name, args)),
    )
    pool = LLMPool("online", 1, adaptive_enabled=False)

    ticket = pool.acquire_sync()
    ticket.finish()

    admissions = [args for name, args in calls if name == "record_guard_llm_admitted"]
    assert admissions == [("online",)]
    inflight = [args for name, args in calls if name == "set_guard_llm_inflight"]
    assert inflight == [("online", 1), ("online", 0)]


def test_delayed_publish_cannot_overwrite_inflight_with_stale_value(monkeypatch):
    published: list[int] = []

    def capture(name: str, *args: Any) -> None:
        if name == "set_guard_llm_inflight":
            published.append(int(args[1]))

    monkeypatch.setattr("app.harness.llm_guard._metric", capture)
    pool = LLMPool("online", 3, adaptive_enabled=False)
    with pool._condition:
        pool._inflight = 1

    old_publish_waiting = threading.Event()
    allow_old_publish = threading.Event()

    def publish_old_snapshot_late() -> None:
        old_publish_waiting.set()
        allow_old_publish.wait(timeout=2)
        pool._publish_utilization(1, 1 / 3)

    thread = threading.Thread(target=publish_old_snapshot_late)
    thread.start()
    assert old_publish_waiting.wait(timeout=1)

    with pool._condition:
        pool._inflight = 2
    pool._publish_utilization(2, 2 / 3)
    allow_old_publish.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert published == [2, 2]


def test_online_and_background_pools_do_not_block_each_other():
    online = LLMPool("online", 1, adaptive_enabled=False)
    background = LLMPool("background", 1, adaptive_enabled=False)

    online_ticket = online.acquire_sync()
    background_ticket = background.acquire_sync()

    assert online.inflight == 1
    assert background.inflight == 1
    online_ticket.finish()
    background_ticket.finish()


def test_endpoint_breaker_sliding_window_and_single_half_open_probe():
    now = [0.0]
    endpoint = ("https://models.example.test/v1", "primary")
    breaker = EndpointCircuitBreaker(
        enabled=True,
        window_seconds=60,
        min_samples=2,
        error_rate_threshold=0.5,
        cooldown_seconds=5,
        clock=lambda: now[0],
    )

    breaker.record_failure(endpoint)
    breaker.record_success(endpoint)
    assert breaker.state(endpoint) == CircuitMode.OPEN
    with pytest.raises(LLMGuardOpenError):
        breaker.before_call(endpoint)

    now[0] = 5.0
    probe = breaker.before_call(endpoint)
    assert breaker.state(endpoint) == CircuitMode.HALF_OPEN
    with pytest.raises(LLMGuardOpenError):
        breaker.before_call(endpoint)

    breaker.after_acquire(probe)
    breaker.record_success(probe)
    assert breaker.state(endpoint) == CircuitMode.CLOSED


def test_guard_falls_back_after_transient_primary_failure():
    primary = StubChatModel(failure_status=500)
    fallback = StubChatModel(response="fallback answer")
    delays: list[float] = []
    guarded = _guard(
        primary,
        fallbacks=(
            GuardedEndpoint(
                fallback,
                "https://models.example.test/v1",
                "fallback-model",
            ),
        ),
        sync_retry_sleep=delays.append,
    )

    result = guarded.invoke([HumanMessage(content="hello")])

    assert result.content == "fallback answer"
    assert primary.calls == 3
    assert fallback.calls == 1
    assert delays == [1.0, 2.0]


def test_transient_5xx_retries_then_recovers_without_fallback():
    delays: list[float] = []
    primary = StubChatModel(failure_statuses=[500, 500, None])
    fallback = StubChatModel(response="must not run")
    guarded = _guard(
        primary,
        fallbacks=(
            GuardedEndpoint(
                fallback,
                "https://models.example.test/v1",
                "fallback-model",
            ),
        ),
        sync_retry_sleep=delays.append,
    )

    result = guarded.invoke([HumanMessage(content="hello")])

    assert result.content == "ok"
    assert primary.calls == 3
    assert fallback.calls == 0
    assert delays == [1.0, 2.0]


def test_partial_stream_endpoint_failure_returns_stable_503_without_retry():
    inner = PartialFailStreamingModel()
    guarded = _guard(inner, sync_retry_sleep=lambda _delay: None)
    stream = guarded.stream([HumanMessage(content="hello")])

    first = next(stream)
    assert first.content == "partial"
    with pytest.raises(LLMGuardOpenError) as error:
        next(stream)

    assert error.value.code == 50300
    assert error.value.reason == "upstream_failure"
    assert error.value.rejection_recorded is True
    assert inner.calls == 0


def test_429_retries_twice_reduces_each_time_and_returns_stable_503():
    pool = LLMPool("online", 100, adaptive_enabled=True)
    breaker = EndpointCircuitBreaker(enabled=True, min_samples=1)
    delays: list[float] = []
    inner = StubChatModel(failure_status=429)
    guarded = _guard(
        inner,
        pool=pool,
        breaker=breaker,
        sync_retry_sleep=delays.append,
    )

    with pytest.raises(LLMGuardOpenError) as error:
        guarded.invoke([HumanMessage(content="hello")])

    assert error.value.code == 50300
    assert error.value.reason == "upstream_429"
    assert error.value.rejection_recorded is True
    assert error.value.retry_after_ms == 4000
    assert inner.calls == 3
    assert delays == [1.0, 2.0]
    assert pool.effective_max == 51
    assert breaker.state((guarded.base_url, guarded.model_name)) == CircuitMode.CLOSED


def test_429_retry_after_is_honored_and_slot_is_reacquired():
    pool = LLMPool("online", 10, adaptive_enabled=True)
    delays: list[float] = []
    inner = StubChatModel(
        failure_statuses=[429, None],
        response_headers={"Retry-After": "0.25"},
    )
    guarded = _guard(
        inner,
        pool=pool,
        sync_retry_sleep=delays.append,
    )

    result = guarded.invoke([HumanMessage(content="hello")])

    assert result.content == "ok"
    assert inner.calls == 2
    assert delays == [0.25]
    assert pool.effective_max == 8
    assert pool.inflight == 0


def test_final_429_preserves_retry_after_and_does_not_fall_through_to_model_fallback():
    delays: list[float] = []
    primary = StubChatModel(
        failure_status=429,
        response_headers={"Retry-After": "0.5"},
    )
    fallback = StubChatModel(response="must not run")
    guarded = _guard(
        primary,
        fallbacks=(
            GuardedEndpoint(
                fallback,
                "https://models.example.test/v1",
                "fallback-model",
            ),
        ),
        sync_retry_sleep=delays.append,
    )

    with pytest.raises(LLMGuardOpenError) as error:
        guarded.invoke([HumanMessage(content="hello")])

    assert error.value.reason == "upstream_429"
    assert error.value.retry_after_ms == 500
    assert delays == [0.5, 0.5]
    assert primary.calls == 3
    assert fallback.calls == 0


@pytest.mark.asyncio
async def test_async_429_retry_after_uses_guard_managed_retry():
    pool = LLMPool("online", 10, adaptive_enabled=True)
    delays: list[float] = []
    inner = StubChatModel(
        failure_statuses=[429, None],
        response_headers={"retry-after-ms": "125"},
    )

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    guarded = _guard(
        inner,
        pool=pool,
        async_retry_sleep=record_sleep,
    )

    result = await guarded.ainvoke([HumanMessage(content="hello")])

    assert result.content == "ok"
    assert inner.calls == 2
    assert delays == [0.125]
    assert pool.effective_max == 8


def test_guard_rechecks_deadline_after_sync_slot_wait(monkeypatch):
    from app.harness import llm_guard as llm_guard_module

    now = [0.0]
    captured_timeouts: list[float | None] = []

    class LateSyncPool(LLMPool):
        def acquire_sync(self, *, request_id=None, timeout=None):
            captured_timeouts.append(timeout)
            ticket = super().acquire_sync(request_id=request_id, timeout=0)
            now[0] = 1.01
            return ticket

    monkeypatch.setattr(
        llm_guard_module,
        "time",
        SimpleNamespace(monotonic=lambda: now[0]),
    )
    pool = LateSyncPool("online", 1, adaptive_enabled=False)
    breaker = EndpointCircuitBreaker(enabled=True, min_samples=1)
    inner = StubChatModel()
    guarded = _guard(
        inner,
        pool=pool,
        breaker=breaker,
        outcome_deadline_monotonic=1.0,
    )

    with pytest.raises(LLMOutcomeTimeoutError) as error:
        guarded.invoke([HumanMessage(content="hello")])

    assert error.value.code == 50401
    assert error.value.reason == "first_feedback_timeout"
    assert error.value.rejection_recorded is True
    assert captured_timeouts == [pytest.approx(1.0)]
    assert inner.calls == 0
    assert pool.inflight == 0
    assert breaker.state((guarded.base_url, guarded.model_name)) == CircuitMode.CLOSED


@pytest.mark.asyncio
async def test_guard_rechecks_deadline_after_async_slot_wait(monkeypatch):
    from app.harness import llm_guard as llm_guard_module

    now = [0.0]
    captured_timeouts: list[float | None] = []

    class LateAsyncPool(LLMPool):
        async def acquire_async(self, *, request_id=None, timeout=None):
            captured_timeouts.append(timeout)
            ticket = await super().acquire_async(
                request_id=request_id,
                timeout=timeout,
            )
            now[0] = 1.01
            return ticket

    monkeypatch.setattr(
        llm_guard_module,
        "time",
        SimpleNamespace(monotonic=lambda: now[0]),
    )
    pool = LateAsyncPool("online", 1, adaptive_enabled=False)
    breaker = EndpointCircuitBreaker(enabled=True, min_samples=1)
    inner = StubChatModel()
    guarded = _guard(
        inner,
        pool=pool,
        breaker=breaker,
        outcome_deadline_monotonic=1.0,
    )

    with pytest.raises(LLMOutcomeTimeoutError) as error:
        await guarded.ainvoke([HumanMessage(content="hello")])

    assert error.value.code == 50401
    assert error.value.reason == "first_feedback_timeout"
    assert error.value.rejection_recorded is True
    assert captured_timeouts == [pytest.approx(1.0)]
    assert inner.calls == 0
    assert pool.inflight == 0
    assert breaker.state((guarded.base_url, guarded.model_name)) == CircuitMode.CLOSED


def test_guard_retry_stops_before_absolute_outcome_deadline(monkeypatch):
    from app.harness import llm_guard as llm_guard_module

    now = [0.0]
    delays: list[float] = []

    def advance_clock(delay: float) -> None:
        delays.append(delay)
        now[0] += delay

    monkeypatch.setattr(
        llm_guard_module,
        "time",
        SimpleNamespace(monotonic=lambda: now[0]),
    )
    inner = StubChatModel(
        failure_status=429,
        response_headers={"Retry-After": "0.6"},
    )
    guarded = _guard(
        inner,
        outcome_deadline_monotonic=1.0,
        sync_retry_sleep=advance_clock,
    )

    with pytest.raises(LLMOutcomeTimeoutError) as error:
        guarded.invoke([HumanMessage(content="hello")])

    assert error.value.code == 50401
    assert error.value.reason == "first_feedback_timeout"
    assert error.value.rejection_recorded is True
    assert inner.calls == 2
    assert delays == [0.6]
    assert now[0] == pytest.approx(0.6)


def test_regular_queued_permit_is_rejected_if_breaker_opens_before_slot():
    pool = LLMPool(
        "online",
        1,
        slot_wait_timeout_seconds=1,
        adaptive_enabled=False,
    )
    occupied = pool.acquire_sync()
    breaker = EndpointCircuitBreaker(enabled=True, min_samples=1)
    endpoint = ("https://models.example.test/v1", "primary")
    permit_created = threading.Event()
    original_before_call = breaker.before_call

    def coordinated_before_call(value):
        permit = original_before_call(value)
        permit_created.set()
        return permit

    breaker.before_call = coordinated_before_call  # type: ignore[method-assign]
    inner = StubChatModel()
    guarded = _guard(inner, pool=pool, breaker=breaker)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            guarded.invoke([HumanMessage(content="hello")])
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    assert permit_created.wait(timeout=1)
    breaker.record_failure(endpoint)
    occupied.finish()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], LLMGuardOpenError)
    assert inner.calls == 0
    assert pool.inflight == 0


def test_half_open_probe_survives_post_slot_recheck_and_closes_on_success():
    now = [0.0]
    endpoint = ("https://models.example.test/v1", "primary")
    breaker = EndpointCircuitBreaker(
        enabled=True,
        min_samples=1,
        cooldown_seconds=5,
        clock=lambda: now[0],
    )
    breaker.record_failure(endpoint)
    now[0] = 5.0
    inner = StubChatModel()
    guarded = _guard(inner, breaker=breaker)

    result = guarded.invoke([HumanMessage(content="hello")])

    assert result.content == "ok"
    assert breaker.state(endpoint) == CircuitMode.CLOSED


def test_late_regular_success_cannot_close_an_active_half_open_probe():
    now = [0.0]
    endpoint = ("https://models.example.test/v1", "primary")
    breaker = EndpointCircuitBreaker(
        enabled=True,
        min_samples=1,
        cooldown_seconds=5,
        clock=lambda: now[0],
    )
    late_regular = breaker.before_call(endpoint)
    breaker.record_failure(endpoint)
    now[0] = 5.0
    probe = breaker.before_call(endpoint)

    breaker.record_success(late_regular)
    assert breaker.state(endpoint) == CircuitMode.HALF_OPEN
    breaker.record_success(probe)
    assert breaker.state(endpoint) == CircuitMode.CLOSED


def test_half_open_slot_timeout_returns_probe_to_open_not_closed():
    now = [0.0]
    endpoint = ("https://models.example.test/v1", "primary")
    breaker = EndpointCircuitBreaker(
        enabled=True,
        min_samples=1,
        cooldown_seconds=5,
        clock=lambda: now[0],
    )
    pool = LLMPool(
        "online",
        1,
        slot_wait_timeout_seconds=0.01,
        adaptive_enabled=False,
    )
    occupied = pool.acquire_sync()
    breaker.record_failure(endpoint)
    now[0] = 5.0
    guarded = _guard(StubChatModel(), pool=pool, breaker=breaker)

    with pytest.raises(LLMBusyError):
        guarded.invoke([HumanMessage(content="hello")])

    assert breaker.state(endpoint) == CircuitMode.OPEN
    replacement_probe = breaker.before_call(endpoint)
    assert replacement_probe.is_half_open_probe
    breaker.abandon(replacement_probe)
    occupied.finish()


@pytest.mark.asyncio
async def test_half_open_async_acquire_cancellation_returns_probe_to_open():
    now = [0.0]
    endpoint = ("https://models.example.test/v1", "primary")
    breaker = EndpointCircuitBreaker(
        enabled=True,
        min_samples=1,
        cooldown_seconds=5,
        clock=lambda: now[0],
    )
    pool = LLMPool(
        "online",
        1,
        slot_wait_timeout_seconds=1,
        adaptive_enabled=False,
    )
    occupied = pool.acquire_sync()
    breaker.record_failure(endpoint)
    now[0] = 5.0
    guarded = _guard(StubChatModel(), pool=pool, breaker=breaker)

    task = asyncio.create_task(guarded.ainvoke([HumanMessage(content="hello")]))
    for _ in range(100):
        if breaker.state(endpoint) == CircuitMode.HALF_OPEN:
            break
        await asyncio.sleep(0)
    assert breaker.state(endpoint) == CircuitMode.HALF_OPEN
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert breaker.state(endpoint) == CircuitMode.OPEN
    occupied.finish()


def test_aimd_recovers_ten_percent_after_thirty_seconds_without_429():
    now = [0.0]
    pool = LLMPool(
        "online",
        100,
        adaptive_enabled=True,
        adaptive_recovery_seconds=30,
        clock=lambda: now[0],
    )

    assert pool.record_429() == 80
    now[0] = 29.9
    assert pool.effective_max == 80
    now[0] = 30.0
    assert pool.effective_max == 88

    now[0] = 31.0
    for _ in range(10):
        pool.record_429()
    assert pool.effective_max == 25


def test_degrade_controller_uses_hold_periods_and_hysteresis():
    now = [0.0]
    controller = DegradeController(
        enabled=True,
        high_watermark=0.8,
        low_watermark=0.7,
        enter_hold_s=60,
        exit_hold_s=120,
        clock=lambda: now[0],
    )

    assert controller.update(0.95).level == 0
    now[0] = 60.0
    assert controller.update(0.95).level == 1
    assert controller.snapshot().effects == ("thinking_disabled", "max_tokens_reduced")
    now[0] = 120.0
    assert controller.update(0.95).level == 2

    now[0] = 121.0
    assert controller.update(0.6).level == 2
    now[0] = 241.0
    assert controller.update(0.6).level == 0


def test_degrade_monitor_advances_hold_without_request_events_and_closes():
    now = [0.0]
    observed_three_times = threading.Event()
    observations = [0]
    controller = DegradeController(
        enabled=True,
        high_watermark=0.8,
        low_watermark=0.7,
        enter_hold_s=1,
        exit_hold_s=1,
        clock=lambda: now[0],
    )

    def utilization() -> float:
        observations[0] += 1
        now[0] += 1.0
        if observations[0] >= 3:
            observed_three_times.set()
        return 0.85

    monitor = DegradeMonitor(controller, utilization, interval_seconds=0.01)
    monitor.start()
    try:
        assert observed_three_times.wait(timeout=1)
        assert controller.level == 1
        assert monitor.running is True
    finally:
        monitor.close()

    assert monitor.running is False


def test_cancel_request_immediately_releases_sync_slot():
    from app.observability.metrics import metrics

    started = threading.Event()
    blocker = threading.Event()
    orphan_calls_before = metrics.llm_orphan_calls_total
    pool = LLMPool("online", 1, adaptive_enabled=False)
    guarded = _guard(
        StubChatModel(started=started, blocker=blocker),
        pool=pool,
        request_id="req-cancel",
    )
    errors: list[BaseException] = []

    def run() -> None:
        try:
            guarded.invoke([HumanMessage(content="hello")])
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(timeout=1)
    assert pool.inflight == 1

    assert cancel_request("req-cancel") == 1
    assert pool.inflight == 0
    blocker.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], asyncio.CancelledError)
    assert metrics.llm_orphan_calls_total == orphan_calls_before + 1
    assert cancel_request("req-cancel") == 0


def test_cancelled_request_cannot_retry_or_acquire_a_future_slot():
    started = threading.Event()
    blocker = threading.Event()
    pool = LLMPool("online", 1, adaptive_enabled=False)
    inner = StubChatModel(
        started=started,
        blocker=blocker,
        failure_status=500,
    )
    guarded = _guard(
        inner,
        pool=pool,
        request_id="req-timeout-no-retry",
        sync_retry_sleep=lambda _seconds: None,
    )
    errors: list[BaseException] = []

    def run() -> None:
        try:
            guarded.invoke([HumanMessage(content="hello")])
        except BaseException as exc:  # cancellation intentionally skips retries
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(timeout=1)
    assert cancel_request("req-timeout-no-retry") == 1
    blocker.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert inner.calls == 1
    assert pool.inflight == 0
    assert len(errors) == 1
    assert isinstance(errors[0], asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        pool.acquire_sync(request_id="req-timeout-no-retry", timeout=0)
    assert pool.inflight == 0


@pytest.mark.asyncio
async def test_sse_outcome_deadline_releases_blocking_sync_slot_without_retry():
    from app.api import router_chat
    from app.observability.metrics import metrics

    request_id = f"req-sse-deadline-{uuid.uuid4().hex}"
    started = threading.Event()
    blocker = threading.Event()
    pool = LLMPool("online", 1, adaptive_enabled=False)
    inner = StubChatModel(
        started=started,
        blocker=blocker,
        failure_status=500,
    )
    guarded = _guard(
        inner,
        pool=pool,
        request_id=request_id,
        sync_retry_sleep=lambda _seconds: None,
    )
    errors: list[BaseException] = []
    cancelled_before = metrics.stream_cancelled_total
    feedback_timeout_before = metrics.guard_rejected_total.get(
        "first_feedback_timeout", 0
    )

    def run() -> None:
        try:
            guarded.invoke([HumanMessage(content="hello")])
        except BaseException as exc:  # cancellation intentionally skips retries
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(timeout=1)
    assert pool.inflight == 1

    lifecycle = router_chat._SSEGuardLifecycle(
        SimpleNamespace(),
        SimpleNamespace(
            tenant=SimpleNamespace(tenant_id="deadline-tenant", api_key=""),
            user_id="deadline-user",
            ),
            uuid.UUID(int=0),
            request_id,
        outcome_deadline_monotonic=time.monotonic(),
    )
    with pytest.raises(LLMOutcomeTimeoutError) as error:
        await lifecycle.raise_outcome_deadline()

    assert error.value.code == 50401
    assert error.value.reason == "first_feedback_timeout"
    assert (
        metrics.guard_rejected_total.get("first_feedback_timeout", 0)
        == feedback_timeout_before + 1
    )
    assert pool.inflight == 0
    assert metrics.stream_cancelled_total == cancelled_before
    blocker.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert inner.calls == 1
    assert len(errors) == 1
    assert isinstance(errors[0], asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        pool.acquire_sync(request_id=request_id, timeout=0)


def test_cancel_marker_wins_race_before_ticket_local_flag(monkeypatch):
    from app.observability.metrics import metrics

    request_id = f"req-cancel-race-{uuid.uuid4().hex}"
    started = threading.Event()
    blocker = threading.Event()
    cancel_entered = threading.Event()
    allow_ticket_cancel = threading.Event()
    pool = LLMPool("online", 1, adaptive_enabled=False)
    breaker = EndpointCircuitBreaker(enabled=True, min_samples=1)
    inner = StubChatModel(started=started, blocker=blocker, failure_status=500)
    guarded = _guard(inner, pool=pool, breaker=breaker, request_id=request_id)
    invoke_errors: list[BaseException] = []
    cancel_results: list[int] = []
    orphan_calls_before = metrics.llm_orphan_calls_total
    original_cancel = LLMSlotTicket.cancel

    def delayed_cancel(ticket: LLMSlotTicket) -> bool:
        cancel_entered.set()
        allow_ticket_cancel.wait(timeout=2)
        return original_cancel(ticket)

    def invoke_sync() -> None:
        try:
            guarded.invoke([HumanMessage(content="hello")])
        except BaseException as exc:  # cancellation is expected
            invoke_errors.append(exc)

    monkeypatch.setattr(LLMSlotTicket, "cancel", delayed_cancel)
    invoke_thread = threading.Thread(target=invoke_sync)
    invoke_thread.start()
    assert started.wait(timeout=1)

    cancel_thread = threading.Thread(
        target=lambda: cancel_results.append(cancel_request(request_id))
    )
    cancel_thread.start()
    assert cancel_entered.wait(timeout=1)

    # The global marker is already visible even though ticket.cancel() is paused.
    blocker.set()
    invoke_thread.join(timeout=2)

    assert not invoke_thread.is_alive()
    assert len(invoke_errors) == 1
    assert isinstance(invoke_errors[0], asyncio.CancelledError)
    assert inner.calls == 1
    assert pool.inflight == 0
    assert breaker.state((guarded.base_url, guarded.model_name)) == CircuitMode.CLOSED

    allow_ticket_cancel.set()
    cancel_thread.join(timeout=2)

    assert not cancel_thread.is_alive()
    assert cancel_results == [0]
    assert metrics.llm_orphan_calls_total == orphan_calls_before + 1


@pytest.mark.asyncio
async def test_graph_runtime_timeout_releases_slot_and_stops_sync_retry(
    monkeypatch,
):
    from app.harness import graph_runtime as graph_runtime_module
    from app.harness.graph_runtime import GraphRuntime
    from app.observability.metrics import metrics

    request_id = f"req-runtime-timeout-{uuid.uuid4().hex}"
    started = threading.Event()
    blocker = threading.Event()
    pool = LLMPool("online", 1, adaptive_enabled=False)
    breaker = EndpointCircuitBreaker(enabled=True, min_samples=1)
    inner = BlockingFailStreamingModel(started=started, blocker=blocker)
    guarded = _guard(
        inner,
        pool=pool,
        breaker=breaker,
        request_id=request_id,
        sync_retry_sleep=lambda _seconds: None,
    )
    thread_errors: list[BaseException] = []
    cancelled_request_ids: list[str] = []
    orphan_calls_before = metrics.llm_orphan_calls_total
    response_timeout_before = metrics.guard_rejected_total.get(
        "model_response_timeout", 0
    )
    slot_timeout_before = metrics.guard_rejected_total.get("llm_slot_timeout", 0)

    def invoke_sync() -> None:
        try:
            guarded.invoke([HumanMessage(content="hello")])
        except BaseException as exc:  # cancellation is the expected terminal state
            thread_errors.append(exc)

    class NoEventGraph:
        def __init__(self) -> None:
            self.thread = threading.Thread(target=invoke_sync)

        async def astream_events(self, *_args, **_kwargs):
            self.thread.start()
            while not started.is_set():
                await asyncio.sleep(0)
            while self.thread.is_alive():
                await asyncio.sleep(60)
            if False:  # pragma: no cover - make this an async generator
                yield {}

    graph = NoEventGraph()
    real_cancel_request = cancel_request

    def track_cancel_request(value: str) -> int:
        cancelled_request_ids.append(value)
        return real_cancel_request(value)

    monkeypatch.setattr(graph_runtime_module.settings, "AGENT_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(graph_runtime_module, "cancel_request", track_cancel_request)
    runtime = GraphRuntime(graph)

    frames = [
        frame
        async for frame in runtime.astream(
            "thread-timeout",
            "hello",
            "tenant-a",
            "user-a",
            [],
            request_id=request_id,
        )
    ]

    assert cancelled_request_ids == [request_id]
    joined = "\n".join(frames)
    assert '"code": 50402' in joined
    assert "模型响应超时，请稍后重试" in joined
    assert (
        metrics.guard_rejected_total.get("model_response_timeout", 0)
        == response_timeout_before + 1
    )
    assert metrics.guard_rejected_total.get("llm_slot_timeout", 0) == slot_timeout_before
    assert pool.inflight == 0
    assert graph.thread.is_alive()

    blocker.set()
    graph.thread.join(timeout=2)

    assert not graph.thread.is_alive()
    assert inner.calls == 1
    assert len(thread_errors) == 1
    assert isinstance(thread_errors[0], asyncio.CancelledError)
    assert pool.inflight == 0
    assert breaker.state((guarded.base_url, guarded.model_name)) == CircuitMode.CLOSED
    assert metrics.llm_orphan_calls_total == orphan_calls_before + 1


def test_create_llm_snapshots_channel_degrade_provider_and_fallback(monkeypatch):
    from app.agents import base_agent
    from app.harness.context import ToolContext, reset_tool_context, set_tool_context

    captured: list[dict[str, Any]] = []

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    monkeypatch.setattr(base_agent, "ChatOpenAI", FakeChatOpenAI)
    monkeypatch.setattr(base_agent.settings, "GUARD_ENABLED", True)
    monkeypatch.setattr(base_agent.settings, "LLM_MAX_TOKENS", 4000)
    monkeypatch.setattr(base_agent.settings, "LLM_FALLBACK_MODELS", "backup,primary,backup")
    set_tool_context(
        ToolContext(
            user_id="user-1",
            model_id="Qwen3.8-primary",
            model_base_url="http://vllm.internal/v1",
            model_api_key="",
            call_channel="background",
            degrade_level=1,
            request_id="req-1",
        )
    )
    try:
        model = base_agent.create_llm()
    finally:
        reset_tool_context()

    assert isinstance(model, GuardedChatModel)
    assert model.pool.name == "background"
    assert [item["model"] for item in captured] == ["Qwen3.8-primary", "backup", "primary"]
    assert captured[0]["max_tokens"] == 2000
    assert captured[0]["max_retries"] == 0
    assert captured[0]["extra_body"] == {"thinking_mode": "none"}
    assert captured[0]["model_kwargs"] == {"user": "user-1"}


def test_d2_history_trim_preserves_system_prefix_and_recent_messages():
    from app.agents.base_agent import _messages_for_degrade

    system = SystemMessage(content="system")
    history = [HumanMessage(content=f"message-{index}") for index in range(10)]

    trimmed = _messages_for_degrade([system, *history], 2)

    assert trimmed[0] is system
    assert [item.content for item in trimmed[1:]] == [f"message-{index}" for index in range(4, 10)]


def test_d2_history_trim_keeps_latest_user_for_attachment_injection():
    from app.agents.base_agent import _messages_for_degrade, _with_attachments

    system_messages = [
        SystemMessage(content="base-system"),
        SystemMessage(content="tenant-system"),
    ]
    current_user = HumanMessage(content="请分析附件", id="current-user")
    tool_history = [AIMessage(content=f"tool-round-{index}") for index in range(7)]

    trimmed = _messages_for_degrade([*system_messages, current_user, *tool_history], 2)
    enriched = _with_attachments(
        trimmed,
        [
            {"kind": "document", "name": "report.txt", "text": "附件正文"},
            {"kind": "image", "name": "chart.png", "data_url": "data:image/png;base64,AA=="},
        ],
    )

    assert trimmed[:2] == system_messages
    assert trimmed[2] is current_user
    assert trimmed[3:] == tool_history[-6:]
    assert isinstance(enriched[2], HumanMessage)
    assert enriched[2].id == "current-user"
    assert enriched[2].content == [
        {
            "type": "text",
            "text": "请分析附件\n\n--- 附件：report.txt ---\n附件正文\n--- 附件结束 ---",
        },
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
    ]


def test_d2_history_trim_keeps_tool_exchange_atomic_and_omits_orphans():
    from app.agents.base_agent import _messages_for_degrade

    tool_request = AIMessage(
        content="",
        tool_calls=[
            {"name": "lookup", "args": {"id": 1}, "id": "call-1", "type": "tool_call"},
            {"name": "lookup", "args": {"id": 2}, "id": "call-2", "type": "tool_call"},
        ],
    )
    tool_result_1 = ToolMessage(content="result-1", tool_call_id="call-1")
    tool_result_2 = ToolMessage(content="result-2", tool_call_id="call-2")
    orphan = ToolMessage(content="orphan", tool_call_id="missing-call")
    recent = [HumanMessage(content=f"recent-{index}") for index in range(4)]
    messages = [
        HumanMessage(content="old-0"),
        HumanMessage(content="old-1"),
        HumanMessage(content="old-2"),
        tool_request,
        tool_result_1,
        tool_result_2,
        orphan,
        *recent,
    ]

    trimmed = _messages_for_degrade(messages, 2)

    assert trimmed == [tool_request, tool_result_1, tool_result_2, *recent]
    assert all(message is not orphan for message in trimmed)
    assert [message.tool_call_id for message in trimmed if isinstance(message, ToolMessage)] == [
        "call-1",
        "call-2",
    ]


def test_graph_builder_does_not_swallow_guard_errors(monkeypatch):
    from app.harness import graph_builder
    from app.tools.registry import ToolRegistry

    def fail_with_guard_error(state: dict) -> dict:
        raise LLMBusyError()

    monkeypatch.setattr(graph_builder, "agent_node", fail_with_guard_error)
    graph = graph_builder.build_main_graph(ToolRegistry())

    with pytest.raises(LLMBusyError):
        asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="hello")]}))


def test_graph_runtime_guard_error_frame_exposes_stable_code_and_retry():
    from app.harness.graph_runtime import _guard_error_frame

    frame = _guard_error_frame(LLMGuardOpenError(retry_after_ms=30_000))
    assert frame is not None
    payload = json.loads(next(line[6:] for line in frame.splitlines() if line.startswith("data: ")))
    assert payload == {
        "code": 50300,
        "message": "模型服务暂时不可用，请稍后重试",
        "retry_after_ms": 30_000,
    }


@pytest.mark.asyncio
async def test_graph_idle_timeout_has_distinct_response_timeout_contract(monkeypatch):
    from app.harness import graph_runtime as graph_runtime_module
    from app.harness.graph_runtime import GraphRuntime

    class StalledGraph:
        async def astream_events(self, *_args, **_kwargs):
            await asyncio.Event().wait()
            yield {}  # pragma: no cover - keep an async-generator shape

    monkeypatch.setattr(graph_runtime_module.settings, "AGENT_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(LLMResponseTimeoutError) as error:
        await asyncio.wait_for(
            GraphRuntime(StalledGraph()).ainvoke(
                "thread-idle-timeout",
                "hello",
                "tenant-idle-timeout",
                "user-idle-timeout",
                [],
                request_id=f"req-idle-{uuid.uuid4().hex}",
            ),
            timeout=0.2,
        )

    assert error.value.code == 50402
    assert error.value.reason == "model_response_timeout"
    assert str(error.value) == "模型响应超时，请稍后重试"


@pytest.mark.asyncio
async def test_memory_and_evaluation_llm_calls_use_background_pool(monkeypatch):
    from app.agents import base_agent
    from app.knowledge import evaluator
    from app.memory import extractor
    from app.memory import service as memory_service

    channels: list[str | None] = []

    class FakeLLM:
        async def ainvoke(self, prompt: str) -> AIMessage:
            if "faithfulness" in prompt:
                return AIMessage(content='{"faithfulness": 5, "relevancy": 4}')
            if "ADD" in prompt and "UPDATE" in prompt:
                return AIMessage(content="ADD")
            return AIMessage(content="[]")

    def fake_create_llm(*args, **kwargs):
        channels.append(kwargs.get("call_channel"))
        return FakeLLM()

    monkeypatch.setattr(base_agent, "create_llm", fake_create_llm)

    await extractor.extract("用户消息", "助手消息")
    await memory_service._decide_conflict({"content": "新记忆"}, "旧记忆")
    await evaluator.generate_answer("问题", ["上下文"])
    await evaluator.judge_answer("问题", ["上下文"], "答案")

    assert channels == ["background", "background", "background", "background"]


@pytest.mark.asyncio
async def test_orchestration_layers_propagate_guard_errors(monkeypatch):
    from app.agents.models import OrchestrationAgent
    from app.harness import synthesizer, worker
    from app.harness.context import ToolContext, reset_tool_context, set_tool_context
    from app.harness.models import TaskCard
    from app.harness.planner import create_plan
    from app.harness.router import decide_route
    from app.harness.synthesizer import synthesize
    from app.harness.worker import execute_task

    agent = OrchestrationAgent(
        agent_id=uuid.uuid4(),
        tenant_id="tenant-a",
        name="analyst",
        description="负责分析任务。",
        scope="internal",
        system_prompt=None,
        model=None,
        model_base_url=None,
        memory_enabled=False,
    )

    class FailingLLM:
        async def ainvoke(self, prompt):
            raise LLMBusyError()

    with pytest.raises(LLMBusyError):
        await decide_route(
            "分析",
            tenant_id="tenant-a",
            agents={"analyst": agent},
            llm=FailingLLM(),
        )
    with pytest.raises(LLMBusyError):
        await create_plan("分析", agents={"analyst": agent}, llm=FailingLLM())

    monkeypatch.setattr(synthesizer, "create_llm", lambda *_args, **_kwargs: FailingLLM())
    with pytest.raises(LLMBusyError):
        await synthesize(
            {
                "results": [
                    {"task_id": "1", "target_agent": "a", "result": "A", "status": "done"},
                    {"task_id": "2", "target_agent": "b", "result": "B", "status": "done"},
                ],
                "failures": [],
            }
        )

    async def fail_worker(*args, **kwargs):
        raise LLMBusyError()

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(worker, "_run_agent", fail_worker)
    monkeypatch.setattr(worker, "_persist_start", noop)
    monkeypatch.setattr(worker, "_persist_finish", noop)
    monkeypatch.setattr(worker, "emit", noop)
    set_tool_context(ToolContext(tenant_id="tenant-a", orchestration_agents={"analyst": agent}))
    try:
        with pytest.raises(LLMBusyError):
            await execute_task(
                {"tenant_id": "tenant-a"},
                TaskCard(target_agent="analyst", goal="分析").model_dump(),
                [],
            )
    finally:
        reset_tool_context()


@pytest.mark.asyncio
async def test_stream_guard_error_emits_no_fake_assistant_delta(monkeypatch):
    from app.harness import graph_builder
    from app.harness.graph_runtime import GraphRuntime
    from app.tools.registry import ToolRegistry

    def fail_with_guard_error(state: dict) -> dict:
        raise LLMGuardOpenError(retry_after_ms=30_000)

    monkeypatch.setattr(graph_builder, "agent_node", fail_with_guard_error)
    runtime = GraphRuntime(graph_builder.build_main_graph(ToolRegistry()))

    frames = [
        frame
        async for frame in runtime.astream(
            "thread-guard-error",
            "hello",
            "tenant-a",
            "user-a",
            [],
            request_id="req-guard-error",
        )
    ]
    joined = "\n".join(frames)

    assert '"code": 50300' in joined
    assert "event: message_delta" not in joined


@pytest.mark.asyncio
async def test_stream_model_failure_is_an_error_not_a_cacheable_assistant(monkeypatch):
    from app.harness import graph_builder
    from app.harness.graph_runtime import GraphRuntime
    from app.tools.registry import ToolRegistry

    def fail_model(_state: dict) -> dict:
        raise RuntimeError("provider authentication failed")

    monkeypatch.setattr(graph_builder, "agent_node", fail_model)
    runtime = GraphRuntime(graph_builder.build_main_graph(ToolRegistry()))

    frames = [
        frame
        async for frame in runtime.astream(
            "thread-model-error",
            "hello",
            "tenant-model-error",
            "user-a",
            [],
            request_id="req-model-error",
        )
    ]
    joined = "\n".join(frames)

    assert '"code": 50001' in joined
    assert "模型执行失败，请稍后重试" in joined
    assert "[Agent 执行出错]" not in joined
    assert "event: message_delta" not in joined
    assert '"status": "error"' in joined


@pytest.mark.asyncio
async def test_stream_empty_graph_result_is_an_error_not_a_dummy_answer():
    from app.harness.graph_runtime import GraphRuntime

    class EmptyGraphApp:
        async def astream_events(self, *_args, **_kwargs):
            if False:  # pragma: no cover - keep an async-generator shape
                yield {}

    frames = [
        frame
        async for frame in GraphRuntime(EmptyGraphApp()).astream(
            "thread-empty-result",
            "hello",
            "tenant-empty-result",
            "user-a",
            [],
            request_id="req-empty-result",
        )
    ]
    joined = "\n".join(frames)

    assert '"code": 50002' in joined
    assert "执行未生成回复，请稍后重试" in joined
    assert "未收到模型回复" not in joined
    assert "event: message_delta" not in joined
    assert '"status": "error"' in joined


@pytest.mark.asyncio
async def test_non_stream_empty_graph_result_has_explicit_error_status():
    from app.harness.graph_runtime import GraphRuntime

    class EmptyGraphApp:
        async def astream_events(self, *_args, **_kwargs):
            if False:  # pragma: no cover - keep an async-generator shape
                yield {}

        async def aget_state(self, *_args, **_kwargs):
            return SimpleNamespace(values={})

    result = await GraphRuntime(EmptyGraphApp()).ainvoke(
        "thread-empty-invoke",
        "hello",
        "tenant-empty-invoke",
        "user-a",
        [],
        request_id="req-empty-invoke",
    )

    assert result["_observability"]["status"] == "error"
    assert result["_observability"]["error_code"] == 50002
    assert result["_observability"]["error"] == "graph produced no assistant response"


@pytest.mark.asyncio
async def test_d2_caps_rag_after_retrieval_to_three(monkeypatch):
    from app.harness.context import ToolContext, reset_tool_context, set_tool_context
    from app.tools import knowledge_search as knowledge_search_module

    captured: dict[str, int] = {}

    class FakeSessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def fake_retrieve(*args, **kwargs):
        captured["top_k"] = kwargs["top_k"]
        return []

    async def fake_audit(*args, **kwargs):
        return None

    monkeypatch.setattr(knowledge_search_module, "AsyncSessionLocal", FakeSessionContext)
    monkeypatch.setattr(knowledge_search_module.knowledge_service, "retrieve_v2", fake_retrieve)
    monkeypatch.setattr(knowledge_search_module.audit, "audit", fake_audit)
    set_tool_context(ToolContext(tenant_id="tenant-a", user_id="user-a", degrade_level=2))
    try:
        await knowledge_search_module._knowledge_search_async("query", top_k=10, rerank=True)
    finally:
        reset_tool_context()

    assert captured["top_k"] == 3


@pytest.mark.asyncio
async def test_graph_rag_uses_request_degrade_top_k(monkeypatch):
    from app.config import settings
    from app.harness.context import ToolContext, reset_tool_context, set_tool_context
    from app.tools import knowledge_graph_search as graph_search_module

    observed: list[int] = []

    class FakeSessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def fake_run(*_args, top_k, **_kwargs):
        observed.append(top_k)
        return {
            "entities": [],
            "relations": [],
            "communities": [],
            "events": [],
            "evidence_chunk_ids": [],
            "hits": [],
        }

    async def fake_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(graph_search_module, "AsyncSessionLocal", FakeSessionContext)
    monkeypatch.setattr(graph_search_module, "_run", fake_run)
    monkeypatch.setattr(graph_search_module.audit, "audit", fake_audit)
    monkeypatch.setattr(settings, "RAG_TOP_K", 5)

    try:
        set_tool_context(ToolContext(tenant_id="tenant-a", degrade_level=0))
        await graph_search_module._graph_search_async("query")
        set_tool_context(ToolContext(tenant_id="tenant-a", degrade_level=2))
        await graph_search_module._graph_search_async("query")
    finally:
        reset_tool_context()

    assert observed == [5, 3]
