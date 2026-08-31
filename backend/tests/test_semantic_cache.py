"""CONCURRENCY_GUARD_SPEC §8.4：语义缓存与风险门控。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.harness.semantic_cache import (
    SemanticCache,
    SemanticCacheScope,
    is_semantic_cache_answer_candidate,
    is_semantic_cache_candidate,
)


async def _embed(text: str) -> Sequence[float]:
    normalized = " ".join(text.casefold().split())
    if "rag" in normalized and "完全不同" not in normalized:
        return [0.999, 0.01] if "解释" in normalized else [1.0, 0.0]
    return [0.0, 1.0]


def test_similarity_threshold_cannot_be_configured_below_spec_floor():
    with pytest.raises(ValidationError):
        Settings(SEMANTIC_CACHE_SIMILARITY_THRESHOLD=0.5, _env_file=None)


@pytest.mark.asyncio
async def test_similar_pure_question_hits_and_marks_response_metadata():
    cache = SemanticCache(enabled=True, redis_url="", embedder=_embed)
    scope = SemanticCacheScope("tenant-a", "user-a", "agent-a", "model-a")

    assert await cache.store(
        "什么是 RAG？", "RAG 是检索增强生成。", scope=scope, metadata={"source": "llm"}
    )
    hit = await cache.lookup("请解释 RAG", scope=scope)

    assert hit is not None
    assert hit.answer == "RAG 是检索增强生成。"
    assert hit.similarity >= 0.95
    assert hit.response_metadata == {
        "source": "llm",
        "cache_hit": True,
        "semantic_cache_similarity": round(hit.similarity, 6),
    }


@pytest.mark.asyncio
async def test_threshold_ttl_and_tenant_user_isolation():
    now = [1000.0]
    cache = SemanticCache(
        enabled=True,
        redis_url="",
        embedder=_embed,
        ttl_seconds=10,
        clock=lambda: now[0],
    )
    owner = SemanticCacheScope("tenant-a", "user-a")
    other_user = SemanticCacheScope("tenant-a", "user-b")
    other_tenant = SemanticCacheScope("tenant-b", "user-a")
    assert await cache.store("什么是 RAG？", "answer", scope=owner)

    assert await cache.lookup("完全不同的问题", scope=owner) is None
    assert await cache.lookup("什么是 RAG？", scope=other_user) is None
    assert await cache.lookup("什么是 RAG？", scope=other_tenant) is None
    now[0] += 11
    assert await cache.lookup("什么是 RAG？", scope=owner) is None


@pytest.mark.asyncio
async def test_configuration_fingerprint_and_global_memory_cap_are_isolated():
    now = [1000.0]
    cache = SemanticCache(
        enabled=True,
        redis_url="",
        embedder=_embed,
        max_entries=2,
        clock=lambda: now[0],
    )
    first = SemanticCacheScope("tenant", "user-1", configuration_id="prompt-v1")
    second = SemanticCacheScope("tenant", "user-2", configuration_id="prompt-v1")
    third = SemanticCacheScope("tenant", "user-3", configuration_id="prompt-v1")
    changed_prompt = SemanticCacheScope(
        "tenant",
        "user-1",
        configuration_id="prompt-v2",
    )

    assert first.digest != changed_prompt.digest
    assert await cache.store("什么是 RAG？", "first", scope=first)
    now[0] += 1
    assert await cache.store("什么是 RAG？", "second", scope=second)
    now[0] += 1
    assert await cache.store("什么是 RAG？", "third", scope=third)

    assert sum(len(items) for items in cache._entries.values()) == 2
    assert await cache.lookup("什么是 RAG？", scope=first) is None
    assert await cache.lookup("什么是 RAG？", scope=changed_prompt) is None
    assert (await cache.lookup("什么是 RAG？", scope=second)).answer == "second"


@pytest.mark.asyncio
async def test_risk_gate_blocks_tool_attachment_dynamic_and_tool_results_without_embedding():
    calls: list[str] = []

    async def embed(text: str) -> Sequence[float]:
        calls.append(text)
        return [1.0, 0.0]

    cache = SemanticCache(enabled=True, redis_url="", embedder=embed)
    scope = SemanticCacheScope("tenant-a", "user-a")

    assert not is_semantic_cache_candidate("查询数据库里的订单")
    assert not is_semantic_cache_candidate("今天的天气怎么样？")
    assert not is_semantic_cache_candidate("What is my account balance?")
    assert not is_semantic_cache_candidate("What is order 12345?")
    assert not is_semantic_cache_candidate("什么是 RAG？", tool_intent=True)
    assert not is_semantic_cache_candidate("总结内容", has_attachments=True)
    assert await cache.lookup("今天的天气怎么样？", scope=scope) is None
    assert await cache.lookup("什么是 RAG？", scope=scope, tool_intent=True) is None
    assert await cache.lookup("什么是 RAG？", scope=scope, has_attachments=True) is None
    assert not await cache.store("什么是 RAG？", "tool answer", scope=scope, tool_used=True)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        "[Agent 执行出错] provider authentication failed",
        "（未收到模型回复，请检查 LLM_API_KEY 配置）",
        "执行未生成回复，请稍后重试",
    ],
)
async def test_synthetic_failure_answers_are_never_cached(answer):
    calls: list[str] = []

    async def embed(text: str) -> Sequence[float]:
        calls.append(text)
        return [1.0, 0.0]

    cache = SemanticCache(enabled=True, redis_url="", embedder=embed)
    scope = SemanticCacheScope("tenant-a", "user-a")

    assert not is_semantic_cache_answer_candidate(answer)
    assert not await cache.store("什么是 RAG？", answer, scope=scope)
    assert calls == []


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.set_expiry: dict[str, int] = {}

    async def ping(self) -> bool:
        return True

    async def set(self, key: str, value: str, *, ex: int) -> None:
        self.values[key] = value
        self.set_expiry[key] = ex

    async def zadd(self, key: str, values: dict[str, float]) -> None:
        self.zsets.setdefault(key, {}).update(values)

    async def expire(self, key: str, seconds: int) -> None:
        self.set_expiry[key] = seconds

    async def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    async def zrange(self, key: str, start: int, end: int) -> list[str]:
        items = [
            item[0] for item in sorted(self.zsets.get(key, {}).items(), key=lambda item: item[1])
        ]
        if start < 0:
            start = max(0, len(items) + start)
        if end < 0:
            end = len(items) + end
        return items[start : end + 1]

    async def zremrangebyscore(self, key: str, minimum: Any, maximum: float) -> None:
        _ = minimum
        bucket = self.zsets.get(key, {})
        for item in [item for item, score in bucket.items() if score <= maximum]:
            bucket.pop(item, None)

    async def mget(self, keys: list[str]) -> list[str | None]:
        return [self.values.get(key) for key in keys]

    async def zrem(self, key: str, *items: str) -> None:
        for item in items:
            self.zsets.get(key, {}).pop(item, None)

    async def zpopmin(self, key: str, count: int) -> list[tuple[str, float]]:
        bucket = self.zsets.get(key, {})
        items = sorted(bucket.items(), key=lambda item: item[1])[:count]
        for item, _ in items:
            bucket.pop(item, None)
        return items

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.values.pop(key, None)
            self.zsets.pop(key, None)

    async def aclose(self) -> None:
        return None


class _FailingRedis(_FakeRedis):
    async def ping(self) -> bool:
        raise ConnectionError("redis unavailable")


@pytest.mark.asyncio
async def test_redis_ttl_shares_entries_between_instances(monkeypatch):
    redis = _FakeRedis()
    writer = SemanticCache(
        enabled=True, redis_url="redis://cache", embedder=_embed, ttl_seconds=86_400
    )
    reader = SemanticCache(
        enabled=True, redis_url="redis://cache", embedder=_embed, ttl_seconds=86_400
    )

    async def get_redis():
        return redis

    monkeypatch.setattr(writer, "_redis_client", get_redis)
    monkeypatch.setattr(reader, "_redis_client", get_redis)
    scope = SemanticCacheScope("tenant-a", "user-a")
    assert await writer.store("什么是 RAG？", "shared answer", scope=scope)

    hit = await reader.lookup("请解释 RAG", scope=scope)
    assert hit is not None and hit.answer == "shared answer"
    entry_ttls = [ttl for key, ttl in redis.set_expiry.items() if "semantic_cache:entry:" in key]
    assert entry_ttls == [86_400]


@pytest.mark.asyncio
async def test_redis_hit_is_authoritative_over_stale_local_value(monkeypatch):
    redis = _FakeRedis()
    scope = SemanticCacheScope("tenant-a", "user-a")
    reader = SemanticCache(enabled=True, redis_url="", embedder=_embed)
    assert await reader.store("什么是 RAG？", "stale local", scope=scope)
    reader._redis_url = "redis://cache"

    writer = SemanticCache(enabled=True, redis_url="redis://cache", embedder=_embed)

    async def get_redis():
        return redis

    monkeypatch.setattr(reader, "_redis_client", get_redis)
    monkeypatch.setattr(writer, "_redis_client", get_redis)
    assert await writer.store("什么是 RAG？", "fresh shared", scope=scope)

    hit = await reader.lookup("请解释 RAG", scope=scope)
    assert hit is not None and hit.answer == "fresh shared"


@pytest.mark.asyncio
async def test_redis_connection_failure_fails_closed_as_cache_miss():
    cache = SemanticCache(
        enabled=True,
        redis_url="redis://127.0.0.1:6399/15",
        embedder=_embed,
    )
    scope = SemanticCacheScope("tenant-a", "user-a")

    assert not await cache.store("什么是 RAG？", "local answer", scope=scope)
    assert cache.backend_kind == "memory"
    hit = await cache.lookup("请解释 RAG", scope=scope)
    assert hit is None


@pytest.mark.asyncio
async def test_redis_reconnects_after_backoff_without_resurrecting_local_data(
    monkeypatch,
):
    import redis.asyncio as redis_asyncio

    shared = _FakeRedis()
    clients = [_FailingRedis(), shared]

    def from_url(*_args, **_kwargs):
        return clients.pop(0)

    monkeypatch.setattr(redis_asyncio, "from_url", from_url)
    cache = SemanticCache(
        enabled=True,
        redis_url="redis://cache",
        embedder=_embed,
    )
    scope = SemanticCacheScope("tenant-a", "user-a")

    assert not await cache.store("什么是 RAG？", "never-local", scope=scope)
    assert cache._entries == {}
    cache._redis_retry_at = 0.0
    assert await cache.store("什么是 RAG？", "shared", scope=scope)

    hit = await cache.lookup("请解释 RAG", scope=scope)
    assert hit is not None and hit.answer == "shared"
    assert cache.backend_kind == "redis"
