"""Semantic-cache hooks on both complete and SSE chat paths."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from app.agents.models import ResolvedAgentCapabilities
from app.api import router_chat
from app.api.deps import AuthContext
from app.api.schemas import MessageStreamRequest
from app.config import settings
from app.harness.app_runtime import harness_app
from app.harness.semantic_cache import SemanticCacheHit, SemanticCacheScope
from app.harness.stream_gate import StreamGate, UserStreamLock
from app.security.tenant import TenantContext

AUTH = {"X-API-Key": "test-key-1"}


async def _new_session(app_client) -> str:
    response = await app_client.post("/api/v1/sessions", headers=AUTH, json={})
    assert response.status_code == 200
    return response.json()["data"]["session_id"]


@pytest.mark.parametrize(
    ("message", "must_bypass"),
    [
        ("什么是向量数据库？", False),
        ("What is a vector database?", False),
        ("What is the current time?", True),
        ("Search the knowledge base for RAG", True),
        ("What is my account balance?", True),
        ("What is order 12345?", True),
        ("请查询我的记忆", True),
        ("请调用 skill 完成任务", True),
        ("讲个笑话", True),
    ],
)
def test_stream_cache_tool_intent_uses_stable_question_allowlist(
    message,
    must_bypass,
):
    body = MessageStreamRequest(message=message)

    result = router_chat._semantic_cache_tool_intent(
        body,
        resolved_agent=ResolvedAgentCapabilities(),
        skill_names=[],
        dynamic_tools=[],
        orchestration_agents={},
        memory_context="",
    )

    assert result is must_bypass


def test_stream_cache_bypasses_when_dynamic_tools_are_available():
    body = MessageStreamRequest(message="什么是向量数据库？")

    result = router_chat._semantic_cache_tool_intent(
        body,
        resolved_agent=ResolvedAgentCapabilities(),
        skill_names=[],
        dynamic_tools=[object()],
        orchestration_agents={},
        memory_context="",
    )

    assert result is True


@pytest.mark.parametrize(
    "resolved_agent",
    [
        ResolvedAgentCapabilities(allowed_tool_names={"lookup"}),
        ResolvedAgentCapabilities(allowed_kb_ids=[object()]),
    ],
)
def test_stream_cache_bypasses_explicit_static_capabilities(
    resolved_agent,
):
    result = router_chat._semantic_cache_tool_intent(
        MessageStreamRequest(message="什么是向量数据库？"),
        resolved_agent=resolved_agent,
        skill_names=[],
        dynamic_tools=[],
        orchestration_agents={},
        memory_context="",
    )

    assert result is True


def test_stream_cache_allows_stable_question_with_enabled_prompt_skill():
    """The main UI sends enabled skills on every request; prompt-only skills stay cacheable.

    Skill identity and body are part of the cache configuration fingerprint, while an
    actual tool call still prevents the generated answer from being stored.
    """

    result = router_chat._semantic_cache_tool_intent(
        MessageStreamRequest(message="什么是向量数据库？"),
        resolved_agent=ResolvedAgentCapabilities(allowed_kb_ids=[]),
        skill_names=["skill-hello"],
        dynamic_tools=[],
        orchestration_agents={},
        memory_context="",
    )

    assert result is False


def test_stream_cache_bypasses_default_active_knowledge_bases():
    result = router_chat._semantic_cache_tool_intent(
        MessageStreamRequest(message="什么是向量数据库？"),
        resolved_agent=ResolvedAgentCapabilities(),
        skill_names=[],
        dynamic_tools=[],
        orchestration_agents={},
        memory_context="",
        allowed_kb_ids=[object()],
    )

    assert result is True


def test_stream_cache_bypasses_follow_up_question_even_with_stable_marker():
    body = MessageStreamRequest(message="Why is that?")

    result = router_chat._semantic_cache_tool_intent(
        body,
        resolved_agent=ResolvedAgentCapabilities(),
        skill_names=[],
        dynamic_tools=[],
        orchestration_agents={},
        memory_context="",
        has_session_history=True,
    )

    assert result is True


def test_semantic_cache_scope_changes_with_embedding_identity(monkeypatch):
    request = SimpleNamespace(state=SimpleNamespace(api_key_id="key-identity"))
    auth = AuthContext(
        tenant=TenantContext(tenant_id="tenant-a", api_key="test-key-1"),
        user_id="user-a",
    )
    body = MessageStreamRequest(message="什么是向量数据库？")
    kwargs = {
        "resolved_agent": ResolvedAgentCapabilities(),
        "model_id": "chat-model",
        "model_base_url": "http://chat.test/v1",
        "model_api_key": "provider-key-a",
        "body": body,
        "skill_names": [],
    }

    before = router_chat._semantic_cache_scope(request, auth, **kwargs)
    other_credential = router_chat._semantic_cache_scope(
        request,
        auth,
        **{**kwargs, "model_api_key": "provider-key-b"},
    )
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "replacement-embedding-model")
    after = router_chat._semantic_cache_scope(request, auth, **kwargs)

    assert before.configuration_id != other_credential.configuration_id
    assert before.configuration_id != after.configuration_id


@pytest.fixture
def no_runtime_context(monkeypatch):
    async def no_memory(*_args, **_kwargs) -> str:
        return ""

    async def no_knowledge_bases(*_args, **_kwargs) -> list:
        return []

    monkeypatch.setattr(router_chat, "_retrieve_memory_context", no_memory)
    monkeypatch.setattr(
        router_chat.knowledge_service,
        "resolve_ids_by_names",
        no_knowledge_bases,
    )
    monkeypatch.setattr(router_chat, "_schedule_memory_extract", lambda **_kwargs: None)
    monkeypatch.setattr(settings, "GUARD_ENABLED", True)
    monkeypatch.setattr(settings, "MULTI_AGENT_ENABLED", False)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLED", True)


@pytest.mark.asyncio
async def test_non_stream_cache_hit_bypasses_runtime_and_marks_response(
    app_client,
    monkeypatch,
    no_runtime_context,
):
    session_id = await _new_session(app_client)

    class FakeCache:
        async def lookup(self, *_args, **_kwargs):
            return SemanticCacheHit("缓存回答", 0.97, {"source": "semantic_cache"})

        async def store(self, *_args, **_kwargs):  # pragma: no cover - hit never stores
            raise AssertionError("cache hit must not be stored again")

    async def runtime_must_not_run(*_args, **_kwargs):
        raise AssertionError("LLM runtime must be bypassed on cache hit")

    monkeypatch.setattr(router_chat, "semantic_cache", FakeCache())
    monkeypatch.setattr(harness_app.runtime, "ainvoke", runtime_must_not_run)

    response = await app_client.post(
        f"/api/v1/sessions/{session_id}/messages",
        headers=AUTH,
        json={"message": "什么是 RAG？"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["reply"] == "缓存回答"
    assert response.json()["data"]["cache_hit"] is True
    history = await app_client.get(
        f"/api/v1/sessions/{session_id}/messages",
        headers=AUTH,
    )
    assert [item["role"] for item in history.json()["data"]] == ["user", "assistant"]
    assert history.json()["data"][-1]["content"] == "缓存回答"


@pytest.mark.asyncio
async def test_non_stream_empty_result_returns_error_and_never_caches_demo_reply(
    app_client,
    monkeypatch,
    no_runtime_context,
):
    session_id = await _new_session(app_client)
    stored: list[tuple] = []

    class FakeCache:
        async def lookup(self, *_args, **_kwargs):
            return None

        async def store(self, *args, **kwargs):
            stored.append((args, kwargs))
            return True

    async def empty_result(*_args, **_kwargs):
        return {
            "_observability": {
                "status": "error",
                "error_code": 50002,
                "error": "graph produced no assistant response",
            }
        }

    monkeypatch.setattr(router_chat, "semantic_cache", FakeCache())
    monkeypatch.setattr(harness_app.runtime, "ainvoke", empty_result)

    response = await app_client.post(
        f"/api/v1/sessions/{session_id}/messages",
        headers=AUTH,
        json={"message": "什么是向量数据库？"},
    )
    await asyncio.sleep(0)

    assert response.status_code == 500
    assert response.json() == {
        "detail": {
            "code": 50002,
            "message": "执行未生成回复，请稍后重试",
            "retry_after_ms": 1_000,
        }
    }
    assert stored == []
    history = await app_client.get(
        f"/api/v1/sessions/{session_id}/messages",
        headers=AUTH,
    )
    assert [item["role"] for item in history.json()["data"]] == ["user"]


@pytest.mark.asyncio
async def test_stream_cache_hit_bypasses_gate_and_runtime(
    app_client,
    monkeypatch,
    no_runtime_context,
):
    session_id = await _new_session(app_client)
    gate = StreamGate(max_streams=1, queue_capacity=0)
    lock = UserStreamLock(ttl_seconds=180)

    class FakeCache:
        async def lookup(self, *_args, **_kwargs):
            return SemanticCacheHit("流式缓存回答", 0.98)

        async def store(self, *_args, **_kwargs):  # pragma: no cover - hit never stores
            raise AssertionError("cache hit must not be stored again")

    async def runtime_must_not_run(*_args, **_kwargs):
        raise AssertionError("LLM runtime must be bypassed on cache hit")
        yield ""  # pragma: no cover - make this an async generator

    monkeypatch.setattr(router_chat, "stream_gate", gate)
    monkeypatch.setattr(router_chat, "user_stream_lock", lock)
    monkeypatch.setattr(router_chat, "semantic_cache", FakeCache())
    monkeypatch.setattr(harness_app.runtime, "astream", runtime_must_not_run)

    response = await app_client.post(
        f"/api/v1/sessions/{session_id}/messages/stream",
        headers=AUTH,
        json={"message": "请解释 RAG"},
    )

    assert response.status_code == 200
    assert "event: queue" not in response.text
    assert '"content":"流式缓存回答"' in response.text
    assert response.text.count('"cache_hit":true') >= 4
    assert gate.inflight == 0
    history = await app_client.get(
        f"/api/v1/sessions/{session_id}/messages",
        headers=AUTH,
    )
    assert [item["role"] for item in history.json()["data"]] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_stream_cache_is_independent_from_guard_and_heartbeats_while_lookup_waits(
    app_client,
    monkeypatch,
    no_runtime_context,
):
    session_id = await _new_session(app_client)
    monkeypatch.setattr(settings, "GUARD_ENABLED", False)
    monkeypatch.setattr(router_chat, "SSE_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    looked_up = asyncio.Event()

    class SlowHitCache:
        async def lookup(self, *_args, **_kwargs):
            looked_up.set()
            await asyncio.sleep(0.035)
            return SemanticCacheHit("独立缓存回答", 0.99)

        async def store(self, *_args, **_kwargs):  # pragma: no cover
            raise AssertionError("cache hit must not be stored again")

    async def runtime_must_not_run(*_args, **_kwargs):
        raise AssertionError("LLM runtime must be bypassed on cache hit")
        yield ""  # pragma: no cover

    monkeypatch.setattr(router_chat, "semantic_cache", SlowHitCache())
    monkeypatch.setattr(harness_app.runtime, "astream", runtime_must_not_run)

    response = await app_client.post(
        f"/api/v1/sessions/{session_id}/messages/stream",
        headers=AUTH,
        json={"message": "请解释向量数据库"},
    )

    assert looked_up.is_set()
    assert response.text.count(": keep-alive\n\n") >= 2
    assert '"content":"独立缓存回答"' in response.text


@pytest.mark.asyncio
async def test_disconnect_during_cache_lookup_releases_lock_without_messages(
    app_client,
    monkeypatch,
    no_runtime_context,
):
    session_id = await _new_session(app_client)
    gate = StreamGate(max_streams=1, queue_capacity=1)
    lock = UserStreamLock(ttl_seconds=180)
    started = asyncio.Event()
    stopped = asyncio.Event()
    never = asyncio.Event()

    class BlockingCache:
        async def lookup(self, *_args, **_kwargs):
            started.set()
            try:
                await never.wait()
            finally:
                stopped.set()

        async def store(self, *_args, **_kwargs):  # pragma: no cover
            return False

    monkeypatch.setattr(router_chat, "stream_gate", gate)
    monkeypatch.setattr(router_chat, "user_stream_lock", lock)
    monkeypatch.setattr(router_chat, "semantic_cache", BlockingCache())
    request_task = asyncio.create_task(
        app_client.post(
            f"/api/v1/sessions/{session_id}/messages/stream",
            headers=AUTH,
            json={"message": "请解释向量数据库"},
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    request_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request_task
    await asyncio.wait_for(stopped.wait(), timeout=1)

    principal = f"api-key:{router_chat.hash_api_key(AUTH['X-API-Key'])}"
    assert await lock.acquire("default", principal, session_id, "next-request")
    await lock.release("default", principal, session_id, "next-request")
    history = await app_client.get(
        f"/api/v1/sessions/{session_id}/messages",
        headers=AUTH,
    )
    assert history.json()["data"] == []


@pytest.mark.asyncio
async def test_cache_miss_stores_only_successful_tool_free_answer(
    app_client,
    monkeypatch,
    no_runtime_context,
):
    session_id = await _new_session(app_client)
    stored = asyncio.Event()
    calls: list[dict] = []

    class FakeCache:
        async def lookup(self, *_args, **_kwargs):
            return None

        async def store(self, *_args, **kwargs):
            calls.append(kwargs)
            stored.set()
            return True

    async def fake_ainvoke(*args, **_kwargs):
        has_tool = len(args) > 1 and "工具" in str(args[1])
        return {
            "messages": [
                AIMessage(
                    content="新回答",
                    tool_calls=(
                        [
                            {
                                "id": "call-1",
                                "name": "get_time",
                                "args": {},
                                "type": "tool_call",
                            }
                        ]
                        if has_tool
                        else []
                    ),
                )
            ],
            "_observability": {"status": "ok", "token_usage": {}},
        }

    monkeypatch.setattr(router_chat, "semantic_cache", FakeCache())
    monkeypatch.setattr(harness_app.runtime, "ainvoke", fake_ainvoke)

    response = await app_client.post(
        f"/api/v1/sessions/{session_id}/messages",
        headers=AUTH,
        json={"message": "什么是向量数据库？"},
    )
    await asyncio.wait_for(stored.wait(), timeout=1)

    assert response.status_code == 200
    assert response.json()["data"]["cache_hit"] is False
    assert calls and calls[0]["tool_used"] is False
    assert calls[0]["tool_intent"] is False

    second_session = await _new_session(app_client)
    stored.clear()
    response = await app_client.post(
        f"/api/v1/sessions/{second_session}/messages",
        headers=AUTH,
        json={"message": "调用工具回答"},
    )
    await asyncio.wait_for(stored.wait(), timeout=1)

    assert response.status_code == 200
    assert calls[-1]["tool_used"] is True


@pytest.mark.asyncio
async def test_semantic_cache_shutdown_cancels_pending_writes_and_rejects_new_ones(
    monkeypatch,
):
    started = asyncio.Event()
    cancelled = asyncio.Event()
    never = asyncio.Event()

    class BlockingCache:
        async def store(self, *_args, **_kwargs):
            started.set()
            try:
                await never.wait()
            finally:
                cancelled.set()

    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLED", True)
    monkeypatch.setattr(router_chat, "semantic_cache", BlockingCache())
    router_chat.start_semantic_cache_tasks()
    router_chat._schedule_semantic_cache_store(
        "什么是 RAG？",
        "回答",
        scope=SemanticCacheScope("tenant", "user"),
        tool_intent=False,
        tool_used=False,
        metadata={},
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    await router_chat.shutdown_semantic_cache_tasks(timeout_seconds=0.01)
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    assert not router_chat._semantic_cache_tasks

    router_chat._schedule_semantic_cache_store(
        "什么是 RAG？",
        "不会调度",
        scope=SemanticCacheScope("tenant", "user"),
        tool_intent=False,
        tool_used=False,
        metadata={},
    )
    assert not router_chat._semantic_cache_tasks
    router_chat.start_semantic_cache_tasks()


@pytest.mark.parametrize(
    "answer",
    [
        "[Agent 执行出错] temporary provider failure",
        "（未收到模型回复，请检查 LLM_API_KEY 配置）",
        "执行未生成回复，请稍后重试",
    ],
)
def test_semantic_cache_scheduler_rejects_synthetic_failure_answers(
    monkeypatch,
    answer,
):
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLED", True)
    router_chat.start_semantic_cache_tasks()
    tasks_before = set(router_chat._semantic_cache_tasks)

    router_chat._schedule_semantic_cache_store(
        "什么是 RAG？",
        answer,
        scope=SemanticCacheScope("tenant", "user"),
        tool_intent=False,
        tool_used=False,
        metadata={},
    )

    assert router_chat._semantic_cache_tasks == tasks_before
