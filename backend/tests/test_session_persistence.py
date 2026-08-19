"""会话持久化测试：创建、查询、删除、历史。"""

import pytest

AUTH = {"X-API-Key": "test-key-1"}


@pytest.mark.asyncio
async def test_create_and_get_session(app_client):
    # 创建
    resp = await app_client.post("/api/v1/sessions", headers=AUTH, json={})
    assert resp.status_code == 200
    data = resp.json()["data"]
    session_id = data["session_id"]
    assert data["thread_id"].startswith("thr_")
    assert data["status"] == "active"

    # 详情
    resp = await app_client.get(f"/api/v1/sessions/{session_id}", headers=AUTH)
    assert resp.json()["data"]["session_id"] == session_id


@pytest.mark.asyncio
async def test_session_list(app_client):
    resp = await app_client.get(
        "/api/v1/sessions?page=1&page_size=10", headers=AUTH
    )
    body = resp.json()
    assert body["code"] == 0
    assert "items" in body["data"]


@pytest.mark.asyncio
async def test_session_delete(app_client):
    resp = await app_client.post("/api/v1/sessions", headers=AUTH, json={})
    session_id = resp.json()["data"]["session_id"]

    resp = await app_client.delete(f"/api/v1/sessions/{session_id}", headers=AUTH)
    assert resp.json()["code"] == 0

    # 删除后再查返回 404
    resp = await app_client.get(f"/api/v1/sessions/{session_id}", headers=AUTH)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_first_message_updates_default_session_title(app_client):
    resp = await app_client.post(
        "/api/v1/sessions", headers=AUTH, json={"title": "新对话"}
    )
    session_id = resp.json()["data"]["session_id"]

    await app_client.post(
        f"/api/v1/sessions/{session_id}/messages",
        headers=AUTH,
        json={"message": "帮我整理本周项目进展和风险"},
    )

    resp = await app_client.get(f"/api/v1/sessions/{session_id}", headers=AUTH)
    assert resp.json()["data"]["title"] == "帮我整理本周项目进展和风险"
