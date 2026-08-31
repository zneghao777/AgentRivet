"""M0 red tests for the frozen observability v2 collection contract."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from sqlalchemy import select

from app.harness.graph_runtime import GraphRuntime
from app.harness.llm_guard import LLMResponseTimeoutError
from app.observability import tracing
from app.storage.db import AsyncSessionLocal
from app.storage.models import AgentSpan
from tests.observability_v2_fakes import (
    FIRST_USAGE,
    MODEL_NAME,
    SECOND_USAGE,
    TRACE_USAGE,
    build_error_graph,
    build_interrupt_graph,
    build_slow_graph,
    build_tool_loop_graph,
)


@pytest.fixture
def observability_enabled(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "OBSERVABILITY_ENABLED", True)
    monkeypatch.setattr(settings, "TRACE_SAMPLE_RATE", 1.0)
    monkeypatch.setattr(settings, "CONTENT_CAPTURE_MODE", "redacted")
    tracing.init_tracing(lambda: AsyncSessionLocal())
    return settings


async def _flush() -> None:
    flush = getattr(tracing, "flush_tracing", None)
    if flush is not None:
        await flush(timeout=5.0)
        return
    state = tracing._state.get(id(asyncio.get_running_loop()))
    if state is not None:
        await asyncio.wait_for(state[0].join(), timeout=5.0)


async def _tenant_spans(tenant_id: str) -> list[AgentSpan]:
    await _flush()
    async with AsyncSessionLocal() as db:
        return list(
            (
                await db.execute(
                    select(AgentSpan)
                    .where(AgentSpan.tenant_id == tenant_id)
                    .order_by(AgentSpan.started_at.asc())
                )
            )
            .scalars()
            .all()
        )


async def _stream(runtime: GraphRuntime, tenant_id: str, **kwargs) -> list[str]:
    frames: list[str] = []
    thread_id = kwargs.pop("thread_id", f"thread-{uuid.uuid4().hex}")
    async for frame in runtime.astream(
        thread_id,
        "exercise the real graph",
        tenant_id,
        "user-v2",
        [],
        **kwargs,
    ):
        frames.append(frame)
    return frames


def _event_payload(frames: list[str], event_name: str) -> dict | None:
    for frame in frames:
        lines = frame.splitlines()
        if f"event: {event_name}" not in lines:
            continue
        data = next((line[6:] for line in lines if line.startswith("data: ")), None)
        return json.loads(data) if data else {}
    return None


def _event_payloads(frames: list[str], event_name: str) -> list[dict]:
    payloads: list[dict] = []
    for frame in frames:
        lines = frame.splitlines()
        if f"event: {event_name}" not in lines:
            continue
        data = next((line[6:] for line in lines if line.startswith("data: ")), None)
        payloads.append(json.loads(data) if data else {})
    return payloads


def _root(spans: list[AgentSpan]) -> AgentSpan:
    return next(span for span in spans if span.span_type == "root")


def _usage_value(span: AgentSpan, canonical: str, legacy: str) -> int | None:
    usage = span.token_usage or {}
    return usage.get(canonical, usage.get(legacy))


class _EmptyThenTextStreamApp:
    """Emit an empty provider chunk before the first user-visible token."""

    async def astream_events(self, *_args, **_kwargs):
        run_id = "llm-empty-then-text"
        base = {
            "name": "empty-then-text",
            "run_id": run_id,
            "parent_ids": [],
            "metadata": {"langgraph_node": "agent"},
        }
        yield {
            **base,
            "event": "on_chat_model_start",
            "data": {"input": [[("human", "hello")]], "model": MODEL_NAME},
        }
        yield {
            **base,
            "event": "on_chat_model_stream",
            "data": {"chunk": AIMessageChunk(content="")},
        }
        await asyncio.sleep(0.03)
        yield {
            **base,
            "event": "on_chat_model_stream",
            "data": {"chunk": AIMessageChunk(content="visible")},
        }
        yield {
            **base,
            "event": "on_chat_model_end",
            "data": {
                "output": AIMessage(
                    content="visible",
                    usage_metadata={
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "total_tokens": 2,
                    },
                )
            },
        }


class _StartThenStallApp:
    """Start one observable node, then stop producing graph events."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.state_reads = 0

    async def astream_events(self, *_args, **_kwargs):
        yield {
            "event": "on_chain_start",
            "name": "slow_node",
            "run_id": "slow-node-run",
            "parent_ids": [],
            "data": {"input": {"messages": [("human", "hello")]}},
        }
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        if False:  # pragma: no cover - keep this an async generator
            yield {}

    async def aget_state(self, *_args, **_kwargs):
        self.state_reads += 1
        raise AssertionError("a timed-out run must not wait for a state snapshot")


