"""Integration coverage for SSE admission, recovery frames, and cancellation."""

from __future__ import annotations

import asyncio
import time
import uuid
from types import SimpleNamespace

import pytest

from app.agents.models import ResolvedAgentCapabilities
from app.api import router_chat
from app.harness import events
from app.harness.app_runtime import harness_app
from app.harness.context import (
    ToolContext,
    get_tool_context,
    reset_tool_context,
    set_tool_context,
)
from app.harness.llm_guard import LLMGuardOpenError, LLMOutcomeTimeoutError
from app.harness.stream_gate import (
    StreamGate,
    StreamQueueTimeoutError,
    UserStreamLock,
)
from app.observability.metrics import metrics
from app.security.api_key import hash_api_key

AUTH = {"X-API-Key": "test-key-1"}


class _NeverDisconnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


def _direct_lifecycle(deadline: float) -> router_chat._SSEGuardLifecycle:
    auth = SimpleNamespace(
        tenant=SimpleNamespace(tenant_id="deadline-tenant"),
        user_id="deadline-user",
    )
    return router_chat._SSEGuardLifecycle(
        _NeverDisconnectedRequest(),
        auth,
        uuid.UUID(int=0),
        "deadline-request",
        outcome_deadline_monotonic=deadline,
    )


async def _new_session(app_client) -> str:
    response = await app_client.post("/api/v1/sessions", headers=AUTH, json={})
    assert response.status_code == 200
    return response.json()["data"]["session_id"]


def _install_guard(monkeypatch, gate: StreamGate) -> UserStreamLock:
    lock = UserStreamLock(ttl_seconds=180)
    monkeypatch.setattr(router_chat.settings, "GUARD_ENABLED", True)
    monkeypatch.setattr(router_chat, "stream_gate", gate)
    monkeypatch.setattr(router_chat, "user_stream_lock", lock)

    async def successful_stream(
        _thread_id,
        _message,
        _tenant_id,
        _user_id,
        _skill_names,
        *,
        session_id=None,
        **_kwargs,
    ):
        if session_id:
            yield events.session_start(session_id, "test-thread")
        yield events.message_delta("test-message", "测试回复")
        yield events.run_end(status="success", duration_ms=1, tool_calls=0)

    async def successful_resume(*_args, **_kwargs):
        yield events.message_delta("resume-message", "恢复回复")
        yield events.run_end(status="success", duration_ms=1, tool_calls=0)

    monkeypatch.setattr(harness_app.runtime, "astream", successful_stream)
    monkeypatch.setattr(harness_app.runtime, "resume", successful_resume)
    return lock


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome_frame",
    [
        events.message_delta("first-message", "首个 token"),
        events.error(50300, "模型服务暂时不可用", retry_after_ms=1000),
        *[
            router_chat._sse_frame(event_name, {"visible": True})
            for event_name in (
                "orchestration.router_decision",
                "orchestration.clarification",
                "orchestration.plan",
                "orchestration.worker_started",
                "orchestration.worker_finished",
                "orchestration.synthesis",
            )
        ],
    ],
)
async def test_sse_first_outcome_observer_records_first_visible_feedback_once(
    monkeypatch,
    outcome_frame,
):
    observed: list[float] = []
    monkeypatch.setattr(
        router_chat.metrics,
        "record_guard_first_outcome",
        observed.append,
    )

    async def stream():
        yield router_chat._sse_frame(
            "queue",
            {"position": 1, "queue_size": 1, "estimated_wait_ms": 1000},
        )
        yield outcome_frame
        yield events.error(50000, "later error")

    started = time.monotonic() - 0.25
    frames = [
        frame
        async for frame in router_chat._observe_sse_first_outcome(
            stream(),
            request_started_monotonic=started,
        )
    ]

    assert len(frames) == 3
    assert len(observed) == 1
    assert 200 <= observed[0] < 2000


