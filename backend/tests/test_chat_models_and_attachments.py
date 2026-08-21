from __future__ import annotations

import uuid

import pytest
from langchain_core.messages import HumanMessage

from app.agents.base_agent import _with_attachments
from app.chat.attachments import prepare_attachments, save_attachment
from app.config import settings

AUTH = {"X-API-Key": "test-key-1"}


@pytest.mark.asyncio
async def test_model_connection_endpoint_uses_requested_model(app_client, monkeypatch):
    from app.api import router_admin

    monkeypatch.setattr(settings, "LLM_API_KEY", "provider-secret")
    monkeypatch.setattr(settings, "LLM_BASE_URL", "https://api.deepseek.com")

    async def fake_call(base_url: str, model: str):
        assert base_url == "https://api.deepseek.com"
        assert model == "deepseek-v4-pro"
        return {"resolved_model": model}

    monkeypatch.setattr(router_admin, "_call_model_connection", fake_call)
    response = await app_client.post(
        "/api/v1/admin/test-connection",
        headers=AUTH,
        json={"model": "deepseek-v4-pro", "base_url": "https://api.deepseek.com"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["connected"] is True
    assert response.json()["data"]["resolved_model"] == "deepseek-v4-pro"


@pytest.mark.asyncio
async def test_chat_attachment_upload_and_delete(app_client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "KB_UPLOAD_DIR", str(tmp_path))
    created = await app_client.post("/api/v1/sessions", headers=AUTH, json={"title": "附件测试"})
    session_id = created.json()["data"]["session_id"]
    png = b"\x89PNG\r\n\x1a\n" + b"test-image"

    uploaded = await app_client.post(
        f"/api/v1/sessions/{session_id}/attachments",
        headers=AUTH,
        files={"file": ("screen.png", png, "image/png")},
    )

    assert uploaded.status_code == 200
    metadata = uploaded.json()["data"]
    assert metadata["kind"] == "image"
    assert metadata["mime_type"] == "image/png"

    deleted = await app_client.delete(
        f"/api/v1/sessions/{session_id}/attachments/{metadata['id']}",
        headers=AUTH,
    )
    assert deleted.status_code == 200
    assert deleted.json()["data"]["deleted"] is True
    await app_client.delete(f"/api/v1/sessions/{session_id}", headers=AUTH)


def test_prepare_text_attachment_and_inject_into_last_user_message(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "KB_UPLOAD_DIR", str(tmp_path))
    session_id = uuid.uuid4()
    metadata = save_attachment(
        "tenant-a",
        session_id,
        "notes.md",
        "项目结论：附件链路可用。".encode(),
    )

    prepared = prepare_attachments("tenant-a", session_id, [metadata["id"]])
    messages = [HumanMessage(content="请总结")]
    enriched = _with_attachments(messages, prepared)

    assert enriched is not messages
    assert "项目结论：附件链路可用。" in enriched[0].content
    assert messages[0].content == "请总结"
