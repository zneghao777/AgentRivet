"""AgentHub 定义层 DTO。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentDefinition:
    id: uuid.UUID
    tenant_id: str
    name: str
    description: str
    system_prompt: str | None
    model: str | None
    model_base_url: str | None
    memory_enabled: bool
    scope: str
    status: str


@dataclass
class ResolvedAgentCapabilities:
    """一次请求最终可用的 Agent 能力集合。"""

    agent_id: uuid.UUID | None = None
    agent_name: str | None = None
    status: str | None = None
    system_prompt_block: str | None = None
    allowed_skill_names: list[str] | None = None
    allowed_tool_names: set[str] | None = None
    allowed_capability_ids: set[uuid.UUID] | None = None
    allowed_kb_ids: list[uuid.UUID] | None = None
    model: str | None = None
    model_base_url: str | None = None
    memory_enabled: bool = True
    warnings: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.agent_id is not None and self.status == "active"


@dataclass
class OrchestrationAgent:
    """一次请求中可被 Router/Worker 使用的 Agent 快照。"""

    agent_id: uuid.UUID
    tenant_id: str
    name: str
    description: str
    scope: str
    system_prompt: str | None
    model: str | None
    model_base_url: str | None
    memory_enabled: bool
    allowed_skill_names: list[str] = field(default_factory=list)
    allowed_tool_names: set[str] | None = None
    allowed_capability_ids: set[uuid.UUID] = field(default_factory=set)
    allowed_kb_ids: list[uuid.UUID] | None = None
    dynamic_tools: list[Any] = field(default_factory=list)
    capability_summary: str = ""