@pytest.mark.asyncio
async def test_trace_context_and_status_contract(observability_enabled):
    assert hasattr(tracing, "TraceContext")
    assert hasattr(tracing, "SpanStatus")
    assert hasattr(tracing, "get_trace_context")
    session_id = uuid.uuid4()
    handle = tracing.start_root_span(
        name="agent.run",
        tenant_id="tenant-context-v2",
        session_id=session_id,
        user_id="user-v2",
        request_id="request-v2",
        thread_id="thread-v2",
        links=("prior-trace",),
        content_capture_mode="redacted",
    )
    context = tracing.get_trace_context()
    assert context.trace_id == handle.trace_id
    assert context.root_span_id == handle.span_id
    assert context.current_span_id == handle.span_id
    assert context.request_id == "request-v2"
    assert context.tenant_id == "tenant-context-v2"
    assert context.session_id == session_id
    assert context.thread_id == "thread-v2"
    assert context.sampled is True
    assert context.links == ("prior-trace",)
    statuses = {status.value for status in tracing.SpanStatus}
    assert {"queued", "running", "ok", "error", "timeout", "canceled", "interrupted"} <= statuses


@pytest.mark.asyncio
async def test_sse_emits_trace_start_with_request_id(observability_enabled):
    tenant_id = f"tenant-sse-{uuid.uuid4().hex}"
    frames = await _stream(
        GraphRuntime(build_tool_loop_graph()),
        tenant_id,
        request_id="request-sse-v2",
    )
    payload = _event_payload(frames, "trace_start")
    assert payload is not None
    assert payload["request_id"] == "request-sse-v2"
    spans = await _tenant_spans(tenant_id)
    root = _root(spans)
    assert payload["trace_id"] == root.trace_id
    assert root.request_id == "request-sse-v2"


@pytest.mark.asyncio
async def test_ttft_ignores_empty_provider_chunks(observability_enabled):
    tenant_id = f"tenant-ttft-{uuid.uuid4().hex}"
    frames = await _stream(GraphRuntime(_EmptyThenTextStreamApp()), tenant_id)

    updates = _event_payloads(frames, "run_step_update")
    assert len(updates) == 1
    assert updates[0]["phase"] == "first_token"
    assert updates[0]["detail"]["ttft_ms"] >= 20

    spans = await _tenant_spans(tenant_id)
    llm = next(span for span in spans if span.span_type == "llm")
    assert llm.ttft_ms is not None
    assert llm.ttft_ms >= 20


@pytest.mark.asyncio
async def test_sse_emits_stable_auditable_run_steps(observability_enabled):
    frames = await _stream(
        GraphRuntime(build_tool_loop_graph()),
        f"tenant-steps-{uuid.uuid4().hex}",
    )
    starts = _event_payloads(frames, "run_step_start")
    ends = _event_payloads(frames, "run_step_end")
    updates = _event_payloads(frames, "run_step_update")
    assert {item["kind"] for item in starts} >= {"node", "llm", "tool"}
    assert {item["id"] for item in starts} == {item["id"] for item in ends}
    assert all(item["phase"] == "first_token" for item in updates)
    kind_by_id = {item["id"]: item["kind"] for item in starts}
    llm_summaries = [item["summary"] for item in ends if kind_by_id[item["id"]] == "llm"]
    assert any(summary and "observability_lookup" in summary for summary in llm_summaries)
    assert "deterministic final answer" in llm_summaries
    tool_start = _event_payload(frames, "tool_start")
    tool_end = _event_payload(frames, "tool_end")
    assert tool_start is not None and tool_end is not None
    assert tool_start["id"] == tool_end["id"]
    assert tool_start["arguments"] == {"query": "frozen-contract"}
    assert "lookup-result" in tool_end["result"]


