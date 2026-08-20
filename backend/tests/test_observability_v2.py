"""M0 red tests for the frozen observability v2 collection contract."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from sqlalchemy import select

from app.harness.graph_runtime import GraphRuntime
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
            ).scalars().all()
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


def _root(spans: list[AgentSpan]) -> AgentSpan:
    return next(span for span in spans if span.span_type == "root")


def _usage_value(span: AgentSpan, canonical: str, legacy: str) -> int | None:
    usage = span.token_usage or {}
    return usage.get(canonical, usage.get(legacy))


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
    assert payload["trace_id"] == _root(spans).trace_id


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
        span.status == "error" and span.error
        for span in error_spans
        if span.span_type != "root"
    )


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
    roots = [span for span in await _tenant_spans(tenant_id) if span.span_type == "root"]
    resumed = next(span for span in roots if span.trace_id != initial_root.trace_id)
    assert resumed.status == "ok"
    assert initial_root.trace_id in str((resumed.attributes or {}).get("links"))
    assert _event_payload(frames, "trace_start")["trace_id"] == resumed.trace_id
