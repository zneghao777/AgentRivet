"""Tool 注册中心：所有工具必须通过此登记，禁止散落硬编码。"""

from __future__ import annotations

from dataclasses import dataclass

from langchain_core.tools import StructuredTool

from ..config import settings
from .builtin import builtin_calculator, builtin_echo, builtin_get_time
from .builtin.read_skill_file import builtin_read_skill_file
from .knowledge_search import knowledge_search


@dataclass
class ToolInfo:
    """供 API 查询的工具快照。"""

    name: str
    description: str
    source: str = "builtin"


class ToolRegistry:
    """工具注册中心。"""

    def __init__(self) -> None:
        self._tools: dict[str, StructuredTool] = {}
        self._sources: dict[str, str] = {}

    def register(self, tool: StructuredTool, *, source: str = "builtin") -> None:
        """注册工具；名称冲突时抛异常。"""
        if tool.name in self._tools:
            raise ValueError(f"工具名冲突: {tool.name}")
        self._tools[tool.name] = tool
        self._sources[tool.name] = source

    def unregister(self, name: str) -> None:
        """卸载工具。"""
        self._tools.pop(name, None)
        self._sources.pop(name, None)

    def get(self, name: str) -> StructuredTool | None:
        return self._tools.get(name)

    def all(self) -> list[StructuredTool]:
        return list(self._tools.values())

    def snapshot(self) -> list[ToolInfo]:
        return [
            ToolInfo(
                name=name,
                description=tool.description,
                source=self._sources.get(name, "builtin"),
            )
            for name, tool in self._tools.items()
        ]

    def register_builtin(self) -> None:
        """注册底座内置工具（幂等：已注册则跳过）。"""
        tools = [
            builtin_echo,
            builtin_get_time,
            builtin_calculator,
            builtin_read_skill_file,
            knowledge_search,
        ]
        # memory_search 受 MEMORY_SEARCH_TOOL 开关控制
        if settings.MEMORY_SEARCH_TOOL:
            from .memory_search import memory_search

            tools.append(memory_search)
        for tool in tools:
            if tool.name not in self._tools:
                self.register(tool, source="builtin")

    def clear(self) -> None:
        """测试辅助：清空注册中心。"""
        self._tools.clear()
        self._sources.clear()


# 全局单例
tool_registry = ToolRegistry()
