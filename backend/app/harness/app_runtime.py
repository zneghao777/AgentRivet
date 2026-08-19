"""应用运行时单例：持有编译后的图 + GraphRuntime。

在启动钩子中初始化（build_harness），供路由使用。
"""

from __future__ import annotations

from typing import Any

from .graph_builder import build_main_graph
from .graph_runtime import GraphRuntime
from .supervisor import build_supervisor_graph


class HarnessApp:
    """全局 Harness 运行实例。"""

    def __init__(self) -> None:
        self.graph: Any = None
        self.runtime: GraphRuntime | None = None
        self.checkpointer: Any = None

    def build(self, tool_reg, checkpointer: Any | None = None) -> None:
        """构建并缓存图与运行时。"""
        self.checkpointer = checkpointer
        graph = build_supervisor_graph(tool_reg, checkpointer=checkpointer)
        self.graph = graph
        self.runtime = GraphRuntime(graph, checkpointer=checkpointer)


harness_app = HarnessApp()