@pytest.mark.asyncio
async def test_internal_run_step_does_not_satisfy_first_feedback_deadline():
    deadline = time.monotonic() + 0.02
    guard = _direct_lifecycle(deadline)
    guard.disconnect_task = asyncio.create_task(asyncio.Event().wait())
    guard.outcome_deadline_task = asyncio.create_task(
        asyncio.sleep(max(0.0, deadline - time.monotonic()))
    )
    set_tool_context(
        ToolContext(
            request_id=guard.request_id,
            outcome_deadline_monotonic=deadline,
        )
    )

    async def internal_events_only():
        yield events.run_step_start(
            step_id="internal-step",
            kind="node",
            name="router",
            parent_id=None,
        )
        await asyncio.sleep(max(0.0, deadline - time.monotonic()) + 0.01)
        yield events.message_delta("too-late", "late")

    frames: list[str] = []
    try:
        with pytest.raises(LLMOutcomeTimeoutError) as error:
            async for frame in guard.iter_runtime(internal_events_only()):
                frames.append(frame)
    finally:
        await guard.close()
        reset_tool_context()

    assert error.value.code == 50401
    assert error.value.reason == "first_feedback_timeout"
    assert any("event: run_step_start" in frame for frame in frames)


@pytest.mark.asyncio
async def test_stream_api_pushes_queue_position_before_admission(app_client, monkeypatch):
    gate = StreamGate(
        max_streams=1,
        queue_capacity=1,
        queue_timeout_seconds=1,
        position_push_interval_seconds=0.01,
    )
    _install_guard(monkeypatch, gate)
    active = await gate.acquire()
    session_id = await _new_session(app_client)

    async def release_when_queued() -> None:
        for _ in range(200):
            if gate.queue_size:
                gate.release(active)
                return
            await asyncio.sleep(0.005)
        raise AssertionError("request never entered the stream queue")

    releaser = asyncio.create_task(release_when_queued())
    response = await app_client.post(
        f"/api/v1/sessions/{session_id}/messages/stream",
        headers=AUTH,
        json={"message": "排队测试"},
    )
    await releaser

    assert response.status_code == 200
    body = response.text
    assert 'event: queue\ndata: {"position":1,"queue_size":1' in body
    assert body.index("event: queue") < body.index("event: session_start")
    assert gate.inflight == 0
    assert gate.queue_size == 0


@pytest.mark.asyncio
async def test_stream_api_returns_queue_full_error(app_client, monkeypatch):
    gate = StreamGate(max_streams=1, queue_capacity=0)
    _install_guard(monkeypatch, gate)
    active = await gate.acquire()
    session_id = await _new_session(app_client)

    response = await app_client.post(
        f"/api/v1/sessions/{session_id}/messages/stream",
        headers=AUTH,
        json={"message": "满队列测试"},
    )

    assert response.status_code == 200
    assert '"code":42900' in response.text
    assert '"retry_after_ms":' in response.text
    history = await app_client.get(
        f"/api/v1/sessions/{session_id}/messages",
        headers=AUTH,
    )
    assert history.json()["data"] == []
    gate.release(active)


@pytest.mark.asyncio
async def test_stream_api_returns_queue_timeout_error(app_client, monkeypatch):
    gate = StreamGate(
        max_streams=1,
        queue_capacity=1,
        queue_timeout_seconds=0.02,
        position_push_interval_seconds=0.01,
    )
    _install_guard(monkeypatch, gate)
    active = await gate.acquire()
    session_id = await _new_session(app_client)
    queue_wait_samples_before = metrics.guard_queue_wait_samples

    response = await app_client.post(
        f"/api/v1/sessions/{session_id}/messages/stream",
        headers=AUTH,
        json={"message": "超时测试"},
    )

    assert "event: queue" in response.text
    assert '"code":50400' in response.text
    assert gate.queue_size == 0
    assert metrics.guard_queue_length == 0
    assert metrics.guard_queue_wait_samples == queue_wait_samples_before + 1
    assert metrics.guard_queue_wait_ms[-1] > 0
    history = await app_client.get(
        f"/api/v1/sessions/{session_id}/messages",
        headers=AUTH,
    )
    assert history.json()["data"] == []
    gate.release(active)


