"""OpenAI-compatible fault-injection server for concurrency guard load tests.

Run from ``backend`` with::

    MOCK_LLM_MODE=ok .venv/bin/uvicorn tests.load.mock_llm:app --port 18080

The mode can also be changed without restarting the server through ``/control``.
This server is test-only and never imports the production application.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

Mode = Literal["ok", "slow", "error", "rate_limit"]


@dataclass
class State:
    mode: Mode = "ok"
    delay_ms: int = 50
    inflight: int = 0
    peak_inflight: int = 0
    total: int = 0
    client_started: int = 0
    client_completed: int = 0
    client_outcomes: int = 0
    first_outcome_requests: int = 0
    queued_requests: int = 0
    queue_full_requests: int = 0
    rejected_requests: int = 0
    disconnect_events_ms: list[int] = field(default_factory=list)
    successful_requests: int = 0
    errors: int = 0
    rate_limits: int = 0
    upstream_max_concurrency: int = 20
    rate_limit_events_ms: list[int] = field(default_factory=list)
    inflight_samples: list[tuple[int, int]] = field(default_factory=list)
    request_parameters: list[dict[str, Any]] = field(default_factory=list)


state = State(
    mode=os.getenv("MOCK_LLM_MODE", "ok"),  # type: ignore[arg-type]
    delay_ms=int(os.getenv("MOCK_LLM_DELAY_MS", "50")),
)
_lock = asyncio.Lock()


class ControlRequest(BaseModel):
    mode: Mode
    delay_ms: int = Field(default=50, ge=0, le=600_000)
    upstream_max_concurrency: int = Field(default=20, ge=1, le=10_000)
    reset_stats: bool = False


class ClientOutcomeRequest(BaseModel):
    success: bool
    first_outcome: bool = False
    queued: bool = False
    queue_full: bool = False
    rejected: bool = False


async def _begin_call(body: dict) -> int:
    async with _lock:
        state.inflight += 1
        state.total += 1
        state.peak_inflight = max(state.peak_inflight, state.inflight)
        now_ms = int(time.time() * 1000)
        state.inflight_samples.append((now_ms, state.inflight))
        state.request_parameters.append(
            {
                "at_ms": now_ms,
                "max_tokens": body.get("max_tokens"),
                "thinking_mode": (body.get("extra_body") or {}).get("thinking_mode")
                or body.get("thinking_mode"),
                "stream": bool(body.get("stream")),
            }
        )
        # Keep enough history for the formal 60s/120s degrade hold windows.
        # The 150-VU scenario can produce more than 20,000 begin/end samples.
        state.inflight_samples = state.inflight_samples[-100_000:]
        state.request_parameters = state.request_parameters[-100_000:]
        return state.inflight


async def _end_call() -> None:
    async with _lock:
        state.inflight = max(0, state.inflight - 1)
        state.inflight_samples.append((int(time.time() * 1000), state.inflight))
        state.inflight_samples = state.inflight_samples[-100_000:]


app = FastAPI(title="concurrency-guard-mock-llm")


@app.post("/control")
async def control(body: ControlRequest) -> dict:
    async with _lock:
        state.mode = body.mode
        state.delay_ms = body.delay_ms
        state.upstream_max_concurrency = body.upstream_max_concurrency
        if body.reset_stats:
            state.inflight = 0
            state.peak_inflight = 0
            state.total = 0
            state.client_started = 0
            state.client_completed = 0
            state.client_outcomes = 0
            state.first_outcome_requests = 0
            state.queued_requests = 0
            state.queue_full_requests = 0
            state.rejected_requests = 0
            state.disconnect_events_ms.clear()
            state.successful_requests = 0
            state.errors = 0
            state.rate_limits = 0
            state.rate_limit_events_ms.clear()
            state.inflight_samples.clear()
            state.request_parameters.clear()
        result = snapshot()
    return result


@app.post("/client-start")
async def client_start() -> dict:
    """Record a k6 iteration before it starts any application HTTP request."""
    async with _lock:
        state.client_started += 1
        started = state.client_started
    return {"client_started": started}


@app.post("/client-outcome")
async def client_outcome(body: ClientOutcomeRequest) -> dict:
    """Record the completed SSE outcome so teardown can reconcile end-to-end coverage."""
    async with _lock:
        state.client_completed += 1
        state.client_outcomes += 1
        if body.first_outcome:
            state.first_outcome_requests += 1
        if body.success:
            state.successful_requests += 1
        if body.queued:
            state.queued_requests += 1
        if body.queue_full:
            state.queue_full_requests += 1
        if body.rejected:
            state.rejected_requests += 1
    return {
        "client_started": state.client_started,
        "client_completed": state.client_completed,
        "client_outcomes": state.client_outcomes,
        "first_outcome_requests": state.first_outcome_requests,
        "successful_requests": state.successful_requests,
        "queued_requests": state.queued_requests,
        "queue_full_requests": state.queue_full_requests,
        "rejected_requests": state.rejected_requests,
    }


@app.post("/client-disconnect")
async def client_disconnect() -> dict:
    """Record when k6 observes its intentional five-second stream abort."""
    async with _lock:
        state.disconnect_events_ms.append(int(time.time() * 1000))
        state.disconnect_events_ms = state.disconnect_events_ms[-10_000:]
        events = len(state.disconnect_events_ms)
    return {"client_disconnects": events}


@app.get("/stats")
async def stats() -> dict:
    return snapshot()


def snapshot() -> dict:
    return {
        "mode": state.mode,
        "delay_ms": state.delay_ms,
        "inflight": state.inflight,
        "peak_inflight": state.peak_inflight,
        "total": state.total,
        "client_started": state.client_started,
        "client_completed": state.client_completed,
        "client_outcomes": state.client_outcomes,
        "first_outcome_requests": state.first_outcome_requests,
        "queued_requests": state.queued_requests,
        "queue_full_requests": state.queue_full_requests,
        "rejected_requests": state.rejected_requests,
        "disconnect_events_ms": state.disconnect_events_ms,
        "successful_requests": state.successful_requests,
        "errors": state.errors,
        "rate_limits": state.rate_limits,
        "upstream_max_concurrency": state.upstream_max_concurrency,
        "rate_limit_events_ms": state.rate_limit_events_ms,
        "inflight_samples": state.inflight_samples,
        "request_parameters": state.request_parameters,
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    inflight = await _begin_call(body)
    streaming_response_started = False
    try:
        if state.mode == "error":
            state.errors += 1
            return JSONResponse(
                status_code=500,
                content={"error": {"message": "injected upstream failure"}},
            )
        if state.mode == "rate_limit" and inflight > state.upstream_max_concurrency:
            state.rate_limits += 1
            state.rate_limit_events_ms.append(int(time.time() * 1000))
            state.rate_limit_events_ms = state.rate_limit_events_ms[-20_000:]
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": "1"},
                content={"error": {"message": "injected upstream rate limit"}},
            )

        delay_ms = state.delay_ms if state.mode == "slow" else min(state.delay_ms, 50)
        if state.mode == "rate_limit":
            delay_ms = state.delay_ms
        if body.get("stream"):
            streaming_response_started = True
            return StreamingResponse(
                _tracked_stream_response(body, delay_ms),
                media_type="text/event-stream",
            )
        await asyncio.sleep(delay_ms / 1000)
        return _completion_response(body)
    finally:
        # Streaming responses keep ownership until their generator finishes.
        if not streaming_response_started:
            await _end_call()


async def _tracked_stream_response(body: dict, delay_ms: int) -> AsyncIterator[str]:
    try:
        async for chunk in _stream_response(body, delay_ms):
            yield chunk
    finally:
        await _end_call()


async def _stream_response(body: dict, delay_ms: int) -> AsyncIterator[str]:
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    model = str(body.get("model") or "mock-model")
    await asyncio.sleep(delay_ms / 1000)
    for index, text in enumerate(("并发", "防护", "测试", "完成")):
        payload = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant"} if index == 0 else {"content": text},
                    "finish_reason": None,
                }
            ],
        }
        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        await asyncio.sleep(0.01)
    final_payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
    }
    yield f"data: {json.dumps(final_payload, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


def _completion_response(body: dict) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": str(body.get("model") or "mock-model"),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "并发防护测试完成"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
    }