@pytest.mark.asyncio
async def test_real_langgraph_maps_parent_ids_and_sequence(observability_enabled):
    tenant_id = f"tenant-tree-{uuid.uuid4().hex}"
    await _stream(GraphRuntime(build_tool_loop_graph()), tenant_id)
    spans = await _tenant_spans(tenant_id)
    by_id = {span.span_id: span for span in spans}
    llms = [span for span in spans if span.span_type == "llm"]
    tools = [span for span in spans if span.span_type == "tool"]
    assert len(llms) == 2
    assert len(tools) == 1
    assert all(by_id[span.parent_span_id].span_name == "node.agent" for span in llms)
    assert by_id[tools[0].parent_span_id].span_name == "node.tools"
    assert llms[0].started_at <= tools[0].started_at <= llms[1].started_at
    nodes = [span for span in spans if span.span_type == "node"]
    assert nodes
    assert all(span.input_summary for span in nodes)
    assert all(span.input_summary for span in llms)
    for span in [*llms, *tools]:
        phases = [event["phase"] for event in (span.events or [])]
        assert phases[0] == "start"
        assert phases[-1] == "end"


@pytest.mark.asyncio
async def test_real_langgraph_records_model_and_sums_both_llm_usages(observability_enabled):
    tenant_id = f"tenant-usage-{uuid.uuid4().hex}"
    await _stream(GraphRuntime(build_tool_loop_graph()), tenant_id)
    spans = await _tenant_spans(tenant_id)
    llms = [span for span in spans if span.span_type == "llm"]
    assert [span.model for span in llms] == [MODEL_NAME, MODEL_NAME]
    assert [
        (
            _usage_value(span, "input", "prompt"),
            _usage_value(span, "output", "completion"),
            _usage_value(span, "total", "total"),
        )
        for span in llms
    ] == [
        (FIRST_USAGE["input"], FIRST_USAGE["output"], FIRST_USAGE["total"]),
        (SECOND_USAGE["input"], SECOND_USAGE["output"], SECOND_USAGE["total"]),
    ]
    root = _root(spans)
    assert _usage_value(root, "input", "prompt") == TRACE_USAGE["input"]
    assert _usage_value(root, "output", "completion") == TRACE_USAGE["output"]
    assert _usage_value(root, "total", "total") == TRACE_USAGE["total"]
    assert root.token_usage["prompt"] == TRACE_USAGE["input"]
    assert root.token_usage["completion"] == TRACE_USAGE["output"]


@pytest.mark.asyncio
async def test_non_streaming_real_graph_has_same_observability_shape(observability_enabled):
    tenant_id = f"tenant-invoke-{uuid.uuid4().hex}"
    result = await GraphRuntime(build_tool_loop_graph()).ainvoke(
        f"thread-{uuid.uuid4().hex}",
        "exercise the real graph",
        tenant_id,
        "user-v2",
        [],
        session_id=None,
        request_id="request-invoke-v2",
    )
    assert result["_observability"]["request_id"] == "request-invoke-v2"
    assert result["_observability"]["status"] == "ok"
    spans = await _tenant_spans(tenant_id)
    assert {span.span_type for span in spans} >= {"root", "node", "llm", "tool"}
    assert len([span for span in spans if span.span_type == "llm"]) == 2
    assert all(span.input_summary for span in spans if span.span_type in {"node", "llm"})


@pytest.mark.asyncio
async def test_sampling_zero_never_writes_orphan_children(observability_enabled, monkeypatch):
    monkeypatch.setattr(observability_enabled, "TRACE_SAMPLE_RATE", 0.0)
    tenant_id = f"tenant-unsampled-{uuid.uuid4().hex}"
    await _stream(GraphRuntime(build_tool_loop_graph()), tenant_id)
    assert await _tenant_spans(tenant_id) == []