@pytest.mark.asyncio
async def test_stream_api_enforces_one_stream_per_api_key_before_login(
    app_client,
    monkeypatch,
):
    gate = StreamGate(max_streams=2, queue_capacity=1)
    lock = _install_guard(monkeypatch, gate)
    principal = f"api-key:{hash_api_key(AUTH['X-API-Key'])}"
    session_id = await _new_session(app_client)
    assert await lock.acquire("default", principal, session_id, "existing-request")

    response = await app_client.post(
        f"/api/v1/sessions/{session_id}/messages/stream",
        headers=AUTH,
        json={"message": "重复流测试"},
    )

    assert '"code":40901' in response.text
    assert "当前会话已有任务正在进行中" in response.text
    assert gate.inflight == 0
    history = await app_client.get(
        f"/api/v1/sessions/{session_id}/messages",
        headers=AUTH,
    )
    assert history.json()["data"] == []
    assert await lock.release("default", principal, session_id, "existing-request")


@pytest.mark.asyncio
async def test_stream_api_sends_heartbeat_while_waiting_for_model_frame(
    app_client,
    monkeypatch,
):
    gate = StreamGate(max_streams=1, queue_capacity=1)
    _install_guard(monkeypatch, gate)
    monkeypatch.setattr(router_chat, "SSE_HEARTBEAT_INTERVAL_SECONDS", 0.01)

    async def delayed_stream(*_args, **_kwargs):
        await asyncio.sleep(0.035)
        yield events.message_delta("heartbeat-message", "响应完成")
        yield events.run_end(status="success", duration_ms=35, tool_calls=0)

    monkeypatch.setattr(harness_app.runtime, "astream", delayed_stream)
    session_id = await _new_session(app_client)

    response = await app_client.post(
        f"/api/v1/sessions/{session_id}/messages/stream",
        headers=AUTH,
        json={"message": "心跳测试"},
    )

    assert response.text.count(": keep-alive\n\n") >= 2
    assert "event: run_end" in response.text
    assert gate.inflight == 0


@pytest.mark.asyncio
async def test_stream_api_sends_heartbeat_during_slow_request_preparation(
    app_client,
    monkeypatch,
):
    gate = StreamGate(max_streams=1, queue_capacity=1)
    _install_guard(monkeypatch, gate)
    monkeypatch.setattr(router_chat, "SSE_HEARTBEAT_INTERVAL_SECONDS", 0.01)

    async def slow_memory(*_args, **_kwargs):
        await asyncio.sleep(0.035)
        return ""

    monkeypatch.setattr(router_chat, "_retrieve_memory_context", slow_memory)
    session_id = await _new_session(app_client)
    response = await app_client.post(
        f"/api/v1/sessions/{session_id}/messages/stream",
        headers=AUTH,
        json={"message": "请解释向量数据库"},
    )

    assert response.text.count(": keep-alive\n\n") >= 2
    assert "event: run_end" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path_suffix", "payload"),
    [
        ("messages/stream", {"message": "前置读取超时"}),
        ("resume", {"input": "继续", "decision": "approve"}),
    ],
)
async def test_sse_preflight_db_deadline_returns_504_without_resource_leak(
    app_client,
    monkeypatch,
    path_suffix,
    payload,
):
    gate = StreamGate(max_streams=1, queue_capacity=1)
    lock = _install_guard(monkeypatch, gate)
    monkeypatch.setattr(router_chat.settings, "GUARD_OUTCOME_DEADLINE_SECONDS", 0.04)
    monkeypatch.setattr(router_chat, "SSE_HEARTBEAT_INTERVAL_SECONDS", 0.005)
    session_id = await _new_session(app_client)
    original_get_session = router_chat.repo.get_session
    blocked = asyncio.Event()

    async def blocking_get_session(*_args, **_kwargs):
        await blocked.wait()

    monkeypatch.setattr(router_chat.repo, "get_session", blocking_get_session)
    cancelled_before = metrics.stream_cancelled_total
    feedback_before = metrics.guard_rejected_total.get("first_feedback_timeout", 0)
    slot_before = metrics.guard_rejected_total.get("llm_slot_timeout", 0)
    started = time.monotonic()
    response = await app_client.post(
        f"/api/v1/sessions/{session_id}/{path_suffix}",
        headers=AUTH,
        json=payload,
    )
    elapsed = time.monotonic() - started
    monkeypatch.setattr(router_chat.repo, "get_session", original_get_session)

    assert response.status_code == 200
    assert 'event: error' in response.text
    assert '"code":50401' in response.text
    assert "请求未能在规定时间内产生可见反馈" in response.text
    assert ": keep-alive\n\n" in response.text
    assert elapsed < 0.5
    assert metrics.stream_cancelled_total == cancelled_before
    assert (
        metrics.guard_rejected_total.get("first_feedback_timeout", 0)
        == feedback_before + 1
    )
    assert metrics.guard_rejected_total.get("llm_slot_timeout", 0) == slot_before
    assert gate.inflight == 0
    assert gate.queue_size == 0
    principal = f"api-key:{hash_api_key(AUTH['X-API-Key'])}"
    assert await lock.acquire("default", principal, session_id, "next-request")
    await lock.release("default", principal, session_id, "next-request")

    history = await app_client.get(
        f"/api/v1/sessions/{session_id}/messages",
        headers=AUTH,
    )
    assert history.json()["data"] == []


