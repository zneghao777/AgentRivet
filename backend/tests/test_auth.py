"""鉴权测试：无/非法 API Key 返回 401。"""

import pytest


@pytest.mark.asyncio
async def test_missing_api_key_401(app_client):
    resp = await app_client.get("/api/v1/sessions")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_invalid_api_key_401(app_client):
    resp = await app_client.get("/api/v1/sessions", headers={"X-API-Key": "bad-key"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_valid_api_key_ok(app_client):
    resp = await app_client.get(
        "/api/v1/sessions", headers={"X-API-Key": "test-key-1"}
    )
    assert resp.status_code == 200
    assert resp.json()["code"] == 0
