"""Agent 注册中心：所有子 Agent 必须通过此登记。"""

from __future__ import annotations

from dataclasses import dataclass

from langgraph.graph import StateGraph


@dataclass
class AgentEntry:
    """一个已注册的子 Agent。"""

    name: str
    description: str
    graph: StateGraph


class AgentRegistry:
    """子 Agent 注册中心。"""

    def __init__(self) -> None:
        self._agents: dict[str, AgentEntry] = {}

    def register(self, agent: AgentEntry) -> None:
        if agent.name in self._agents:
            raise ValueError(f"Agent 名冲突: {agent.name}")
        self._agents[agent.name] = agent

    def get(self, name: str) -> AgentEntry | None:
        return self._agents.get(name)

    def all(self) -> list[AgentEntry]:
        return list(self._agents.values())

    def clear(self) -> None:
        self._agents.clear()


agent_registry = AgentRegistry()
