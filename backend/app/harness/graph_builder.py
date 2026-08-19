"""主 Agent 图构建：ReAct 循环 + HITL。

结构：__start__ → agent_node ──条件──→ tools_node ──→ agent_node（循环）
             └──(无 tool_call)──→ END
"""

from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from ..agents.base_agent import agent_node
from ..config import settings
from ..tools.registry import ToolRegistry
from .state import HarnessState

logger = logging.getLogger(__name__)


def should_continue(state: dict) -> str:
    """条件边：最后一条消息含 tool_calls → 进入 tools，否则结束。"""
    messages = state.get("messages", [])
    if not messages:
        return "end"
    last = messages[-1]
    if hasattr(last, "tool_calls") and last.tool_calls:
        return "tools"
    return "end"


def build_main_graph(
    tool_reg: ToolRegistry, *, checkpointer=None
):
    """构建主 Agent 图并编译。

    MUST：图构建函数接收 tool_registry 与 checkpointer，保证可测试性与解耦。
    """
    tools = tool_reg.all()

    def _agent(state: dict) -> dict:
        # 将工具注入 state，供 agent_node 读取
        state["_tools"] = tools
        try:
            return agent_node(state)
        except Exception as exc:  # noqa: BLE001
            logger.exception("agent_node 执行失败")
            from langchain_core.messages import AIMessage

            return {
                "messages": [
                    AIMessage(content=f"[Agent 执行出错] {exc}")
                ]
            }

    g = StateGraph(HarnessState)
    g.add_node("agent", _agent)
    tools_node = ToolNode(tools) if tools else None
    if tools_node is not None:
        g.add_node("tools", tools_node)

    g.add_edge(START, "agent")
    if tools_node is not None:
        g.add_conditional_edges(
            "agent", should_continue, {"tools": "tools", "end": END}
        )
        g.add_edge("tools", "agent")
    else:
        g.add_edge("agent", END)

    # HITL：审批类场景通过配置开启 interruptBefore（默认关闭）
    interrupt_before = ["tools"] if settings.GRAPH_INTERRUPT_TOOLS else None

    return g.compile(
        checkpointer=checkpointer,
        interrupt_before=interrupt_before,
    )
