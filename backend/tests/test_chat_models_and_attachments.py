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

    async def fake_call(base_url: str, model: str, api_key: str):
        assert base_url == "https://api.deepseek.com"
        assert model == "deepseek-v4-pro"
        assert api_key == "provider-secret"
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
async def test_model_connection_uses_custom_url_and_key(app_client, monkeypatch):
    from app.api import router_admin

    async def fake_call(base_url: str, model: str, api_key: str):
        assert base_url == "https://models.example.com/v1"
        assert model == "team-reasoner-v1"
        assert api_key == "custom-provider-secret"
        return {"resolved_model": model}

    monkeypatch.setattr(router_admin, "_call_model_connection", fake_call)
    response = await app_client.post(
        "/api/v1/admin/test-connection",
        headers=AUTH,
        json={
            "model": "team-reasoner-v1",
            "base_url": "https://models.example.com/v1/",
            "api_key": "custom-provider-secret",
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["resolved_model"] == "team-reasoner-v1"


@pytest.mark.asyncio
async def test_model_connection_allows_custom_endpoint_without_key(app_client, monkeypatch):
    from app.api import router_admin

    async def fake_call(base_url: str, model: str, api_key: str):
        assert base_url == "http://127.0.0.1:1234/v1"
        assert model == "local-model"
        assert api_key == ""
        return {"resolved_model": model}

    monkeypatch.setattr(router_admin, "_call_model_connection", fake_call)
    response = await app_client.post(
        "/api/v1/admin/test-connection",
        headers=AUTH,
        json={
            "model": "local-model",
            "base_url": "http://127.0.0.1:1234/v1",
            "api_key": "",
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["connected"] is True


@pytest.mark.asyncio
async def test_model_connection_helper_uses_online_guard(monkeypatch):
    from app.agents import base_agent
    from app.api.router_admin import _call_model_connection

    captured: dict = {}

    class FakeLLM:
        async def ainvoke(self, messages):
            captured["messages"] = messages
            return type(
                "Response",
                (),
                {"response_metadata": {"model_name": "resolved-model"}},
            )()

    def fake_create_llm(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeLLM()

    monkeypatch.setattr(base_agent, "create_llm", fake_create_llm)
    result = await _call_model_connection(
        "http://model.test/v1",
        "requested-model",
        "provider-key",
    )

    assert result == {"resolved_model": "resolved-model"}
    assert captured["kwargs"]["call_channel"] == "online"
    assert captured["kwargs"]["max_tokens"] == 1


def test_custom_model_runtime_config_is_request_scoped(monkeypatch):
    from app.agents import base_agent
    from app.harness.context import ToolContext, reset_tool_context, set_tool_context

    captured = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(base_agent, "ChatOpenAI", FakeChatOpenAI)
    set_tool_context(
        ToolContext(
            model_id="team-reasoner-v1",
            model_base_url="https://models.example.com/v1",
            model_api_key="custom-provider-secret",
        )
    )
    try:
        base_agent.create_llm()
    finally:
        reset_tool_context()

    assert captured["model"] == "team-reasoner-v1"
    assert captured["base_url"] == "https://models.example.com/v1"
    assert captured["api_key"] == "custom-provider-secret"


def test_local_model_without_key_uses_non_secret_sdk_placeholder(monkeypatch):
    from app.agents import base_agent
    from app.harness.context import ToolContext, reset_tool_context, set_tool_context

    captured = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(base_agent, "ChatOpenAI", FakeChatOpenAI)
    set_tool_context(
        ToolContext(
            model_id="local-model",
            model_base_url="http://127.0.0.1:1234/v1",
            model_api_key="",
        )
    )
    try:
        base_agent.create_llm()
    finally:
        reset_tool_context()

    assert captured["api_key"] == "not-needed"


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
