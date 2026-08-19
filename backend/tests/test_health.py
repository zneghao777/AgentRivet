"""健康检查测试（不鉴权）。"""

import pytest


@pytest.mark.asyncio
async def test_health(app_client):
    resp = await app_client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["status"] == "ok"


@pytest.mark.asyncio
async def test_health_ready(app_client):
    resp = await app_client.get("/api/v1/health/ready")
    assert resp.status_code == 200
    assert "ready" in resp.json()["data"]
