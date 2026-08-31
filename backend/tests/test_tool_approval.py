"""会话级工具审批与 LangGraph 中断恢复。"""

from __future__ import annotations

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import MemorySaver

import app.harness.graph_builder as graph_builder_module
from app.harness.context import ToolContext, reset_tool_context, set_tool_context
from app.harness.graph_builder import build_main_graph
from app.harness.graph_runtime import GraphRuntime
from app.tools.registry import ToolRegistry


def _runtime(monkeypatch, calls: list[str]) -> GraphRuntime:
    async def guarded_lookup(query: str) -> str:
        calls.append(query)
        return f"result:{query}"

    tool = StructuredTool.from_function(
        coroutine=guarded_lookup,
        name="web_search",
        description="Search the internet for current information.",
    )
    registry = ToolRegistry()
    registry.register(tool)

    def fake_agent(state: dict) -> dict:
        if any(isinstance(message, ToolMessage) for message in state["messages"]):
            last_tool = next(
                message
                for message in reversed(state["messages"])
                if isinstance(message, ToolMessage)
            )
            return {"messages": [AIMessage(content=f"continued:{last_tool.content}")]}
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "web_search",
                            "args": {"query": "latest"},
                            "id": "call-web-1",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        }

    monkeypatch.setattr(graph_builder_module, "agent_node", fake_agent)
    checkpointer = MemorySaver()
    return GraphRuntime(
        build_main_graph(registry, checkpointer=checkpointer),
        checkpointer,
    )


async def _frames(stream) -> str:
    return "".join([frame async for frame in stream])


async def test_manual_mode_interrupts_before_tool_and_approve_resumes(monkeypatch):
    calls: list[str] = []
    runtime = _runtime(monkeypatch, calls)
    set_tool_context(ToolContext(tenant_id="tenant-a", user_id="user-a"))
    try:
        initial = await _frames(
            runtime.astream(
                "thread-approve",
                "find it",
                "tenant-a",
                "user-a",
                [],
                approval_mode="manual",
            )
        )
        assert calls == []
        assert "event: interrupt_requested" in initial
        assert '"name": "web_search"' in initial
        assert '"status": "interrupted"' in initial
        assert await runtime.pending_approval("thread-approve", "tenant-a") is not None

        resumed = await _frames(
            runtime.resume(
                "thread-approve",
                "同意联网",
                "approve",
                tenant_id="tenant-a",
                user_id="user-a",
            )
        )
        assert calls == ["latest"]
        assert "continued:result:latest" in resumed
        assert '"status": "success"' in resumed
        assert await runtime.pending_approval("thread-approve", "tenant-a") is None
    finally:
        reset_tool_context()


async def test_manual_mode_rejects_without_executing_tool(monkeypatch):
    calls: list[str] = []
    runtime = _runtime(monkeypatch, calls)
    set_tool_context(ToolContext(tenant_id="tenant-a", user_id="user-a"))
    try:
        await _frames(
            runtime.astream(
                "thread-reject",
                "find it",
                "tenant-a",
                "user-a",
                [],
                approval_mode="manual",
            )
        )
        resumed = await _frames(
            runtime.resume(
                "thread-reject",
                "不要联网",
                "reject",
                tenant_id="tenant-a",
                user_id="user-a",
            )
        )
        assert calls == []
        assert "用户拒绝执行工具" in resumed
        assert '"status": "success"' in resumed
    finally:
        reset_tool_context()


async def test_auto_mode_executes_without_interrupt(monkeypatch):
    calls: list[str] = []
    runtime = _runtime(monkeypatch, calls)
    set_tool_context(ToolContext(tenant_id="tenant-a", user_id="user-a"))
    try:
        frames = await _frames(
            runtime.astream(
                "thread-auto",
                "find it",
                "tenant-a",
                "user-a",
                [],
                approval_mode="auto",
            )
        )
        assert calls == ["latest"]
        assert "event: interrupt_requested" not in frames
        assert '"status": "success"' in frames
    finally:
        reset_tool_context()
