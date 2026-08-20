"""Deterministic real-LangGraph fixtures for observability v2 integration tests.

The graphs in this module execute LangGraph, a chat model runnable, and ToolNode.
Only the provider boundary is fake, so ``astream_events(version="v2")`` still
produces real ``run_id``/``parent_ids`` chains for the runtime to instrument.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt

MODEL_NAME = "observability-v2-test-model"
FIRST_USAGE = {"input": 3, "output": 2, "total": 5}
SECOND_USAGE = {"input": 7, "output": 11, "total": 18}
TRACE_USAGE = {"input": 10, "output": 13, "total": 23}


@tool
def observability_lookup(query: str) -> str:
    """Return a deterministic lookup result."""

    return f"lookup-result:{query}"


def _usage_metadata(usage: dict[str, int]) -> dict[str, int]:
    return {
        "input_tokens": usage["input"],
        "output_tokens": usage["output"],
        "total_tokens": usage["total"],
    }


def build_tool_loop_graph(*, model_name: str = MODEL_NAME):
    """Build a real ``LLM -> Tool -> LLM`` LangGraph with known usage."""

    first = AIMessage(
        content="",
        tool_calls=[
            {
                "name": observability_lookup.name,
                "args": {"query": "frozen-contract"},
                "id": "call-observability-v2",
                "type": "tool_call",
            }
        ],
        usage_metadata=_usage_metadata(FIRST_USAGE),
        response_metadata={"model_name": model_name, "finish_reason": "tool_calls"},
    )
    second = AIMessage(
        content="deterministic final answer",
        usage_metadata=_usage_metadata(SECOND_USAGE),
        response_metadata={"model_name": model_name, "finish_reason": "stop"},
    )
    model = FakeMessagesListChatModel(
        responses=[first, second],
        name=model_name,
    )

    async def agent(state: MessagesState) -> dict[str, list[AIMessage]]:
        response = await model.ainvoke(state["messages"])
        return {"messages": [response]}

    def route(state: MessagesState) -> str:
        return "tools" if state["messages"][-1].tool_calls else "end"

    graph = StateGraph(MessagesState)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode([observability_lookup]))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges(
        "agent",
        route,
        {"tools": "tools", "end": END},
    )
    graph.add_edge("tools", "agent")
    return graph.compile()


def build_error_graph():
    """Build a graph whose real node event terminates with an error."""

    async def explode(_state: MessagesState) -> dict[str, Any]:
        raise RuntimeError("deterministic graph failure")

    graph = StateGraph(MessagesState)
    graph.add_node("explode", explode)
    graph.add_edge(START, "explode")
    graph.add_edge("explode", END)
    return graph.compile()


def build_slow_graph(*, delay: float = 0.05):
    """Build a graph that is slow enough for deterministic timeout tests."""

    async def slow(_state: MessagesState) -> dict[str, list[AIMessage]]:
        await asyncio.sleep(delay)
        return {"messages": [AIMessage(content="too late")]}

    graph = StateGraph(MessagesState)
    graph.add_node("slow", slow)
    graph.add_edge(START, "slow")
    graph.add_edge("slow", END)
    return graph.compile()


def build_interrupt_graph():
    """Build a checkpointed graph that pauses via LangGraph ``interrupt``."""

    async def approval(_state: MessagesState) -> dict[str, list[AIMessage]]:
        decision = interrupt({"reason": "approval-required"})
        return {"messages": [AIMessage(content=f"resumed:{decision}")]}

    graph = StateGraph(MessagesState)
    graph.add_node("approval", approval)
    graph.add_edge(START, "approval")
    graph.add_edge("approval", END)
    checkpointer = MemorySaver()
    return graph.compile(checkpointer=checkpointer), checkpointer
