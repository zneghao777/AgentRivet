"""工具运行上下文：通过 contextvars 保存当前请求的租户与知识库白名单。

设计动机（SPEC §3.5）：
- 路由层（router_chat）在调用 harness runtime 前 set()；
- knowledge_search 工具闭包内 get()；
- 与"工具由 graph_builder 注入 _tools 到 state"的机制互补，避免改动图结构。
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


@dataclass
class ToolContext:
    """一次请求内的工具执行上下文。"""

    tenant_id: str = ""
    user_id: str = ""
    # 知识库白名单；None 表示使用全部已启用知识库，空列表表示全部停用
    allowed_kb_ids: list[uuid.UUID] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "allowed_kb_ids": (
                [str(k) for k in self.allowed_kb_ids]
                if self.allowed_kb_ids is not None
                else None
            ),
        }


_tool_context: ContextVar[ToolContext | None] = ContextVar(
    "tool_context", default=None
)


def set_tool_context(ctx: ToolContext) -> None:
    """设置当前请求的工具上下文（由路由层调用）。"""
    _tool_context.set(ctx)


def get_tool_context() -> ToolContext:
    """获取当前工具上下文（工具内部调用）；缺省返回空上下文。"""
    ctx = _tool_context.get()
    return ctx if ctx is not None else ToolContext()


def reset_tool_context() -> None:
    """清空上下文（请求结束/测试隔离）。"""
    _tool_context.set(None)