@pytest.mark.asyncio
async def test_stream_persist_deadline_returns_504_and_releases_admission(
    app_client,
    monkeypatch,
):
    gate = StreamGate(max_streams=1, queue_capacity=1)
    lock = _install_guard(monkeypatch, gate)
    monkeypatch.setattr(router_chat.settings, "GUARD_OUTCOME_DEADLINE_SECONDS", 0.1)
    monkeypatch.setattr(router_chat, "SSE_HEARTBEAT_INTERVAL_SECONDS", 0.005)
    session_id = await _new_session(app_client)
    original_add_message = router_chat.repo.add_message
    persist_started = asyncio.Event()
    never = asyncio.Event()
    runtime_started = False

    # Keep every preflight seam deterministic so the short deadline is spent
    # specifically inside persist_user_message rather than a cold database or
    # capability lookup.
    fake_session = SimpleNamespace(
        id=session_id,
        tenant_id="default",
        status="active",
        thread_id="persist-deadline-thread",
        title="existing title",
    )

    async def immediate_session(*_args, **_kwargs):
        return fake_session

    async def immediate_agent(*_args, **_kwargs):
        return ResolvedAgentCapabilities(memory_enabled=False)

    async def empty_list(*_args, **_kwargs):
        return []

    async def empty_dict(*_args, **_kwargs):
        return {}

    async def cache_miss(*_args, **_kwargs):
        return None

    async def blocking_add_message(*_args, **_kwargs):
        persist_started.set()
        await never.wait()

    async def runtime_must_not_start(*_args, **_kwargs):
        nonlocal runtime_started
        runtime_started = True
        yield events.run_end(status="success", duration_ms=1, tool_calls=0)

    monkeypatch.setattr(router_chat.repo, "add_message", blocking_add_message)
    monkeypatch.setattr(router_chat.repo, "get_session", immediate_session)
    monkeypatch.setattr(router_chat.repo, "list_messages", empty_list)
    monkeypatch.setattr(router_chat, "_resolve_request_agent", immediate_agent)
    monkeypatch.setattr(
        router_chat.knowledge_service,
        "resolve_ids_by_names",
        empty_list,
    )
    monkeypatch.setattr(router_chat.mcp_manager, "resolve_tools", empty_list)
    monkeypatch.setattr(router_chat, "_resolve_orchestration_context", empty_dict)
    monkeypatch.setattr(router_chat.semantic_cache, "lookup", cache_miss)
    monkeypatch.setattr(harness_app.runtime, "astream", runtime_must_not_start)
    cancelled_before = metrics.stream_cancelled_total
    feedback_before = metrics.guard_rejected_total.get("first_feedback_timeout", 0)
    slot_before = metrics.guard_rejected_total.get("llm_slot_timeout", 0)
    started = time.monotonic()
    request_task = asyncio.create_task(
        app_client.post(
            f"/api/v1/sessions/{session_id}/messages/stream",
            headers=AUTH,
            json={"message": "持久化超时"},
        )
    )
    await asyncio.wait_for(persist_started.wait(), timeout=1)
    assert gate.inflight == 1
    response = await asyncio.wait_for(request_task, timeout=1)
    elapsed = time.monotonic() - started
    monkeypatch.setattr(router_chat.repo, "add_message", original_add_message)

    assert 'event: error' in response.text
    assert '"code":50401' in response.text
    assert "请求未能在规定时间内产生可见反馈" in response.text
    assert ": keep-alive\n\n" in response.text
    assert elapsed < 0.7
    assert runtime_started is False
    assert metrics.stream_cancelled_total == cancelled_before
    assert (
        metrics.guard_rejected_total.get("first_feedback_timeout", 0)
        == feedback_before + 1
    )
    assert metrics.guard_rejected_total.get("llm_slot_timeout", 0) == slot_before
    assert gate.inflight == 0
    assert gate.queue_size == 0
    principal = f"api-key:{hash_api_key(AUTH['X-API-Key'])}"
    assert await lock.acquire("default", principal, session_id, "next-request")
    await lock.release("default", principal, session_id, "next-request")

    history = await app_client.get(
        f"/api/v1/sessions/{session_id}/messages",
        headers=AUTH,
    )
    assert history.json()["data"] == []


