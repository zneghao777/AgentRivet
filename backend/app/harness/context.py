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
from typing import Any, Literal

from langchain_core.tools import BaseTool

from ..agents.models import OrchestrationAgent


@dataclass
class ToolContext:
    """一次请求内的工具执行上下文。"""

    tenant_id: str = ""
    user_id: str = ""
    # 知识库白名单；None 表示使用全部已启用知识库，空列表表示全部停用
    allowed_kb_ids: list[uuid.UUID] | None = None
    # 请求态动态工具只驻留 ContextVar，不进入 LangGraph checkpoint。
    dynamic_tools: list[BaseTool] | None = None
    # 请求级模型连接与附件仅驻留 ContextVar，避免写入 checkpoint。
    model_id: str | None = None
    model_base_url: str | None = None
    model_api_key: str | None = None
    attachments: list[dict[str, Any]] | None = None
    # LLM 防护通道与请求开始时的降级快照；后台任务必须显式标记。
    call_channel: Literal["online", "background"] = "online"
    degrade_level: int | None = None
    # Monotonic deadline for the first visible answer/error commitment.  It is
    # cleared after the first user-visible token so later tool-loop calls keep
    # their normal per-call timeout.
    outcome_deadline_monotonic: float | None = None
    # AgentHub 定义解析结果；全部只在一次请求内存在。
    agent_id: uuid.UUID | None = None
    agent_name: str | None = None
    agent_system_prompt: str | None = None
    allowed_skill_names: list[str] | None = None
    allowed_tool_names: set[str] | None = None
    allowed_capability_ids: set[uuid.UUID] | None = None
    memory_enabled: bool = True
    orchestration_agents: dict[str, OrchestrationAgent] | None = None
    session_id: uuid.UUID | None = None
    message_id: int | None = None
    request_id: str | None = None
    chat_run_id: uuid.UUID | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "allowed_kb_ids": (
                [str(k) for k in self.allowed_kb_ids]
                if self.allowed_kb_ids is not None
                else None
            ),
            "dynamic_tool_names": [tool.name for tool in self.dynamic_tools or []],
            "model_id": self.model_id,
            "model_base_url": self.model_base_url,
            "model_credential_configured": bool(self.model_api_key),
            "attachment_names": [item.get("name", "") for item in self.attachments or []],
            "call_channel": self.call_channel,
            "degrade_level": self.resolved_degrade_level,
            "outcome_deadline_active": self.outcome_deadline_monotonic is not None,
            "agent_id": str(self.agent_id) if self.agent_id else None,
            "agent_name": self.agent_name,
            "allowed_skill_names": self.allowed_skill_names,
            "allowed_tool_names": (
                sorted(self.allowed_tool_names)
                if self.allowed_tool_names is not None
                else None
            ),
            "allowed_capability_ids": [
                str(value)
                for value in sorted(self.allowed_capability_ids or set(), key=str)
            ],
            "memory_enabled": self.memory_enabled,
            "orchestration_agent_names": sorted(self.orchestration_agents or {}),
            "session_id": str(self.session_id) if self.session_id else None,
            "message_id": self.message_id,
            "request_id": self.request_id,
            "chat_run_id": str(self.chat_run_id) if self.chat_run_id else None,
        }

    @property
    def resolved_degrade_level(self) -> int:
        """Return this request's immutable D0/D1/D2 snapshot."""
        if self.degrade_level is None:
            from .llm_guard import get_degrade_level

            self.degrade_level = get_degrade_level()
        return max(0, min(2, self.degrade_level))

    @property
    def effective_rag_top_k(self) -> int:
        """Expose the D2 RAG limit without spreading degradation policy."""
        from ..config import settings

        if self.resolved_degrade_level >= 2:
            return min(settings.RAG_TOP_K, 3)
        return settings.RAG_TOP_K


_tool_context: ContextVar[ToolContext | None] = ContextVar(
    "tool_context", default=None
)


def set_tool_context(ctx: ToolContext) -> None:
    """设置当前请求的工具上下文（由路由层调用）。"""
    _ = ctx.resolved_degrade_level
    _tool_context.set(ctx)


def push_tool_context(ctx: ToolContext):
    """临时覆盖上下文并返回可恢复 token，供并行 Worker 使用。"""
    _ = ctx.resolved_degrade_level
    return _tool_context.set(ctx)


def pop_tool_context(token) -> None:
    _tool_context.reset(token)


def get_tool_context() -> ToolContext:
    """获取当前工具上下文（工具内部调用）；缺省返回空上下文。"""
    ctx = _tool_context.get()
    if ctx is None:
        ctx = ToolContext()
        _ = ctx.resolved_degrade_level
    return ctx


def reset_tool_context() -> None:
    """清空上下文（请求结束/测试隔离）。"""
    _tool_context.set(None)
