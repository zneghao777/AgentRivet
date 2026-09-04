"""Run 持久化/恢复后，Worker 必须重新装载各自授权的 MCP 工具。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool

from app.config import settings
from app.harness import events, worker
from app.harness.app_runtime import harness_app
from app.harness.context import get_tool_context
from app.harness.models import TaskCard
from app.harness.run_coordinator import RunCoordinator, run_coordinator
from app.mcp.manager import mcp_manager
from app.storage import chat_runs
from app.storage.db import AsyncSessionLocal
from app.storage.models import Agent, AgentMcpBinding, ChatRun, McpCapability, McpServer


@pytest.fixture
async def mcp_run_env(monkeypatch):
    for name, value in {
        "CHAT_RUN_ENABLED": True,
        "CHAT_RUN_DURABLE_ENABLED": False,
        "MULTI_AGENT_ENABLED": True,
        "MQ_ENABLED": False,
        "MCP_ENABLED": True,
        "MEMORY_ENABLED": False,
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(run_coordinator, "start", AsyncMock())
    monkeypatch.setattr(run_coordinator, "enqueue", AsyncMock())
    tenant = f"run-mcp-{uuid.uuid4().hex}"
    calls = []

    async def web_search(query: str) -> str:
        """检索网络资料。"""
        calls.append(query)
        return "测试来源：https://example.com/book；作者：测试作者"

    tool = StructuredTool.from_function(
        coroutine=web_search,
        name="mcp_search__webSearchStd",
        description="联网检索",
    )
    other_tool = StructuredTool.from_function(
        coroutine=web_search,
        name="mcp_search__unbound",
        description="未绑定的能力",
    )
    async with AsyncSessionLocal() as db:
        server = McpServer(
            tenant_id=tenant,
            name="search",
            endpoint_url="http://127.0.0.1:1/mcp",
            enabled=True,
            health_status="ready",
        )
        agent = Agent(
            tenant_id=tenant,
            name="网络资料考证员",
            description="联网核验资料并标注来源",
            scope="internal",
            status="active",
            memory_enabled=False,
        )
        db.add_all([server, agent])
        await db.flush()
        capability = McpCapability(
            server_id=server.id,
            remote_name="webSearchStd",
            effective_name=tool.name,
            enabled=True,
            review_status="approved",
            schema_hash="test-web-search",
        )
        other_capability = McpCapability(
            server_id=server.id,
            remote_name="unbound",
            effective_name=other_tool.name,
            enabled=True,
            review_status="approved",
            schema_hash="test-unbound",
        )
        db.add_all([capability, other_capability])
        await db.flush()
        db.add(AgentMcpBinding(agent_id=agent.id, capability_id=capability.id))
        await db.commit()
    monkeypatch.setitem(
        mcp_manager._runtime_tools,
        (tenant, server.id),
        {"webSearchStd": tool, "unbound": other_tool},
    )
    return tenant, server.id, capability.id, calls, tool.name, other_tool.name


async def _create_run(client, tenant):
    headers = {"X-API-Key": "test-key-1", "X-Tenant-Id": tenant}
    response = await client.post("/api/v1/sessions", headers=headers, json={})
    assert response.status_code == 200
    session_id = response.json()["data"]["session_id"]
    response = await client.post(
        f"/api/v1/sessions/{session_id}/runs",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"message": "联网核验作者并标注来源", "approval_mode": "auto"},
    )
    assert response.status_code == 202
    return uuid.UUID(response.json()["data"]["run_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True])
async def test_worker_can_call_bound_mcp_after_run_snapshot_restore(
    app_client, monkeypatch, mcp_run_env, resume
):
    tenant, _, capability_id, calls, tool_name, other_tool_name = mcp_run_env
    observed = {}

    class Llm:
        def bind_tools(self, tools):
            observed["bound"] = [tool.name for tool in tools]
            return self

        async def ainvoke(self, messages):
            if isinstance(messages[-1], ToolMessage):
                observed["result"] = messages[-1].content
                return AIMessage(content="已联网核验并取得来源")
            if not observed.get("bound"):
                return AIMessage(content="CANNOT_HANDLE: 当前会话未绑定联网搜索工具")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "search-call",
                        "name": tool_name,
                        "args": {"query": "作者 首发平台"},
                    }
                ],
            )

    async def run_worker():
        ctx = get_tool_context()
        observed["parent_tools"] = [tool.name for tool in ctx.dynamic_tools]
        agent = ctx.orchestration_agents["网络资料考证员"]
        result = await worker._run_agent(
            agent,
            TaskCard(target_agent=agent.name, goal="联网核验作者"),
            [],
            conversation_context="",
            verification_reason=None,
            previous_error=None,
        )
        observed["worker_answer"] = result
        yield events.message_delta("answer", result)
        yield events.run_end(status="success", duration_ms=1, tool_calls=len(calls))

    class Runtime:
        async def astream(self, *_args, **_kwargs):
            if resume:
                yield events.interrupt_requested("等待恢复", {"type": "tool_approval"})
                yield events.run_end(status="interrupted", duration_ms=1, tool_calls=0)
            else:
                async for frame in run_worker():
                    yield frame

        async def resume(self, *_args, **_kwargs):
            async for frame in run_worker():
                yield frame

    monkeypatch.setattr(worker, "create_llm", lambda *_args, **_kwargs: Llm())
    monkeypatch.setattr(harness_app, "runtime", Runtime())
    run_id = await _create_run(app_client, tenant)
    async with AsyncSessionLocal() as db:
        stored = await db.get(ChatRun, run_id)
        snapshot = stored.request_payload["orchestration_agents"][0]
    assert snapshot["allowed_capability_ids"] == [str(capability_id)]
    assert "dynamic_tools" not in snapshot  # 可执行对象不持久化。
    coordinator = RunCoordinator()
    await coordinator.execute(run_id)
    if resume:
        assert calls == []
        async with AsyncSessionLocal() as db:
            pending = await db.get(ChatRun, run_id)
            assert pending.status == "pending_approval"
            await chat_runs.queue_resume(
                db,
                run_id=run_id,
                command={"decision": "approve", "input": "批准"},
                write_outbox=False,
            )
        await coordinator.execute(run_id)
    assert calls == ["作者 首发平台"]
    assert observed["bound"] == [tool_name]
    assert other_tool_name in observed["parent_tools"]
    assert "https://example.com/book" in observed["result"]
    assert "CANNOT_HANDLE" not in observed["worker_answer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", ["disabled", "review", "removed", "server", "empty", "tenant"])
async def test_restored_worker_does_not_gain_unavailable_or_unbound_tools(
    app_client, monkeypatch, mcp_run_env, revoked
):
    tenant, server_id, capability_id, _, _, _ = mcp_run_env
    observed = []

    class Runtime:
        async def astream(self, *_args, **_kwargs):
            agent = get_tool_context().orchestration_agents["网络资料考证员"]
            observed.extend(tool.name for tool in agent.dynamic_tools)
            yield events.message_delta("answer", "完成检查")
            yield events.run_end(status="success", duration_ms=1, tool_calls=0)

    monkeypatch.setattr(harness_app, "runtime", Runtime())
    run_id = await _create_run(app_client, tenant)
    async with AsyncSessionLocal() as db:
        cap = await db.get(McpCapability, capability_id)
        server = await db.get(McpServer, server_id)
        if revoked == "disabled":
            cap.enabled = False
        elif revoked == "review":
            cap.review_status = "pending"
        elif revoked == "removed":
            cap.removed_at = datetime.now(UTC)
        elif revoked == "server":
            server.enabled = False
        elif revoked == "tenant":
            server.tenant_id = f"other-{uuid.uuid4().hex}"
        else:
            run = await db.get(ChatRun, run_id)
            snapshots = [dict(item) for item in run.request_payload["orchestration_agents"]]
            snapshots[0]["allowed_capability_ids"] = []
            run.request_payload = {**run.request_payload, "orchestration_agents": snapshots}
        await db.commit()
    await RunCoordinator().execute(run_id)
    async with AsyncSessionLocal() as db:
        stored = await db.get(ChatRun, run_id)
    assert stored.status == "succeeded"
    assert observed == []
