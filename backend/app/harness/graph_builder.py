"""主 Agent 图构建：ReAct 循环 + HITL。

结构：__start__ → agent_node ──条件──→ tools_node ──→ agent_node（循环）
             └──(无 tool_call)──→ END
"""

from __future__ import annotations

import json
import logging

from langchain_core.messages import ToolMessage
from langgraph.graph import END, START, StateGraph

from ..agents.base_agent import agent_node
from ..config import settings
from ..tools.registry import ToolRegistry
from .context import get_tool_context
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


def build_main_graph(tool_reg: ToolRegistry, *, checkpointer=None):
    """构建主 Agent 图并编译。

    MUST：图构建函数接收 tool_registry 与 checkpointer，保证可测试性与解耦。
    """
    tools = tool_reg.all()

    def _agent(state: dict) -> dict:
        # 将工具注入 state，供 agent_node 读取
        dynamic_tools = get_tool_context().dynamic_tools or []
        state["_tools"] = [*tools, *dynamic_tools]
        try:
            return agent_node(state)
        except Exception as exc:  # noqa: BLE001
            logger.exception("agent_node 执行失败")
            from langchain_core.messages import AIMessage

            return {"messages": [AIMessage(content=f"[Agent 执行出错] {exc}")]}

    g = StateGraph(HarnessState)
    g.add_node("agent", _agent)

    async def _tools(state: dict) -> dict:
        available = {
            tool.name: tool for tool in [*tools, *(get_tool_context().dynamic_tools or [])]
        }
        messages = state.get("messages", [])
        calls = getattr(messages[-1], "tool_calls", []) if messages else []
        results: list[ToolMessage] = []
        for call in calls:
            name = call.get("name", "")
            tool = available.get(name)
            if tool is None:
                results.append(
                    ToolMessage(
                        content=f"工具不可用或未获授权: {name}",
                        tool_call_id=call.get("id", "unknown"),
                        name=name,
                        status="error",
                    )
                )
                continue
            try:
                output = await tool.ainvoke(call.get("args") or {})
                if isinstance(output, (str, list)):
                    content = output
                else:
                    content = json.dumps(output, ensure_ascii=False, default=str)
                if isinstance(content, str):
                    content = content[: settings.MCP_MAX_OUTPUT_BYTES]
                results.append(
                    ToolMessage(
                        content=content,
                        tool_call_id=call.get("id", "unknown"),
                        name=name,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("工具 %s 执行失败: %s", name, exc)
                results.append(
                    ToolMessage(
                        content=f"工具执行失败: {str(exc)[:500]}",
                        tool_call_id=call.get("id", "unknown"),
                        name=name,
                        status="error",
                    )
                )
        return {"messages": results}

    # 动态节点在运行时读取请求态 MCP tools；即使启动时没有静态工具也可工作。
    g.add_node("tools", _tools)

    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", should_continue, {"tools": "tools", "end": END})
    g.add_edge("tools", "agent")

    # HITL：审批类场景通过配置开启 interruptBefore（默认关闭）
    interrupt_before = ["tools"] if settings.GRAPH_INTERRUPT_TOOLS else None

    return g.compile(
        checkpointer=checkpointer,
        interrupt_before=interrupt_before,
    )
