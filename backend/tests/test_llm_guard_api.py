"""Public JSON error contract for non-streaming guarded LLM failures."""

from __future__ import annotations

import asyncio

import pytest

from app.api import router_chat
from app.config import settings
from app.harness.app_runtime import harness_app
from app.harness.llm_guard import (
    LLMBusyError,
    LLMGuardOpenError,
    LLMOutcomeTimeoutError,
    LLMResponseTimeoutError,
)
from app.observability.metrics import metrics

AUTH = {"X-API-Key": "test-key-1"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status_code", "code", "message", "retry_after_ms", "reason"),
    [
        (
            LLMBusyError(),
            504,
            50400,
            "当前使用人数较多，请稍后重试",
            5_000,
            "llm_slot_timeout",
        ),
        (
            LLMOutcomeTimeoutError(),
            504,
            50401,
            "请求未能在规定时间内产生可见反馈，请稍后重试",
            5_000,
            "first_feedback_timeout",
        ),
        (
            LLMResponseTimeoutError(),
            504,
            50402,
            "模型响应超时，请稍后重试",
            5_000,
            "model_response_timeout",
        ),
        (
            LLMGuardOpenError(retry_after_ms=30_000),
            503,
            50300,
            "模型服务暂时不可用，请稍后重试",
            30_000,
            "cb_open",
        ),
    ],
)
async def test_non_stream_guard_errors_use_stable_json_contract(
    app_client,
    monkeypatch,
    error,
    status_code,
    code,
    message,
    retry_after_ms,
    reason,
):
    async def no_memory(*_args, **_kwargs) -> str:
        return ""

    async def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(router_chat, "_retrieve_memory_context", no_memory)
    monkeypatch.setattr(harness_app.runtime, "ainvoke", fail)
    rejected_before = metrics.guard_rejected_total.get(reason, 0)
    created = await app_client.post("/api/v1/sessions", headers=AUTH, json={})
    session_id = created.json()["data"]["session_id"]

    response = await app_client.post(
        f"/api/v1/sessions/{session_id}/messages",
        headers=AUTH,
        json={"message": "容量保护测试"},
    )

    assert response.status_code == status_code
    assert response.json() == {
        "code": code,
        "message": message,
        "retry_after_ms": retry_after_ms,
    }
    assert int(response.headers["retry-after"]) == retry_after_ms // 1000
    assert metrics.guard_rejected_total.get(reason, 0) == rejected_before + 1
    history = await app_client.get(
        f"/api/v1/sessions/{session_id}/messages",
        headers=AUTH,
    )
    assert [item["role"] for item in history.json()["data"]] == ["user"]


@pytest.mark.asyncio
async def test_non_stream_preflight_uses_absolute_deadline_and_records_once(
    app_client,
    monkeypatch,
):
    created = await app_client.post("/api/v1/sessions", headers=AUTH, json={})
    session_id = created.json()["data"]["session_id"]
    cancelled = asyncio.Event()

    async def stalled_get_session(*_args, **_kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(settings, "GUARD_OUTCOME_DEADLINE_SECONDS", 0.01)
    monkeypatch.setattr(router_chat.repo, "get_session", stalled_get_session)
    rejected_before = metrics.guard_rejected_total.get("first_feedback_timeout", 0)
    slot_before = metrics.guard_rejected_total.get("llm_slot_timeout", 0)

    response = await app_client.post(
        f"/api/v1/sessions/{session_id}/messages",
        headers=AUTH,
        json={"message": "前置阶段也必须受首结果截止约束"},
    )

    assert response.status_code == 504
    assert response.json() == {
        "code": 50401,
        "message": "请求未能在规定时间内产生可见反馈，请稍后重试",
        "retry_after_ms": 5_000,
    }
    assert cancelled.is_set()
    assert (
        metrics.guard_rejected_total.get("first_feedback_timeout", 0)
        == rejected_before + 1
    )
    assert metrics.guard_rejected_total.get("llm_slot_timeout", 0) == slot_before