@pytest.mark.asyncio
async def test_timeout_and_error_close_real_graph_spans(observability_enabled, monkeypatch):
    monkeypatch.setattr(observability_enabled, "AGENT_TIMEOUT_SECONDS", 0.01)
    timeout_tenant = f"tenant-timeout-{uuid.uuid4().hex}"
    await _stream(GraphRuntime(build_slow_graph(delay=0.05)), timeout_tenant)
    timeout_spans = await _tenant_spans(timeout_tenant)
    assert _root(timeout_spans).status == "timeout"
    assert any(span.status == "timeout" for span in timeout_spans if span.span_type != "root")

    monkeypatch.setattr(observability_enabled, "AGENT_TIMEOUT_SECONDS", 1.0)
    error_tenant = f"tenant-error-{uuid.uuid4().hex}"
    await _stream(GraphRuntime(build_error_graph()), error_tenant)
    error_spans = await _tenant_spans(error_tenant)
    assert _root(error_spans).status == "error"
    assert any(
        span.status == "error" and span.error for span in error_spans if span.span_type != "root"
    )


@pytest.mark.asyncio
async def test_non_streaming_idle_timeout_cancels_request_and_closes_spans(
    observability_enabled,
    monkeypatch,
):
    from app.harness import graph_runtime as graph_runtime_module

    app = _StartThenStallApp()
    request_id = f"request-invoke-timeout-{uuid.uuid4().hex}"
    tenant_id = f"tenant-invoke-timeout-{uuid.uuid4().hex}"
    cancelled_request_ids: list[str] = []
    monkeypatch.setattr(observability_enabled, "AGENT_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(
        graph_runtime_module,
        "cancel_request",
        lambda value: cancelled_request_ids.append(value) or 0,
    )

    with pytest.raises(LLMResponseTimeoutError) as error:
        await asyncio.wait_for(
            GraphRuntime(app).ainvoke(
                f"thread-{uuid.uuid4().hex}",
                "exercise timeout",
                tenant_id,
                "user-v2",
                [],
                request_id=request_id,
            ),
            timeout=0.2,
        )

    assert error.value.code == 50402
    assert error.value.reason == "model_response_timeout"
    assert cancelled_request_ids == [request_id]
    assert app.cancelled.is_set()
    assert app.state_reads == 0
    spans = await _tenant_spans(tenant_id)
    assert _root(spans).status == "timeout"
    assert any(span.status == "timeout" for span in spans if span.span_type != "root")


@pytest.mark.asyncio
async def test_resume_idle_timeout_cancels_request_and_closes_spans(
    observability_enabled,
    monkeypatch,
):
    from app.harness import graph_runtime as graph_runtime_module

    app = _StartThenStallApp()
    request_id = f"request-resume-timeout-{uuid.uuid4().hex}"
    tenant_id = f"tenant-resume-timeout-{uuid.uuid4().hex}"
    cancelled_request_ids: list[str] = []
    monkeypatch.setattr(observability_enabled, "AGENT_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(
        graph_runtime_module,
        "cancel_request",
        lambda value: cancelled_request_ids.append(value) or 0,
    )

    async def collect_frames() -> list[str]:
        return [
            frame
            async for frame in GraphRuntime(app).resume(
                f"thread-{uuid.uuid4().hex}",
                "approved",
                tenant_id=tenant_id,
                user_id="user-v2",
                request_id=request_id,
            )
        ]

    frames = await asyncio.wait_for(collect_frames(), timeout=0.2)

    assert cancelled_request_ids == [request_id]
    assert app.cancelled.is_set()
    error = _event_payload(frames, "error")
    assert error == {
        "code": 50402,
        "message": "模型响应超时，请稍后重试",
        "retry_after_ms": 5000,
    }
    assert _event_payload(frames, "run_end")["status"] == "timeout"
    spans = await _tenant_spans(tenant_id)
    assert _root(spans).status == "timeout"
    assert any(span.status == "timeout" for span in spans if span.span_type != "root")


