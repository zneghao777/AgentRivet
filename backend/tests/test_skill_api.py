"""Skill API 测试。"""

import pytest

AUTH = {"X-API-Key": "test-key-1"}


@pytest.mark.asyncio
async def test_list_skills(app_client):
    resp = await app_client.get("/api/v1/skills", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    names = [s["name"] for s in body["data"]]
    assert "skill_hello" in names


@pytest.mark.asyncio
async def test_skill_detail(app_client):
    resp = await app_client.get("/api/v1/skills/skill_hello", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["data"]["name"] == "skill_hello"


@pytest.mark.asyncio
async def test_skill_enable_disable(app_client):
    # 停用
    resp = await app_client.post(
        "/api/v1/skills/skill_hello/disable", headers=AUTH
    )
    assert resp.json()["data"]["enabled"] is False

    # 启用
    resp = await app_client.post(
        "/api/v1/skills/skill_hello/enable", headers=AUTH
    )
    assert resp.json()["data"]["enabled"] is True


@pytest.mark.asyncio
async def test_skill_reload(app_client):
    resp = await app_client.post("/api/v1/skills/reload", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["code"] == 0
