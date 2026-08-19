"""示例子 Agent：demo_subagent（Hello，直接返回问候）。

用于验证多 Agent（Supervisor）链路。
"""

from __future__ import annotations

from langgraph.graph import END, StateGraph

from ..harness.state import HarnessState
from .registry import AgentEntry


def _demo_node(state: dict) -> dict:
    """直接返回问候，不调用 LLM（纯演示）。"""
    user_msg = state.get("messages", [])[-1].content if state.get("messages") else ""
    reply = f"（来自 demo_subagent）你好！已收到你的消息：「{user_msg}」。这是底座的多 Agent 演示链路。"
    return {"final_answer": reply}


def build_demo_subagent() -> AgentEntry:
    """构建并注册 demo_subagent。"""
    g = StateGraph(HarnessState)
    g.add_node("demo", _demo_node)
    g.add_edge("__start__", "demo")
    g.add_edge("demo", END)
    return AgentEntry(
        name="demo_subagent",
        description="演示子 Agent。当用户要求展示多 Agent 能力或打招呼时使用。",
        graph=g,
    )
