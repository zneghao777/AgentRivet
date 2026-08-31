"""保守语义缓存：复用 BGE-M3，以 Redis TTL 存储作为共享权威源。

缓存按 tenant、user、Agent、模型四个维度隔离。调用方必须在回答完成后传入
``tool_used=True`` 阻止工具结果写入；查询侧还会用保守规则拦截附件、工具动作、
时效信息和依赖对话上下文的问题。未配置 Redis 时使用有界进程内缓存；已配置
Redis 但暂时不可用时按 miss 处理，避免多副本间复活过期或已清理答案。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from ..observability.metrics import metrics

logger = logging.getLogger(__name__)

DEFAULT_SIMILARITY_THRESHOLD = 0.95
DEFAULT_TTL_SECONDS = 24 * 60 * 60
DEFAULT_MAX_ENTRIES = 1000

_SPACE_RE = re.compile(r"\s+")
_TOOL_ACTION_RE = re.compile(
    r"(?:搜索|联网|查找|检索|查询(?:数据库|订单|库存|余额|日志)|"
    r"打开|发送|创建|新建|删除|修改|上传|下载|运行|执行|调用|"
    r"订票|预订|下单|提醒|发邮件|导出|"
    r"\b(?:search|browse|lookup|find|open|send|create|delete|update|"
    r"upload|download|run|book|order|remind|export)\b)"
)
_DYNAMIC_RE = re.compile(
    r"(?:今天|明天|昨天|当前|现在|实时|最新|刚刚|本周|本月|"
    r"天气|新闻|热搜|股价|汇率|币价|航班|余票|库存|比分|"
    r"\b(?:today|tomorrow|yesterday|current|now|latest|live|weather|"
    r"news|price|stock|exchange rate|flight|inventory|score|account|"
    r"balance|invoice|subscription|profile|status)\b|\d)"
)
_CONTEXT_RE = re.compile(
    r"(?:刚才|上文|上面|前面|上一条|继续|接着|这个文件|该文件|"
    r"附件|这张图|这份|本会话|我的|我们之前|"
    r"\b(?:my|this|that|it|those|previous|above|continue|again|"
    r"attachment|file|our earlier)\b)"
)
_SYNTHETIC_FAILURE_PREFIXES = (
    "[Agent 执行出错]",
    "（未收到模型回复",
    "执行未生成回复",
    "恢复执行未生成回复",
)


@dataclass(frozen=True)
class SemanticCacheScope:
    """缓存隔离范围，tenant 与 user 均为必填。"""

    tenant_id: str
    user_id: str
    agent_id: str = "default"
    model_id: str = "default"
    configuration_id: str = "default"

    def __post_init__(self) -> None:
        if not self.tenant_id.strip() or not self.user_id.strip():
            raise ValueError("semantic cache requires non-empty tenant_id and user_id")

    @property
    def digest(self) -> str:
        raw = "\x1f".join(
            (
                self.tenant_id,
                self.user_id,
                self.agent_id,
                self.model_id,
                self.configuration_id,
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SemanticCacheHit:
    """缓存命中结果；metadata 始终包含 cache_hit=true。"""

    answer: str
    similarity: float
    metadata: dict[str, Any] = field(default_factory=dict)
    cache_hit: bool = True

    @property
    def response_metadata(self) -> dict[str, Any]:
        return {
            **self.metadata,
            "cache_hit": True,
            "semantic_cache_similarity": round(self.similarity, 6),
        }


@dataclass
class _Entry:
    entry_id: str
    vector: tuple[float, ...]
    answer: str
    metadata: dict[str, Any]
    created_at: float
    expires_at: float


Embedder = Callable[[str], Awaitable[Sequence[float]] | Sequence[float]]


def is_semantic_cache_candidate(
    question: str,
    *,
    tool_intent: bool = False,
    has_attachments: bool = False,
) -> bool:
    """仅放行上下文独立、无工具意图、无时效风险的纯问答。"""
    normalized = _normalize(question)
    if not normalized or len(normalized) > 4000:
        return False
    if tool_intent or has_attachments:
        return False
    return not any(
        pattern.search(normalized) for pattern in (_TOOL_ACTION_RE, _DYNAMIC_RE, _CONTEXT_RE)
    )


def is_semantic_cache_answer_candidate(answer: str) -> bool:
    """Reject synthetic/failure text even if an upstream status was misclassified."""
    normalized = answer.strip()
    return bool(normalized) and not normalized.startswith(_SYNTHETIC_FAILURE_PREFIXES)


class SemanticCache:
    """Redis 共享或单进程内存模式的 BGE-M3 语义缓存。"""

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        similarity_threshold: float | None = None,
        ttl_seconds: int | None = None,
        max_entries: int | None = None,
        redis_url: str | None = None,
        embedder: Embedder | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._enabled = enabled
        self._similarity_threshold = similarity_threshold
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._redis_url = redis_url
        self._embedder = embedder
        self._clock = clock
        self._entries: dict[SemanticCacheScope, dict[str, _Entry]] = {}
        self._lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._redis: Any | None = None
        self._redis_failed = False
        self._redis_failure_count = 0
        self._redis_retry_at = 0.0

    @property
    def enabled(self) -> bool:
        if self._enabled is not None:
            return self._enabled
        return bool(getattr(settings, "SEMANTIC_CACHE_ENABLED", False))

    @property
    def backend_kind(self) -> str:
        """返回当前连接状态；memory 也表示已配置 Redis 暂时降级。"""
        redis_url = self._redis_url if self._redis_url is not None else settings.REDIS_URL
        return "redis" if redis_url and not self._redis_failed else "memory"

    @property
    def similarity_threshold(self) -> float:
        value = self._similarity_threshold
        if value is None:
            value = float(
                getattr(
                    settings,
                    "SEMANTIC_CACHE_SIMILARITY_THRESHOLD",
                    DEFAULT_SIMILARITY_THRESHOLD,
                )
            )
        return min(1.0, max(DEFAULT_SIMILARITY_THRESHOLD, float(value)))

    @property
    def ttl_seconds(self) -> int:
        value = self._ttl_seconds
        if value is None:
            value = int(getattr(settings, "SEMANTIC_CACHE_TTL_SECONDS", DEFAULT_TTL_SECONDS))
        return max(1, int(value))

    @property
    def max_entries(self) -> int:
        value = self._max_entries
        if value is None:
            value = int(getattr(settings, "SEMANTIC_CACHE_MAX_ENTRIES", DEFAULT_MAX_ENTRIES))
        return max(1, int(value))

    @property
    def operation_timeout_seconds(self) -> float:
        return max(
            0.05,
            float(
                getattr(
                    settings,
                    "SEMANTIC_CACHE_OPERATION_TIMEOUT_SECONDS",
                    5.0,
                )
            ),
        )

    async def lookup(
        self,
        question: str,
        *,
        scope: SemanticCacheScope,
        tool_intent: bool = False,
        has_attachments: bool = False,
    ) -> SemanticCacheHit | None:
        """查询最相似的未过期回答；任何缓存故障均降级为 miss。"""
        if not self.enabled:
            metrics.record_semantic_cache_lookup("bypass_disabled")
            return None
        if not is_semantic_cache_candidate(
            question, tool_intent=tool_intent, has_attachments=has_attachments
        ):
            metrics.record_semantic_cache_lookup("bypass_risk")
            return None
        try:
            vector = await asyncio.wait_for(
                self._embed(question),
                timeout=self.operation_timeout_seconds,
            )
            redis_url = self._redis_url if self._redis_url is not None else settings.REDIS_URL
            if redis_url:
                hit = await self._lookup_redis(scope, vector)
                # With a configured shared backend, fail closed on Redis
                # outages. Returning a process-local mirror could resurrect an
                # answer cleared or replaced by another replica.
                metrics.record_semantic_cache_lookup(
                    "error" if self._redis_failed else ("hit" if hit else "miss")
                )
                return hit
            hit = await self._lookup_memory(scope, vector)
        except Exception as exc:  # 缓存绝不能阻断主对话
            logger.warning("语义缓存查询失败，按未命中继续: %s", exc)
            metrics.record_semantic_cache_lookup("error")
            return None
        metrics.record_semantic_cache_lookup("hit" if hit is not None else "miss")
        return hit

    async def store(
        self,
        question: str,
        answer: str,
        *,
        scope: SemanticCacheScope,
        tool_intent: bool = False,
        tool_used: bool = False,
        has_attachments: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """保存纯问答结果；工具调用、附件和空回答不会进入缓存。"""
        if not self.enabled or tool_used or not is_semantic_cache_answer_candidate(answer):
            return False
        if not is_semantic_cache_candidate(
            question, tool_intent=tool_intent, has_attachments=has_attachments
        ):
            return False
        try:
            normalized = _normalize(question)
            vector = await asyncio.wait_for(
                self._embed(normalized),
                timeout=self.operation_timeout_seconds,
            )
            now = self._clock()
            entry = _Entry(
                entry_id=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
                vector=tuple(vector),
                answer=answer,
                metadata=_json_safe(metadata or {}),
                created_at=now,
                expires_at=now + self.ttl_seconds,
            )
            redis_url = self._redis_url if self._redis_url is not None else settings.REDIS_URL
            if redis_url:
                return await self._store_redis(scope, entry)
            await self._store_memory(scope, entry)
            return True
        except Exception as exc:  # 缓存写失败不影响已生成的回答
            logger.warning("语义缓存写入失败，忽略本次缓存: %s", exc)
            metrics.record_semantic_cache_lookup("error")
            return False

    async def clear_scope(self, scope: SemanticCacheScope) -> None:
        """清理一个用户/Agent/模型范围内的缓存。"""
        async with self._lock:
            self._entries.pop(scope, None)
        redis = await self._redis_client()
        if redis is None:
            return
        index_key = self._index_key(scope)
        try:
            entry_ids = await redis.zrange(index_key, 0, -1)
            if entry_ids:
                await redis.delete(
                    *(self._entry_key(scope, str(entry_id)) for entry_id in entry_ids)
                )
                await redis.zrem(
                    "semantic_cache:index:global",
                    *(f"{scope.digest}:{entry_id}" for entry_id in entry_ids),
                )
            await redis.delete(index_key)
        except Exception as exc:  # noqa: BLE001
            await self._disable_redis(exc)

    async def close(self) -> None:
        """关闭缓存持有的 Redis 连接。"""
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.warning("关闭语义缓存 Redis 连接失败: %s", exc)
        self._redis = None
        self._redis_failed = False
        self._redis_failure_count = 0
        self._redis_retry_at = 0.0

    async def _embed(self, text: str) -> tuple[float, ...]:
        if self._embedder is None:
            from ..knowledge.embedder import embed_query

            raw = await embed_query(text)
        else:
            raw = self._embedder(text)
            if inspect.isawaitable(raw):
                raw = await raw
        vector = tuple(float(value) for value in raw)
        if not vector or not all(math.isfinite(value) for value in vector):
            raise ValueError("semantic cache embedding must be finite and non-empty")
        return vector

    async def _lookup_memory(
        self, scope: SemanticCacheScope, vector: tuple[float, ...]
    ) -> SemanticCacheHit | None:
        now = self._clock()
        async with self._lock:
            bucket = self._entries.get(scope, {})
            expired = [key for key, entry in bucket.items() if entry.expires_at <= now]
            for key in expired:
                bucket.pop(key, None)
            if not bucket:
                self._entries.pop(scope, None)
            return _best_hit(bucket.values(), vector, self.similarity_threshold)

    async def _store_memory(self, scope: SemanticCacheScope, entry: _Entry) -> None:
        async with self._lock:
            now = self._clock()
            for cached_scope, cached_bucket in list(self._entries.items()):
                for key in [
                    key
                    for key, item in cached_bucket.items()
                    if item.expires_at <= now
                ]:
                    cached_bucket.pop(key, None)
                if not cached_bucket:
                    self._entries.pop(cached_scope, None)
            bucket = self._entries.setdefault(scope, {})
            bucket[entry.entry_id] = entry
            while sum(len(items) for items in self._entries.values()) > self.max_entries:
                oldest_scope, oldest = min(
                    (
                        (candidate_scope, candidate)
                        for candidate_scope, items in self._entries.items()
                        for candidate in items.values()
                    ),
                    key=lambda pair: pair[1].created_at,
                )
                self._entries[oldest_scope].pop(oldest.entry_id, None)
                if not self._entries[oldest_scope]:
                    self._entries.pop(oldest_scope, None)

    async def _lookup_redis(
        self, scope: SemanticCacheScope, vector: tuple[float, ...]
    ) -> SemanticCacheHit | None:
        redis = await self._redis_client()
        if redis is None:
            return None
        index_key = self._index_key(scope)
        try:
            now = self._clock()
            await redis.zremrangebyscore(index_key, "-inf", now)
            entry_ids = await redis.zrange(index_key, -self.max_entries, -1)
            if not entry_ids:
                return None
            payloads = await redis.mget(
                [self._entry_key(scope, str(entry_id)) for entry_id in entry_ids]
            )
            entries: list[_Entry] = []
            missing: list[str] = []
            for entry_id, payload in zip(entry_ids, payloads, strict=True):
                if payload is None:
                    missing.append(str(entry_id))
                    continue
                entry = _entry_from_json(str(entry_id), str(payload))
                if entry.expires_at > now:
                    entries.append(entry)
            if missing:
                await redis.zrem(index_key, *missing)
            return _best_hit(entries, vector, self.similarity_threshold)
        except Exception as exc:  # noqa: BLE001
            await self._disable_redis(exc)
            return None

    async def _store_redis(self, scope: SemanticCacheScope, entry: _Entry) -> bool:
        redis = await self._redis_client()
        if redis is None:
            return False
        index_key = self._index_key(scope)
        try:
            await redis.set(
                self._entry_key(scope, entry.entry_id),
                _entry_to_json(entry),
                ex=self.ttl_seconds,
            )
            await redis.zadd(index_key, {entry.entry_id: entry.expires_at})
            await redis.expire(index_key, self.ttl_seconds * 2)
            global_index = "semantic_cache:index:global"
            global_member = f"{scope.digest}:{entry.entry_id}"
            await redis.zremrangebyscore(global_index, "-inf", self._clock())
            await redis.zadd(global_index, {global_member: entry.expires_at})
            await redis.expire(global_index, self.ttl_seconds * 2)
            size = int(await redis.zcard(global_index))
            overflow = size - self.max_entries
            if overflow > 0:
                evicted = await redis.zpopmin(global_index, overflow)
                for member, _score in evicted or []:
                    digest, entry_id = str(member).split(":", 1)
                    await redis.delete(f"semantic_cache:entry:{digest}:{entry_id}")
                    await redis.zrem(f"semantic_cache:index:{digest}", entry_id)
            return True
        except Exception as exc:  # noqa: BLE001
            await self._disable_redis(exc)
            return False

    async def _redis_client(self) -> Any | None:
        redis_url = self._redis_url if self._redis_url is not None else settings.REDIS_URL
        if not redis_url:
            return None
        if self._redis is not None:
            return self._redis
        if self._redis_failed and time.monotonic() < self._redis_retry_at:
            return None
        async with self._connect_lock:
            if self._redis is not None:
                return self._redis
            if self._redis_failed and time.monotonic() < self._redis_retry_at:
                return None
            client = None
            try:
                from redis.asyncio import from_url

                client = from_url(
                    redis_url,
                    decode_responses=True,
                    socket_connect_timeout=0.5,
                    socket_timeout=0.5,
                )
                await client.ping()
                self._redis = client
                self._redis_failed = False
                self._redis_failure_count = 0
                self._redis_retry_at = 0.0
            except Exception as exc:  # noqa: BLE001
                if client is not None:
                    await client.aclose()
                await self._disable_redis(exc)
        return self._redis

    async def _disable_redis(self, exc: Exception) -> None:
        logger.warning("语义缓存 Redis 不可用，本次按 cache miss 处理: %s", exc)
        client = self._redis
        self._redis_failed = True
        self._redis_failure_count += 1
        retry_delay = min(30.0, float(2 ** min(5, self._redis_failure_count - 1)))
        self._redis_retry_at = time.monotonic() + retry_delay
        self._redis = None
        if client is not None:
            close = getattr(client, "aclose", None)
            if close is not None:
                try:
                    result = close()
                    if inspect.isawaitable(result):
                        await result
                except Exception as close_exc:  # noqa: BLE001
                    logger.warning("关闭故障语义缓存 Redis 连接失败: %s", close_exc)

    @staticmethod
    def _index_key(scope: SemanticCacheScope) -> str:
        return f"semantic_cache:index:{scope.digest}"

    @staticmethod
    def _entry_key(scope: SemanticCacheScope, entry_id: str) -> str:
        return f"semantic_cache:entry:{scope.digest}:{entry_id}"


def _best_hit(
    entries: Sequence[_Entry] | Any,
    vector: tuple[float, ...],
    threshold: float,
) -> SemanticCacheHit | None:
    best: _Entry | None = None
    best_similarity = -1.0
    for entry in entries:
        similarity = _cosine_similarity(vector, entry.vector)
        if similarity > best_similarity:
            best = entry
            best_similarity = similarity
    if best is None or best_similarity < threshold:
        return None
    return SemanticCacheHit(
        answer=best.answer,
        similarity=best_similarity,
        metadata=dict(best.metadata),
    )


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return -1.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return -1.0
    return dot / (left_norm * right_norm)


def _normalize(text: str) -> str:
    return _SPACE_RE.sub(" ", text).strip().casefold()


def _json_safe(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _entry_to_json(entry: _Entry) -> str:
    return json.dumps(
        {
            "vector": list(entry.vector),
            "answer": entry.answer,
            "metadata": entry.metadata,
            "created_at": entry.created_at,
            "expires_at": entry.expires_at,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _entry_from_json(entry_id: str, payload: str) -> _Entry:
    data = json.loads(payload)
    return _Entry(
        entry_id=entry_id,
        vector=tuple(float(value) for value in data["vector"]),
        answer=str(data["answer"]),
        metadata=dict(data.get("metadata") or {}),
        created_at=float(data["created_at"]),
        expires_at=float(data["expires_at"]),
    )


semantic_cache = SemanticCache()