@pytest.mark.asyncio
async def test_preparation_and_queue_share_one_outcome_deadline(monkeypatch):
    now = [100.0]
    captured_queue_timeouts: list[float] = []

    class TimeoutGate:
        inflight = 0
        queue_size = 0

        async def acquire(self, _on_position, *, timeout_seconds):
            captured_queue_timeouts.append(timeout_seconds)
            raise StreamQueueTimeoutError("queue deadline reached")

    monkeypatch.setattr(
        router_chat,
        "time",
        SimpleNamespace(monotonic=lambda: now[0]),
    )
    monkeypatch.setattr(router_chat.settings, "GUARD_ENABLED", True)
    monkeypatch.setattr(router_chat, "stream_gate", TimeoutGate())
    guard = _direct_lifecycle(deadline=110.0)

    async def prepare_request() -> str:
        now[0] = 104.0
        return "prepared"

    try:
        assert [frame async for frame in guard.iter_operation(prepare_request())] == []
        assert guard.operation_result == "prepared"
        frames = [frame async for frame in guard.acquire_stream()]
    finally:
        await guard.close()

    assert captured_queue_timeouts == [pytest.approx(6.0)]
    assert len(frames) == 1
    assert '"code":50400' in frames[0]
    assert guard.rejection_code == 50400


@pytest.mark.asyncio
async def test_first_delta_wins_when_outcome_deadline_finishes_in_same_wait(
    monkeypatch,
):
    real_wait = asyncio.wait

    async def coalescing_wait(awaitables, **kwargs):
        tasks = set(awaitables)
        await asyncio.sleep(0)
        done = {task for task in tasks if task.done()}
        if done:
            return done, tasks - done
        return await real_wait(tasks, **kwargs)

    monkeypatch.setattr(router_chat.asyncio, "wait", coalescing_wait)
    deadline = time.monotonic() - 1.0
    guard = _direct_lifecycle(deadline)
    guard.disconnect_task = asyncio.create_task(asyncio.Event().wait())
    guard.outcome_deadline_task = asyncio.create_task(asyncio.sleep(0))
    await guard.outcome_deadline_task
    set_tool_context(
        ToolContext(
            request_id=guard.request_id,
            outcome_deadline_monotonic=deadline,
        )
    )

    async def immediate_delta():
        yield events.message_delta("deadline-message", "首个 token")
        yield events.run_end(status="success", duration_ms=1, tool_calls=0)

    try:
        frames = [frame async for frame in guard.iter_runtime(immediate_delta())]
    finally:
        await guard.close()
        reset_tool_context()

    assert any("event: message_delta" in frame for frame in frames)
    assert all('"code":50400' not in frame for frame in frames)
    assert guard.outcome_delivered is True


