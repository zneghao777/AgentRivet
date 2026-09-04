"""通过 Run API 验证跨会话记忆身份、成功收尾及审批恢复。"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.api import router_chat
from app.config import settings
from app.harness import events
from app.harness.app_runtime import harness_app
from app.harness.context import get_tool_context
from app.harness.run_coordinator import RunCoordinator, run_coordinator
from app.memory import extractor, retriever
from app.memory import service as memory_module
from app.storage import chat_runs
from app.storage.db import AsyncSessionLocal
from app.storage.models import ChatRun, MemoryEntry
from app.tools.memory_search import memory_search


@pytest.fixture
def memory_run_env(monkeypatch):
    monkeypatch.setattr(settings, "CHAT_RUN_ENABLED", True)
    monkeypatch.setattr(settings, "CHAT_RUN_DURABLE_ENABLED", False)
    monkeypatch.setattr(settings, "MULTI_AGENT_ENABLED", False)
    monkeypatch.setattr(settings, "MQ_ENABLED", False)
    monkeypatch.setattr(settings, "MEMORY_ENABLED", True)
    monkeypatch.setattr(settings, "MEMORY_AUTO_INJECT", True)
    monkeypatch.setattr(settings, "MEMORY_SEARCH_TOOL", True)
    monkeypatch.setattr(run_coordinator, "start", AsyncMock())
    monkeypatch.setattr(run_coordinator, "enqueue", AsyncMock())
    vector = [1.0] + [0.0] * (settings.EMBEDDING_DIM - 1)
    monkeypatch.setattr(retriever, "embed_query", AsyncMock(return_value=vector))
    monkeypatch.setattr(memory_module, "embed_query", AsyncMock(return_value=vector))
    monkeypatch.setattr(memory_module, "embed_texts", AsyncMock(return_value=[vector]))
    extract = AsyncMock(return_value=[])
    monkeypatch.setattr(extractor, "extract", extract)
    headers = {"X-API-Key": "test-key-1", "X-Tenant-Id": f"mem-run-{uuid.uuid4().hex}"}
    return headers, extract


async def _create_run(client, headers, message):
    response = await client.post("/api/v1/sessions", headers=headers, json={})
    assert response.status_code == 200
    session_id = response.json()["data"]["session_id"]
    response = await client.post(
        f"/api/v1/sessions/{session_id}/runs",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"message": message},
    )
    assert response.status_code == 202
    return uuid.UUID(response.json()["data"]["run_id"])


async def _drain_extraction():
    if router_chat._extract_tasks:
        await asyncio.gather(*tuple(router_chat._extract_tasks))


async def _stored_run(run_id):
    async with AsyncSessionLocal() as db:
        return await db.get(ChatRun, run_id)


@pytest.mark.asyncio
async def test_run_recalls_existing_memory_with_session_identity(
    app_client, monkeypatch, memory_run_env
):
    headers, _ = memory_run_env
    tenant = headers["X-Tenant-Id"]
    async with AsyncSessionLocal() as db:
        for owner, content in [
            (settings.ANONYMOUS_USER_ID, "用户要求称呼他为豪哥"),
            ("another-user", "另一个用户的秘密"),
        ]:
            await memory_module.memory_service._insert(
                db,
                tenant_id=tenant,
                user_id=owner,
                content=content,
                memory_type="semantic",
                confidence=1.0,
                status="active",
                session_id=None,
                source_summary="旧会话",
            )
        await db.commit()
    captured = {}

    class Runtime:
        async def astream(self, _thread, _message, _tenant, user_id, _skills, **kwargs):
            captured["runtime_user"] = user_id
            captured["tool_user"] = get_tool_context().user_id
            captured["injected"] = kwargs["memory_context"]
            captured["tool_result"] = await memory_search.ainvoke({"query": "我的称呼"})
            yield events.message_delta("answer", "豪哥，你好")
            yield events.run_end(status="success", duration_ms=1, tool_calls=1)

    monkeypatch.setattr(harness_app, "runtime", Runtime())
    run_id = await _create_run(app_client, headers, "你该叫我什么？")
    await RunCoordinator().execute(run_id)
    await _drain_extraction()
    stored = await _stored_run(run_id)
    assert stored.status == "succeeded"
    # Run 保留 API Key 配额/鉴权主体；记忆使用会话用户。
    assert stored.user_id != settings.ANONYMOUS_USER_ID
    assert captured["runtime_user"] == settings.ANONYMOUS_USER_ID
    assert captured["tool_user"] == settings.ANONYMOUS_USER_ID
    for result in (captured["injected"], captured["tool_result"]):
        assert "豪哥" in result
        assert "另一个用户的秘密" not in result


@pytest.mark.asyncio
async def test_run_extracts_memory_visible_in_next_session_and_memory_api(
    app_client, monkeypatch, memory_run_env
):
    headers, extract = memory_run_env
    extract.return_value = [
        {
            "content": "用户要求称呼他为豪哥",
            "memory_type": "semantic",
            "confidence": 1.0,
        }
    ]
    injected = []

    class Runtime:
        async def astream(self, *_args, **kwargs):
            injected.append(kwargs["memory_context"])
            yield events.message_delta("answer", "好的，豪哥")
            yield events.run_end(status="success", duration_ms=1, tool_calls=0)

    monkeypatch.setattr(harness_app, "runtime", Runtime())
    coordinator = RunCoordinator()
    first = await _create_run(app_client, headers, "叫我豪哥")
    await coordinator.execute(first)
    await _drain_extraction()
    assert (await _stored_run(first)).status == "succeeded"
    extract.assert_awaited_once()
    assert extract.await_args.args[:2] == ("叫我豪哥", "好的，豪哥")
    response = await app_client.get("/api/v1/memory", headers=headers)
    assert response.status_code == 200
    assert response.json()["data"]["total"] == 1
    assert "豪哥" in response.json()["data"]["items"][0]["content"]
    async with AsyncSessionLocal() as db:
        entry = (
            await db.execute(
                select(MemoryEntry).where(MemoryEntry.tenant_id == headers["X-Tenant-Id"])
            )
        ).scalar_one()
    assert entry.user_id == settings.ANONYMOUS_USER_ID
    assert entry.source_session_id == (await _stored_run(first)).session_id
    await coordinator.execute(first)
    extract.assert_awaited_once()  # 重复领取已完成 Run 不重复提炼。
    extract.return_value = []
    second = await _create_run(app_client, headers, "你该叫我什么？")
    await coordinator.execute(second)
    await _drain_extraction()
    assert (await _stored_run(second)).status == "succeeded"
    assert injected[0] == ""
    assert "豪哥" in injected[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["failed", "canceled", "empty", "agent_off", "user_off", "extract_off"]
)
async def test_run_skips_extraction_when_not_successful_or_disabled(
    app_client, monkeypatch, memory_run_env, mode
):
    headers, extract = memory_run_env
    run_id = await _create_run(app_client, headers, "叫我豪哥")
    if mode == "agent_off":
        async with AsyncSessionLocal() as db:
            run = await db.get(ChatRun, run_id)
            run.request_payload = {
                **run.request_payload,
                "agent_snapshot": {"memory_enabled": False},
            }
            await db.commit()
    if mode in {"user_off", "extract_off"}:
        async with AsyncSessionLocal() as db:
            await memory_module.memory_service.update_settings(
                db,
                tenant_id=headers["X-Tenant-Id"],
                user_id=settings.ANONYMOUS_USER_ID,
                **({"long_term_enabled": False} if mode == "user_off" else {"auto_extract": False}),
            )

    class Runtime:
        async def astream(self, *_args, **_kwargs):
            if mode == "canceled":
                raise asyncio.CancelledError()
            if mode != "empty":
                yield events.message_delta("answer", "未确认的称呼")
            yield events.run_end(
                status="error" if mode == "failed" else "success",
                duration_ms=1,
                tool_calls=0,
            )

    monkeypatch.setattr(harness_app, "runtime", Runtime())
    await RunCoordinator().execute(run_id)
    await _drain_extraction()
    assert (await _stored_run(run_id)).status == {
        "failed": "failed",
        "canceled": "canceled",
        "empty": "failed",
    }.get(mode, "succeeded")
    extract.assert_not_awaited()


@pytest.mark.asyncio
async def test_resumed_run_extracts_original_request_only_after_success(
    app_client, monkeypatch, memory_run_env
):
    headers, extract = memory_run_env
    observed = {}

    class Runtime:
        async def astream(self, *_args, **_kwargs):
            yield events.interrupt_requested("等待审批", {"type": "tool_approval"})
            yield events.run_end(status="interrupted", duration_ms=1, tool_calls=0)

        async def resume(self, *_args, **kwargs):
            observed["user"] = kwargs["user_id"]
            observed["tool_user"] = get_tool_context().user_id
            yield events.message_delta("answer", "已处理，豪哥")
            yield events.run_end(status="success", duration_ms=1, tool_calls=1)

    monkeypatch.setattr(harness_app, "runtime", Runtime())
    run_id = await _create_run(app_client, headers, "叫我豪哥，帮我处理任务")
    coordinator = RunCoordinator()
    await coordinator.execute(run_id)
    await _drain_extraction()
    assert (await _stored_run(run_id)).status == "pending_approval"
    extract.assert_not_awaited()
    async with AsyncSessionLocal() as db:
        await chat_runs.queue_resume(
            db,
            run_id=run_id,
            command={"decision": "approve", "input": "批准"},
            write_outbox=False,
        )
    await coordinator.execute(run_id)
    await _drain_extraction()
    assert (await _stored_run(run_id)).status == "succeeded"
    assert observed == {"user": settings.ANONYMOUS_USER_ID, "tool_user": settings.ANONYMOUS_USER_ID}
    extract.assert_awaited_once()
    assert extract.await_args.args[:2] == ("叫我豪哥，帮我处理任务", "已处理，豪哥")
