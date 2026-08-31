"""API 入口限流：API Key/IP 双维度固定窗口，Redis 失败时降级内存。

该中间件在 FastAPI 依赖解析前运行，因此先把合法 API Key 的不可逆标识写入
``request.state.api_key_id``，再执行限流。业务依赖仍负责完整鉴权。
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from limits import parse
from limits.aio.storage import MemoryStorage, RedisStorage
from limits.aio.strategies import FixedWindowRateLimiter
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from starlette.types import ASGIApp, Receive, Scope, Send

from ..observability.metrics import metrics
from ..security.api_key import hash_api_key, verify_api_key

logger = logging.getLogger(__name__)

RATE_LIMIT_CODE = 42900
RATE_LIMIT_MESSAGE = "请求过于频繁，请稍后重试"
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True)
class RateLimitConfig:
    """L1 限流配置快照。"""

    enabled: bool = True
    stream_per_key: str = "10/minute"
    rest_per_key: str = "60/minute"
    per_ip: str = "120/minute"
    trusted_proxy_hops: int = 0
    redis_url: str | None = None

    @classmethod
    def from_settings(cls, settings: Any) -> RateLimitConfig:
        guard_enabled = bool(getattr(settings, "GUARD_ENABLED", True))
        return cls(
            enabled=guard_enabled and bool(getattr(settings, "RATE_LIMIT_ENABLED", True)),
            stream_per_key=str(getattr(settings, "RATE_LIMIT_STREAM_PER_KEY", "10/minute")),
            rest_per_key=str(getattr(settings, "RATE_LIMIT_REST_PER_KEY", "60/minute")),
            per_ip=str(getattr(settings, "RATE_LIMIT_PER_IP", "120/minute")),
            trusted_proxy_hops=max(
                0, int(getattr(settings, "RATE_LIMIT_TRUSTED_PROXY_HOPS", 0))
            ),
            redis_url=getattr(settings, "REDIS_URL", None),
        )


class ConcurrencyRateLimitMiddleware:
    """只限制写操作与 SSE 发起请求，并统一返回 42900。"""

    def __init__(self, app: ASGIApp, config: RateLimitConfig) -> None:
        self.app = app
        self.config = config
        self._stream_limit = parse(config.stream_per_key)
        self._rest_limit = parse(config.rest_per_key)
        self._ip_limit = parse(config.per_ip)
        self._memory_storage = MemoryStorage("memory://")
        self._storage: Any = self._build_storage(config.redis_url)
        self._limiter = FixedWindowRateLimiter(self._storage)
        self._fallback_lock = asyncio.Lock()
        self._redis_degraded = False

    @property
    def backend_kind(self) -> str:
        """返回当前计数后端，供健康检查和测试读取。"""
        return "memory" if self._storage is self._memory_storage else "redis"

    def _build_storage(self, redis_url: str | None) -> Any:
        if not redis_url:
            return self._memory_storage
        return RedisStorage(
            redis_url,
            implementation="redispy",
            wrap_exceptions=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = _headers(scope)
        api_key = headers.get("x-api-key")
        api_key_id: str | None = None
        if verify_api_key(api_key):
            api_key_id = hash_api_key(api_key or "")
            scope.setdefault("state", {})["api_key_id"] = api_key_id

        bucket = _limited_bucket(scope)
        if not self.config.enabled or bucket is None:
            await self.app(scope, receive, send)
            return

        if api_key_id is not None:
            key_limit = self._stream_limit if bucket == "stream" else self._rest_limit
            allowed, retry_after = await self._hit(key_limit, "api_key", api_key_id, bucket)
        else:
            ip = _client_ip(scope, headers, self.config.trusted_proxy_hops)
            allowed, retry_after = await self._hit(self._ip_limit, "ip", ip, bucket)
        if allowed:
            await self.app(scope, receive, send)
            return

        metrics.record_guard_rejected("rate_limit")
        response = JSONResponse(
            status_code=429,
            content={"code": RATE_LIMIT_CODE, "message": RATE_LIMIT_MESSAGE},
            headers={"Retry-After": str(max(1, retry_after))},
        )
        await response(scope, receive, send)

    async def _hit(self, item: Any, *identifiers: str) -> tuple[bool, int]:
        try:
            allowed = await self._limiter.hit(item, *identifiers)
        except Exception as exc:  # limits 将 Redis 客户端异常包装为 StorageError
            await self._degrade_to_memory(exc)
            allowed = await self._limiter.hit(item, *identifiers)
        if allowed:
            return True, 0
        stats = await self._limiter.get_window_stats(item, *identifiers)
        return False, max(1, math.ceil(stats.reset_time - time.time()))

    async def _degrade_to_memory(self, exc: Exception) -> None:
        if self._storage is self._memory_storage:
            raise exc
        async with self._fallback_lock:
            if self._storage is self._memory_storage:
                return
            self._storage = self._memory_storage
            self._limiter = FixedWindowRateLimiter(self._storage)
            if not self._redis_degraded:
                logger.warning("L1 限流 Redis 不可用，降级为进程内固定窗口: %s", exc)
                self._redis_degraded = True


def install_rate_limiting(app: Any, settings: Any) -> None:
    """安装 L1 中间件，并暴露 SlowAPI 兼容状态与统一异常处理器。

    实际计数使用 ``limits`` 的异步策略，以便一次请求同时原子命中 API Key 与
    IP 两个维度。SlowAPI 的装饰器模型一次只解析一个 key_func，且要求在每个
    路由散落装饰器；这里保留其标准 ``app.state.limiter`` 和异常类型兼容面。
    """
    config = RateLimitConfig.from_settings(settings)
    limiter = Limiter(
        key_func=_slowapi_key,
        storage_uri=config.redis_url or "memory://",
        # Retry-After 由统一 handler 写入；关闭装饰器自动注入，避免强迫所有端点
        # 增加 Response 形参。
        headers_enabled=False,
        enabled=config.enabled,
        swallow_errors=True,
        in_memory_fallback_enabled=True,
    )
    app.state.limiter = limiter
    app.state.rate_limit_config = config
    app.add_exception_handler(RateLimitExceeded, _slowapi_rate_limit_handler)
    app.add_middleware(ConcurrencyRateLimitMiddleware, config=config)


def _limited_bucket(scope: Scope) -> str | None:
    method = str(scope.get("method", "GET")).upper()
    if method not in _MUTATING_METHODS:
        return None
    path = str(scope.get("path", "")).rstrip("/")
    is_stream = path.endswith("/messages/stream") or path.endswith("/resume")
    return "stream" if is_stream else "rest"


def _headers(scope: Scope) -> dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }


def _client_ip(
    scope: Scope,
    headers: dict[str, str],
    trusted_proxy_hops: int = 0,
) -> str:
    """Resolve the client without trusting spoofable forwarding headers by default."""
    client = scope.get("client")
    peer = str(client[0]) if client else "unknown"
    if trusted_proxy_hops <= 0:
        return peer

    forwarded = [
        item.strip()
        for item in headers.get("x-forwarded-for", "").split(",")
        if item.strip()
    ]
    chain = [*forwarded, peer]
    client_index = len(chain) - trusted_proxy_hops - 1
    return chain[max(0, client_index)] if forwarded else peer


def _slowapi_key(request: Request) -> str:
    """SlowAPI 兼容 key_func：优先认证 Key 标识，缺失时使用 IP。"""
    api_key_id = getattr(request.state, "api_key_id", None)
    if api_key_id:
        return str(api_key_id)
    config = getattr(request.app.state, "rate_limit_config", None)
    trusted_proxy_hops = int(getattr(config, "trusted_proxy_hops", 0))
    return _client_ip(request.scope, _headers(request.scope), trusted_proxy_hops)


async def _slowapi_rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """统一所有 SlowAPI 装饰器未来抛出的 429 响应。"""
    metrics.record_guard_rejected("rate_limit")
    retry_after = 1
    limit = getattr(getattr(exc, "limit", None), "limit", None)
    if limit is not None:
        retry_after = max(1, int(limit.get_expiry()))
    response = JSONResponse(
        status_code=429,
        content={"code": RATE_LIMIT_CODE, "message": RATE_LIMIT_MESSAGE},
        headers={"Retry-After": str(retry_after)},
    )
    # 若装饰器已写入 view_rate_limit，复用 SlowAPI 的标准限额响应头。
    if hasattr(request.state, "view_rate_limit"):
        try:
            response = request.app.state.limiter._inject_headers(  # noqa: SLF001
                response, request.state.view_rate_limit
            )
        except Exception:  # noqa: BLE001
            pass
    return response