@pytest.mark.asyncio
async def test_first_delta_clears_deadline_for_later_tool_loop_work():
    deadline = time.monotonic() + 0.02
    observed_deadlines: list[float | None] = []
    guard = _direct_lifecycle(deadline)
    guard.disconnect_task = asyncio.create_task(asyncio.Event().wait())
    guard.outcome_deadline_task = asyncio.create_task(
        asyncio.sleep(max(0.0, deadline - time.monotonic()))
    )
    set_tool_context(
        ToolContext(
            request_id=guard.request_id,
            outcome_deadline_monotonic=deadline,
        )
    )

    async def tool_loop_after_first_delta():
        yield events.message_delta("deadline-message", "首个 token")
        await asyncio.sleep(max(0.0, deadline - time.monotonic()) + 0.01)
        observed_deadlines.append(get_tool_context().outcome_deadline_monotonic)
        yield events.run_end(status="success", duration_ms=30, tool_calls=1)

    try:
        frames = [frame async for frame in guard.iter_runtime(tool_loop_after_first_delta())]
    finally:
        await guard.close()
        reset_tool_context()

    assert time.monotonic() >= deadline
    assert observed_deadlines == [None]
    assert any("event: run_end" in frame for frame in frames)
    assert all('"code":50400' not in frame for frame in frames)


@pytest.mark.asyncio
async def test_visible_router_decision_clears_deadline_for_later_orchestration_work():
    deadline = time.monotonic() + 0.02
    observed_deadlines: list[float | None] = []
    guard = _direct_lifecycle(deadline)
    guard.disconnect_task = asyncio.create_task(asyncio.Event().wait())
    guard.outcome_deadline_task = asyncio.create_task(
        asyncio.sleep(max(0.0, deadline - time.monotonic()))
    )
    set_tool_context(
        ToolContext(
            request_id=guard.request_id,
            outcome_deadline_monotonic=deadline,
        )
    )

    async def orchestration_after_visible_feedback():
        yield router_chat._sse_frame(
            "orchestration.router_decision",
            {"mode": "orchestrate", "target_agent": "main"},
        )
        await asyncio.sleep(max(0.0, deadline - time.monotonic()) + 0.01)
        observed_deadlines.append(get_tool_context().outcome_deadline_monotonic)
        yield events.message_delta("orchestration-message", "综合完成")
        yield events.run_end(status="success", duration_ms=30, tool_calls=0)

    try:
        frames = [
            frame
            async for frame in guard.iter_runtime(
                orchestration_after_visible_feedback()
            )
        ]
    finally:
        await guard.close()
        reset_tool_context()

    assert observed_deadlines == [None]
    assert any("event: orchestration.router_decision" in frame for frame in frames)
    assert any("event: message_delta" in frame for frame in frames)
    assert all('"code":50401' not in frame for frame in frames)


@pytest.mark.asyncio
async def test_partial_reply_before_guard_error_is_not_persisted_or_memorized(
    app_client,
    monkeypatch,
):
    gate = StreamGate(max_streams=1, queue_capacity=1)
    _install_guard(monkeypatch, gate)
    memory_calls: list[dict] = []
    monkeypatch.setattr(
        router_chat,
        "_schedule_memory_extract",
        lambda **kwargs: memory_calls.append(kwargs),
    )

    async def partial_then_error(*_args, **_kwargs):
        yield events.message_delta("partial", "不完整回答")
        yield events.error(50300, "模型服务暂时不可用", retry_after_ms=1234)
        yield events.run_end(status="error", duration_ms=1, tool_calls=0)

    monkeypatch.setattr(harness_app.runtime, "astream", partial_then_error)
    session_id = await _new_session(app_client)
    response = await app_client.post(
        f"/api/v1/sessions/{session_id}/messages/stream",
        headers=AUTH,
        json={"message": "什么是 RAG？"},
    )

    assert '"code": 50300' in response.text
    history = await app_client.get(
        f"/api/v1/sessions/{session_id}/messages",
        headers=AUTH,
    )
    assert [item["role"] for item in history.json()["data"]] == ["user"]
    assert memory_calls == []


