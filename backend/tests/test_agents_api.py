"""AgentHub CRUD、绑定与会话切换测试。"""

from __future__ import annotations

import uuid

import pytest

AUTH = {"X-API-Key": "test-key-1"}


@pytest.mark.asyncio
async def test_agent_crud_bindings_and_session_switch(app_client):
    suffix = uuid.uuid4().hex[:8]
    created = await app_client.post(
        "/api/v1/agents",
        headers=AUTH,
        json={
            "name": f"测试 Agent {suffix}",
            "description": "只用于 AgentHub API 回归测试",
            "system_prompt": "回答前先确认任务边界。",
            "memory_enabled": False,
        },
    )
    assert created.status_code == 200
    agent = created.json()["data"]
    agent_id = agent["id"]
    assert agent["status"] == "active"
    assert agent["scope"] == "user"

    options = (await app_client.get("/api/v1/agents/options", headers=AUTH)).json()["data"]
    skill_ids = [options["skills"][0]["id"]] if options["skills"] else []
    tool_names = [options["tools"][0]["name"]] if options["tools"] else []
    skill_resp = await app_client.put(
        f"/api/v1/agents/{agent_id}/bindings/skills",
        headers=AUTH,
        json={"skill_ids": skill_ids},
    )
    assert skill_resp.status_code == 200
    tool_resp = await app_client.put(
        f"/api/v1/agents/{agent_id}/bindings/tools",
        headers=AUTH,
        json={"tool_names": tool_names},
    )
    assert tool_resp.status_code == 200

    detail = (await app_client.get(f"/api/v1/agents/{agent_id}", headers=AUTH)).json()["data"]
    assert [item["id"] for item in detail["bindings"]["skills"]] == skill_ids
    assert detail["bindings"]["tools"] == tool_names

    session = await app_client.post(
        "/api/v1/sessions",
        headers=AUTH,
        json={"title": "AgentHub 测试会话", "agent_id": agent_id},
    )
    assert session.status_code == 200
    session_id = session.json()["data"]["session_id"]
    assert session.json()["data"]["agent_id"] == agent_id

    sent = await app_client.post(
        f"/api/v1/sessions/{session_id}/messages",
        headers=AUTH,
        json={"message": "说明当前身份"},
    )
    assert sent.status_code == 200
    history = (
        await app_client.get(f"/api/v1/sessions/{session_id}/messages", headers=AUTH)
    ).json()["data"]
    assert history[-1]["agent_id"] == agent_id
    assert history[-1]["agent_name"] == agent["name"]
    usage = (
        await app_client.get(f"/api/v1/agents/{agent_id}/usage", headers=AUTH)
    ).json()["data"]
    assert usage["messages"] >= 1
    assert {"messages", "tool_calls", "tokens", "trend"} <= set(usage)

    switched = await app_client.patch(
        f"/api/v1/sessions/{session_id}", headers=AUTH, json={"agent_id": None}
    )
    assert switched.status_code == 200
    assert switched.json()["data"]["agent_id"] is None

    archived = await app_client.delete(f"/api/v1/agents/{agent_id}", headers=AUTH)
    assert archived.status_code == 200
    assert (await app_client.get(f"/api/v1/agents/{agent_id}", headers=AUTH)).status_code == 404


@pytest.mark.asyncio
async def test_agent_duplicate_name_returns_409(app_client):
    name = f"重名 Agent {uuid.uuid4().hex[:8]}"
    payload = {"name": name, "description": "测试租户内唯一名称"}
    first = await app_client.post("/api/v1/agents", headers=AUTH, json=payload)
    assert first.status_code == 200
    second = await app_client.post("/api/v1/agents", headers=AUTH, json=payload)
    assert second.status_code == 409
    await app_client.delete(f"/api/v1/agents/{first.json()['data']['id']}", headers=AUTH)


@pytest.mark.asyncio
async def test_agent_hub_disabled_returns_404(app_client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "AGENT_HUB_ENABLED", False)
    response = await app_client.get("/api/v1/agents", headers=AUTH)
    assert response.status_code == 404