@pytest.mark.asyncio
async def test_non_streaming_cancellation_cancels_request_and_closes_spans(
    observability_enabled,
    monkeypatch,
):
    from app.harness import graph_runtime as graph_runtime_module

    app = _StartThenStallApp()
    request_id = f"request-invoke-cancel-{uuid.uuid4().hex}"
    tenant_id = f"tenant-invoke-cancel-{uuid.uuid4().hex}"
    cancelled_request_ids: list[str] = []
    monkeypatch.setattr(observability_enabled, "AGENT_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(
        graph_runtime_module,
        "cancel_request",
        lambda value: cancelled_request_ids.append(value) or 0,
    )
    task = asyncio.create_task(
        GraphRuntime(app).ainvoke(
            f"thread-{uuid.uuid4().hex}",
            "exercise cancellation",
            tenant_id,
            "user-v2",
            [],
            request_id=request_id,
        )
    )
    await asyncio.wait_for(app.started.wait(), timeout=0.2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancelled_request_ids == [request_id]
    assert app.cancelled.is_set()
    spans = await _tenant_spans(tenant_id)
    assert _root(spans).status == "canceled"
    assert any(span.status == "canceled" for span in spans if span.span_type != "root")


@pytest.mark.asyncio
async def test_resume_cancellation_cancels_request_and_closes_spans(
    observability_enabled,
    monkeypatch,
):
    from app.harness import graph_runtime as graph_runtime_module

    app = _StartThenStallApp()
    request_id = f"request-resume-cancel-{uuid.uuid4().hex}"
    tenant_id = f"tenant-resume-cancel-{uuid.uuid4().hex}"
    cancelled_request_ids: list[str] = []
    monkeypatch.setattr(observability_enabled, "AGENT_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(
        graph_runtime_module,
        "cancel_request",
        lambda value: cancelled_request_ids.append(value) or 0,
    )
    stream = GraphRuntime(app).resume(
        f"thread-{uuid.uuid4().hex}",
        "approved",
        tenant_id=tenant_id,
        user_id="user-v2",
        request_id=request_id,
    )
    assert _event_payload([await anext(stream)], "trace_start") is not None
    assert _event_payload([await anext(stream)], "run_step_start") is not None
    next_frame = asyncio.create_task(anext(stream))
    await asyncio.wait_for(app.started.wait(), timeout=0.2)

    next_frame.cancel()
    with pytest.raises(asyncio.CancelledError):
        await next_frame

    assert cancelled_request_ids == [request_id]
    assert app.cancelled.is_set()
    spans = await _tenant_spans(tenant_id)
    assert _root(spans).status == "canceled"
    assert any(span.status == "canceled" for span in spans if span.span_type != "root")


@pytest.mark.asyncio
async def test_interrupt_and_resume_create_linked_traces(observability_enabled):
    tenant_id = f"tenant-resume-{uuid.uuid4().hex}"
    graph, checkpointer = build_interrupt_graph()
    runtime = GraphRuntime(graph, checkpointer)
    thread_id = f"thread-{uuid.uuid4().hex}"
    await _stream(runtime, tenant_id, thread_id=thread_id)
    initial_spans = await _tenant_spans(tenant_id)
    initial_root = _root(initial_spans)
    assert initial_root.status == "interrupted"

    frames = []
    async for frame in runtime.resume(
        thread_id,
        "approved",
        tenant_id=tenant_id,
        user_id="user-v2",
        session_id=None,
        request_id="request-resume-v2",
        link_trace_id=initial_root.trace_id,
    ):
        frames.append(frame)
    all_spans = await _tenant_spans(tenant_id)
    roots = [span for span in all_spans if span.span_type == "root"]
    resumed = next(span for span in roots if span.trace_id != initial_root.trace_id)
    assert resumed.status == "ok"
    resumed_nodes = [
        span for span in all_spans if span.trace_id == resumed.trace_id and span.span_type == "node"
    ]
    assert resumed_nodes
    assert all(span.input_summary for span in resumed_nodes)
    assert initial_root.trace_id in str((resumed.attributes or {}).get("links"))
    assert _event_payload(frames, "trace_start")["trace_id"] == resumed.trace_id
