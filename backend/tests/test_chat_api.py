"""Chat API 测试：非流式 + 流式 SSE。"""

import pytest

AUTH = {"X-API-Key": "test-key-1"}


@pytest.mark.asyncio
async def test_send_message_non_stream(app_client):
    resp = await app_client.post("/api/v1/sessions", headers=AUTH, json={})
    session_id = resp.json()["data"]["session_id"]

    resp = await app_client.post(
        f"/api/v1/sessions/{session_id}/messages",
        headers=AUTH,
        json={"message": "你好"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["session_id"] == session_id


@pytest.mark.asyncio
async def test_stream_message(app_client):
    resp = await app_client.post("/api/v1/sessions", headers=AUTH, json={})
    session_id = resp.json()["data"]["session_id"]

    async with app_client.stream(
        "POST",
        f"/api/v1/sessions/{session_id}/messages/stream",
        headers=AUTH,
        json={"message": "你好"},
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = ""
        async for chunk in resp.aiter_text():
            body += chunk
        assert "run_end" in body

    history = await app_client.get(
        f"/api/v1/sessions/{session_id}/messages", headers=AUTH
    )
    messages = history.json()["data"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[-1]["content"]
    assert messages[0]["trace_id"]
    assert messages[-1]["trace_id"] == messages[0]["trace_id"]
    assert messages[-1]["request_id"] == messages[0]["request_id"]


@pytest.mark.asyncio
async def test_missing_session_404(app_client):
    resp = await app_client.post(
        "/api/v1/sessions/00000000-0000-0000-0000-000000000000/messages",
        headers=AUTH,
        json={"message": "hi"},
    )
    assert resp.status_code == 404
