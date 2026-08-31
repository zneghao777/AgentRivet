"""CONCURRENCY_GUARD_SPEC §4：L1 入口限流。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from slowapi import Limiter

from app.api.rate_limit import (
    ConcurrencyRateLimitMiddleware,
    RateLimitConfig,
    install_rate_limiting,
)
from app.security.api_key import hash_api_key


def _app(
    *,
    rest: str = "2/minute",
    stream: str = "1/minute",
    ip: str = "100/minute",
    trusted_proxy_hops: int = 0,
):
    app = FastAPI()
    settings = SimpleNamespace(
        GUARD_ENABLED=True,
        RATE_LIMIT_ENABLED=True,
        RATE_LIMIT_STREAM_PER_KEY=stream,
        RATE_LIMIT_REST_PER_KEY=rest,
        RATE_LIMIT_PER_IP=ip,
        RATE_LIMIT_TRUSTED_PROXY_HOPS=trusted_proxy_hops,
        REDIS_URL=None,
    )
    install_rate_limiting(app, settings)

    @app.post("/api/v1/items")
    async def create_item(request: Request):
        return {"api_key_id": getattr(request.state, "api_key_id", None)}

    @app.get("/api/v1/items")
    async def list_items():
        return {"ok": True}

    @app.post("/api/v1/sessions/1/messages/stream")
    async def stream():
        return {"ok": True}

    @app.post("/api/v1/sessions/1/resume")
    async def resume():
        return {"ok": True}

    @app.post("/api/v1/decorated")
    @app.state.limiter.limit("1/minute")
    async def decorated(request: Request):
        return {"api_key_id": getattr(request.state, "api_key_id", None)}

    return app


@pytest.mark.asyncio
async def test_api_key_limit_injects_hashed_identity_and_returns_unified_429():
    app = _app()
    assert isinstance(app.state.limiter, Limiter)
    transport = ASGITransport(app=app)
    headers = {"X-API-Key": "test-key-1"}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/api/v1/items", headers=headers)
        second = await client.post("/api/v1/items", headers=headers)
        rejected = await client.post("/api/v1/items", headers=headers)

    assert first.json()["api_key_id"] == hash_api_key("test-key-1")
    assert second.status_code == 200
    assert rejected.status_code == 429
    assert rejected.json() == {
        "code": 42900,
        "message": "请求过于频繁，请稍后重试",
    }
    assert int(rejected.headers["retry-after"]) >= 1


@pytest.mark.asyncio
async def test_slowapi_decorator_uses_the_same_429_contract():
    app = _app(rest="100/minute", ip="100/minute")
    transport = ASGITransport(app=app)
    headers = {"X-API-Key": "test-key-1"}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post("/api/v1/decorated", headers=headers)).status_code == 200
        rejected = await client.post("/api/v1/decorated", headers=headers)

    assert rejected.status_code == 429
    assert rejected.json() == {
        "code": 42900,
        "message": "请求过于频繁，请稍后重试",
    }
    assert int(rejected.headers["retry-after"]) >= 1


@pytest.mark.asyncio
async def test_stream_limit_is_independent_and_read_only_routes_are_not_limited():
    app = _app(rest="1/minute", stream="1/minute", ip="1000/minute")
    transport = ASGITransport(app=app)
    headers = {"X-API-Key": "test-key-1"}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post("/api/v1/items", headers=headers)).status_code == 200
        assert (await client.post("/api/v1/items", headers=headers)).status_code == 429
        assert (
            await client.post("/api/v1/sessions/1/messages/stream", headers=headers)
        ).status_code == 200
        assert (
            await client.post("/api/v1/sessions/1/messages/stream", headers=headers)
        ).status_code == 429
        for _ in range(3):
            assert (await client.get("/api/v1/items", headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_resume_uses_the_sse_rate_limit_and_unified_error_contract():
    app = _app(rest="100/minute", stream="1/minute", ip="1000/minute")
    transport = ASGITransport(app=app)
    headers = {"X-API-Key": "test-key-1"}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/api/v1/sessions/1/resume", headers=headers)
        rejected = await client.post("/api/v1/sessions/1/resume", headers=headers)

    assert first.status_code == 200
    assert rejected.status_code == 429
    assert rejected.json() == {
        "code": 42900,
        "message": "请求过于频繁，请稍后重试",
    }
    assert int(rejected.headers["retry-after"]) >= 1


@pytest.mark.asyncio
async def test_ip_fallback_limits_requests_without_a_valid_api_key():
    app = _app(rest="100/minute", ip="1/minute")
    transport = ASGITransport(app=app)
    headers = {"X-API-Key": "invalid"}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/api/v1/items", headers=headers)
        rejected = await client.post("/api/v1/items", headers=headers)

    assert first.status_code == 200
    assert first.json()["api_key_id"] is None
    assert rejected.status_code == 429


@pytest.mark.asyncio
async def test_valid_api_keys_do_not_share_the_ip_fallback_bucket():
    app = _app(rest="1/minute", ip="1/minute")
    transport = ASGITransport(app=app, client=("198.51.100.20", 1234))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/api/v1/items", headers={"X-API-Key": "test-key-1"})
        second = await client.post("/api/v1/items", headers={"X-API-Key": "test-key-2"})

    assert first.status_code == 200
    assert second.status_code == 200


@pytest.mark.asyncio
async def test_untrusted_forwarded_for_cannot_evade_ip_limit():
    app = _app(rest="100/minute", ip="1/minute")
    transport = ASGITransport(app=app, client=("198.51.100.20", 1234))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/v1/items",
            headers={"X-Forwarded-For": "203.0.113.1"},
        )
        rejected = await client.post(
            "/api/v1/items",
            headers={"X-Forwarded-For": "203.0.113.2"},
        )

    assert first.status_code == 200
    assert rejected.status_code == 429


@pytest.mark.asyncio
async def test_one_trusted_proxy_hop_uses_forwarded_client_identity():
    app = _app(rest="100/minute", ip="1/minute", trusted_proxy_hops=1)
    transport = ASGITransport(app=app, client=("172.20.0.10", 1234))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first_client = await client.post(
            "/api/v1/items",
            headers={"X-Forwarded-For": "198.51.100.1"},
        )
        second_client = await client.post(
            "/api/v1/items",
            headers={"X-Forwarded-For": "198.51.100.2"},
        )
        first_client_again = await client.post(
            "/api/v1/items",
            headers={"X-Forwarded-For": "198.51.100.1"},
        )

    assert first_client.status_code == 200
    assert second_client.status_code == 200
    assert first_client_again.status_code == 429


@pytest.mark.asyncio
async def test_redis_failure_degrades_to_memory_fixed_window():
    inner = FastAPI()

    @inner.post("/api/v1/items")
    async def create_item():
        return {"ok": True}

    middleware = ConcurrencyRateLimitMiddleware(
        inner,
        RateLimitConfig(
            enabled=True,
            rest_per_key="1/minute",
            stream_per_key="1/minute",
            per_ip="100/minute",
            redis_url="redis://127.0.0.1:6399/15",
        ),
    )
    transport = ASGITransport(app=middleware)
    headers = {"X-API-Key": "test-key-1"}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post("/api/v1/items", headers=headers)).status_code == 200
        assert (await client.post("/api/v1/items", headers=headers)).status_code == 429

    assert middleware.backend_kind == "memory"