@pytest.mark.asyncio
async def test_stream_disconnect_cancels_runtime_and_releases_all_leases(
    app_client,
    monkeypatch,
):
    gate = StreamGate(max_streams=1, queue_capacity=1)
    lock = _install_guard(monkeypatch, gate)
    started = asyncio.Event()
    stopped = asyncio.Event()
    never = asyncio.Event()

    async def endless_stream(*_args, **_kwargs):
        started.set()
        try:
            await never.wait()
            yield ""  # pragma: no cover - makes this an async generator
        finally:
            stopped.set()

    monkeypatch.setattr(harness_app.runtime, "astream", endless_stream)
    session_id = await _new_session(app_client)
    cancelled_before = metrics.stream_cancelled_total
    request_task = asyncio.create_task(
        app_client.post(
            f"/api/v1/sessions/{session_id}/messages/stream",
            headers=AUTH,
            json={"message": "断连测试"},
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    assert gate.inflight == 1

    request_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request_task
    await asyncio.wait_for(stopped.wait(), timeout=1)

    assert gate.inflight == 0
    assert gate.queue_size == 0
    assert metrics.guard_queue_length == 0
    assert metrics.stream_cancelled_total == cancelled_before + 1
    principal = f"api-key:{hash_api_key(AUTH['X-API-Key'])}"
    assert await lock.acquire("default", principal, session_id, "next-request")
    await lock.release("default", principal, session_id, "next-request")

    history = await app_client.get(
        f"/api/v1/sessions/{session_id}/messages",
        headers=AUTH,
    )
    roles = [message["role"] for message in history.json()["data"]]
    assert roles == ["user"]


@pytest.mark.asyncio
async def test_resume_uses_same_user_lock_and_never_enters_gate(
    app_client,
    monkeypatch,
):
    gate = StreamGate(max_streams=1, queue_capacity=1)
    lock = _install_guard(monkeypatch, gate)
    principal = f"api-key:{hash_api_key(AUTH['X-API-Key'])}"
    session_id = await _new_session(app_client)
    assert await lock.acquire("default", principal, session_id, "existing-request")

    response = await app_client.post(
        f"/api/v1/sessions/{session_id}/resume",
        headers=AUTH,
        json={"input": "继续", "decision": "approve"},
    )

    assert '"code":40901' in response.text
    assert gate.inflight == 0
    assert gate.queue_size == 0
    assert await lock.release("default", principal, session_id, "existing-request")


@pytest.mark.asyncio
async def test_resume_pushes_queue_and_heartbeat_then_releases_gate(
    app_client,
    monkeypatch,
):
    gate = StreamGate(
        max_streams=1,
        queue_capacity=1,
        queue_timeout_seconds=1,
        position_push_interval_seconds=0.01,
    )
    _install_guard(monkeypatch, gate)
    monkeypatch.setattr(router_chat, "SSE_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    active = await gate.acquire()
    seen_request_ids: list[str | None] = []

    async def delayed_resume(*_args, **_kwargs):
        seen_request_ids.append(get_tool_context().request_id)
        await asyncio.sleep(0.035)
        yield events.message_delta("resume-message", "恢复完成")
        yield events.run_end(status="success", duration_ms=35, tool_calls=0)

    monkeypatch.setattr(harness_app.runtime, "resume", delayed_resume)
    session_id = await _new_session(app_client)

    async def release_when_queued() -> None:
        for _ in range(200):
            if gate.queue_size:
                gate.release(active)
                return
            await asyncio.sleep(0.005)
        raise AssertionError("resume never entered the stream queue")

    releaser = asyncio.create_task(release_when_queued())
    response = await app_client.post(
        f"/api/v1/sessions/{session_id}/resume",
        headers=AUTH,
        json={"input": "继续", "decision": "approve"},
    )
    await releaser

    assert "event: queue" in response.text
    assert response.text.count(": keep-alive\n\n") >= 2
    assert "event: message_delta" in response.text
    assert "event: run_end" in response.text
    assert seen_request_ids == [response.headers["x-request-id"]]
    assert gate.inflight == 0
    assert gate.queue_size == 0


@pytest.mark.asyncio
async def test_resume_without_delta_or_error_becomes_explicit_retryable_error(
    app_client,
    monkeypatch,
):
    gate = StreamGate(max_streams=1, queue_capacity=1)
    _install_guard(monkeypatch, gate)

    async def empty_resume(*_args, **_kwargs):
        yield events.run_end(status="success", duration_ms=1, tool_calls=0)

    monkeypatch.setattr(harness_app.runtime, "resume", empty_resume)
    session_id = await _new_session(app_client)
    outcome_before = metrics.guard_first_outcome_samples

    response = await app_client.post(
        f"/api/v1/sessions/{session_id}/resume",
        headers=AUTH,
        json={"input": "继续"},
    )

    assert "event: error" in response.text
    assert '"code":50002' in response.text
    assert '"retry_after_ms":1000' in response.text
    assert '"status":"success"' not in response.text
    assert metrics.guard_first_outcome_samples == outcome_before + 1
    assert gate.inflight == 0


@pytest.mark.asyncio
async def test_resume_disconnect_cancels_runtime_and_releases_leases(
    app_client,
    monkeypatch,
):
    gate = StreamGate(max_streams=1, queue_capacity=1)
    lock = _install_guard(monkeypatch, gate)
    started = asyncio.Event()
    stopped = asyncio.Event()
    never = asyncio.Event()

    async def endless_resume(*_args, **_kwargs):
        started.set()
        try:
            await never.wait()
            yield ""  # pragma: no cover
        finally:
            stopped.set()

    monkeypatch.setattr(harness_app.runtime, "resume", endless_resume)
    session_id = await _new_session(app_client)
    request_task = asyncio.create_task(
        app_client.post(
            f"/api/v1/sessions/{session_id}/resume",
            headers=AUTH,
            json={"input": "继续"},
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    assert gate.inflight == 1

    request_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request_task
    await asyncio.wait_for(stopped.wait(), timeout=1)

    assert gate.inflight == 0
    principal = f"api-key:{hash_api_key(AUTH['X-API-Key'])}"
    assert await lock.acquire("default", principal, session_id, "next-request")
    await lock.release("default", principal, session_id, "next-request")


@pytest.mark.asyncio
async def test_resume_preserves_llm_guard_error_code(app_client, monkeypatch):
    gate = StreamGate(max_streams=1, queue_capacity=1)
    _install_guard(monkeypatch, gate)

    async def failing_resume(*_args, **_kwargs):
        raise LLMGuardOpenError(retry_after_ms=1234)
        yield ""  # pragma: no cover

    monkeypatch.setattr(harness_app.runtime, "resume", failing_resume)
    session_id = await _new_session(app_client)
    before = metrics.guard_rejected_total.get("cb_open", 0)

    response = await app_client.post(
        f"/api/v1/sessions/{session_id}/resume",
        headers=AUTH,
        json={"input": "继续"},
    )

    assert '"code":50300' in response.text
    assert '"retry_after_ms":1234' in response.text
    assert gate.inflight == 0
    assert metrics.guard_rejected_total.get("cb_open", 0) == before + 1


@pytest.mark.asyncio
async def test_lost_user_lease_cancels_active_stream_with_explicit_error(
    app_client,
    monkeypatch,
):
    gate = StreamGate(max_streams=1, queue_capacity=1)
    _install_guard(monkeypatch, gate)
    started = asyncio.Event()
    stopped = asyncio.Event()
    never = asyncio.Event()

    class LostLeaseLock:
        renew_interval_seconds = 0.01

        async def acquire(self, *_args):
            return True

        async def renew(self, *_args):
            await started.wait()
            return False

        async def release(self, *_args):
            return True

    async def endless_stream(*_args, **_kwargs):
        started.set()
        try:
            await never.wait()
            yield ""  # pragma: no cover
        finally:
            stopped.set()

    monkeypatch.setattr(router_chat, "user_stream_lock", LostLeaseLock())
    monkeypatch.setattr(harness_app.runtime, "astream", endless_stream)
    session_id = await _new_session(app_client)

    response = await app_client.post(
        f"/api/v1/sessions/{session_id}/messages/stream",
        headers=AUTH,
        json={"message": "租约丢失测试"},
    )
    await asyncio.wait_for(stopped.wait(), timeout=1)

    assert '"code":40901' in response.text
    assert "对话租约已失效" in response.text
    assert gate.inflight == 0
