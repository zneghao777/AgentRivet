"""多 Agent Supervisor 骨架：「主管 + 工人」多 Agent 架构。

默认关闭（通过配置 MULTI_AGENT_ENABLED 开启）。
关闭时，所有请求只走主 Agent，行为与单 Agent 一致。
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from langgraph.graph import END, START, StateGraph

from ..agents.base_agent import agent_node
from ..agents.registry import agent_registry, AgentEntry
from ..tools.registry import ToolRegistry
from .state import HarnessState

logger = logging.getLogger(__name__)


def build_supervisor_graph(
    tool_reg: ToolRegistry, *, checkpointer=None
):
    """构建 Supervisor 图：主管路由到主 Agent 或子 Agent。

    若 MULTI_AGENT_ENABLED=false，直接回退为主 Agent 单图。
    """
    from .graph_builder import build_main_graph

    if not _enabled():
        logger.info("多 Agent 未开启，回退主 Agent 图")
        return build_main_graph(tool_reg, checkpointer=checkpointer)

    tools = tool_reg.all()

    def _supervisor(state: dict) -> dict:
        # 简易路由：固定路由到 demo_subagent（演示），否则主 agent
        if agent_registry.get("demo_subagent"):
            state["route"] = "demo_subagent"
        else:
            state["route"] = "main"
        return state

    g = StateGraph(SupervisorState)
    g.add_node("supervisor", _supervisor)
    g.add_node("main", _main_node(tools))

    # 注册子 Agent 节点
    sub_node_names: list[str] = []
    for entry in agent_registry.all():
        node_name = f"sub_{entry.name}"
        g.add_node(node_name, _sub_node(entry))
        sub_node_names.append(node_name)

    g.add_edge(START, "supervisor")

    # 路由：main 或子 Agent
    route_map: dict[str, str] = {"main": "main"}
    for name in sub_node_names:
        route_map[name] = name
    g.add_conditional_edges(
        "supervisor",
        lambda s: f"sub_{s.get('route')}" if s.get("route", "").startswith("demo") else "main",
        route_map,
    )

    g.add_edge("main", END)
    for name in sub_node_names:
        g.add_edge(name, END)

    return g.compile(checkpointer=checkpointer)


class SupervisorState(HarnessState):
    """Supervisor 状态：路由 + 各 Agent 输出。"""

    route: str = "main"
    agent_outputs: dict = {}


def _enabled() -> bool:
    from ..config import settings

    return settings.MULTI_AGENT_ENABLED


def _main_node(tools):
    def _n(state: dict) -> dict:
        state["_tools"] = tools
        return agent_node(state)

    return _n


def _sub_node(entry: AgentEntry):
    def _n(state: dict) -> dict:
        return {"final_answer": "（演示）已路由到子 Agent"}

    return _n
