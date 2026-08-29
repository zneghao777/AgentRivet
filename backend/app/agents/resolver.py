"""AgentHub 统一能力解析器。"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..storage import repositories as repo
from ..storage.models import (
    Agent,
    AgentKbBinding,
    AgentMcpBinding,
    AgentSkillBinding,
    AgentToolBinding,
    KnowledgeBase,
    McpCapability,
    McpServer,
    Skill,
)
from .manager import sanitize_system_prompt
from .models import OrchestrationAgent, ResolvedAgentCapabilities

logger = logging.getLogger(__name__)


async def resolve_agent_capabilities(
    db: AsyncSession, *, tenant_id: str, agent_id: uuid.UUID | None
) -> ResolvedAgentCapabilities:
    if agent_id is None:
        return ResolvedAgentCapabilities()

    agent = await repo.agent_repository.get(db, tenant_id=tenant_id, agent_id=agent_id)
    if agent is None or agent.status == "archived":
        logger.warning("Agent 解析降级：不存在或已归档 agent_id=%s", agent_id)
        return ResolvedAgentCapabilities(warnings=["Agent 不存在或已归档，已使用默认助手"])
    if agent.status == "disabled":
        logger.warning("Agent 解析降级：已停用 agent_id=%s", agent_id)
        return ResolvedAgentCapabilities(
            status="disabled", warnings=["Agent 已停用，已使用默认助手"]
        )

    skill_names = list(
        (
            await db.execute(
                select(Skill.name)
                .join(AgentSkillBinding, AgentSkillBinding.skill_id == Skill.id)
                .where(
                    AgentSkillBinding.agent_id == agent.id,
                    Skill.enabled.is_(True),
                )
                .order_by(Skill.name.asc())
            )
        ).scalars().all()
    )
    tool_names = set(
        (
            await db.execute(
                select(AgentToolBinding.tool_name).where(
                    AgentToolBinding.agent_id == agent.id
                )
            )
        ).scalars().all()
    )
    mcp_ids = set(
        (
            await db.execute(
                select(McpCapability.id)
                .join(AgentMcpBinding, AgentMcpBinding.capability_id == McpCapability.id)
                .join(McpServer, McpServer.id == McpCapability.server_id)
                .where(
                    AgentMcpBinding.agent_id == agent.id,
                    McpServer.tenant_id == tenant_id,
                    McpServer.enabled.is_(True),
                    McpServer.health_status.in_(("ready", "degraded")),
                    McpCapability.enabled.is_(True),
                    McpCapability.review_status == "approved",
                    McpCapability.removed_at.is_(None),
                )
            )
        ).scalars().all()
    )
    kb_ids = list(
        (
            await db.execute(
                select(KnowledgeBase.id)
                .join(AgentKbBinding, AgentKbBinding.kb_id == KnowledgeBase.id)
                .where(
                    AgentKbBinding.agent_id == agent.id,
                    KnowledgeBase.tenant_id == tenant_id,
                    KnowledgeBase.status == "active",
                )
            )
        ).scalars().all()
    )
    return ResolvedAgentCapabilities(
        agent_id=agent.id,
        agent_name=agent.name,
        status=agent.status,
        system_prompt_block=sanitize_system_prompt(agent.system_prompt),
        allowed_skill_names=skill_names,
        allowed_tool_names=tool_names or None,
        allowed_capability_ids=mcp_ids,
        allowed_kb_ids=kb_ids or None,
        model=agent.model,
        model_base_url=agent.model_base_url,
        memory_enabled=agent.memory_enabled,
    )


async def resolve_orchestration_agents(
    db: AsyncSession, *, tenant_id: str
) -> dict[str, OrchestrationAgent]:
    """解析当前租户全部 active Agent，供 Router 和 Worker 请求态使用。"""
    from ..mcp.manager import mcp_manager

    rows = list(
        (
            await db.execute(
                select(Agent)
                .where(Agent.tenant_id == tenant_id, Agent.status == "active")
                .order_by(Agent.name.asc())
            )
        ).scalars().all()
    )
    result: dict[str, OrchestrationAgent] = {}
    for row in rows:
        resolved = await resolve_agent_capabilities(
            db, tenant_id=tenant_id, agent_id=row.id
        )
        skill_names = resolved.allowed_skill_names or []
        tool_names = sorted(resolved.allowed_tool_names or set())
        mcp_names = list(
            (
                await db.execute(
                    select(McpCapability.remote_name)
                    .join(
                        AgentMcpBinding,
                        AgentMcpBinding.capability_id == McpCapability.id,
                    )
                    .where(AgentMcpBinding.agent_id == row.id)
                    .order_by(McpCapability.remote_name.asc())
                )
            ).scalars().all()
        )
        kb_names = list(
            (
                await db.execute(
                    select(KnowledgeBase.name)
                    .join(AgentKbBinding, AgentKbBinding.kb_id == KnowledgeBase.id)
                    .where(AgentKbBinding.agent_id == row.id)
                    .order_by(KnowledgeBase.name.asc())
                )
            ).scalars().all()
        )
        capability_ids = resolved.allowed_capability_ids or set()
        dynamic_tools = await mcp_manager.resolve_tools(
            db,
            tenant_id=tenant_id,
            requested_capability_ids=capability_ids,
        )
        parts = []
        if skill_names:
            parts.append(f"Skills: {_preview_names(skill_names)}")
        if tool_names:
            parts.append(f"Tools: {_preview_names(tool_names)}")
        if mcp_names:
            parts.append(f"MCP: {_preview_names(mcp_names)}")
        if kb_names:
            parts.append(f"KB: {_preview_names(kb_names)}")
        result[row.name] = OrchestrationAgent(
            agent_id=row.id,
            tenant_id=tenant_id,
            name=row.name,
            description=row.description,
            scope=row.scope,
            system_prompt=resolved.system_prompt_block,
            model=resolved.model,
            model_base_url=resolved.model_base_url,
            memory_enabled=resolved.memory_enabled,
            allowed_skill_names=skill_names,
            allowed_tool_names=resolved.allowed_tool_names,
            allowed_capability_ids=capability_ids,
            allowed_kb_ids=resolved.allowed_kb_ids,
            dynamic_tools=dynamic_tools,
            capability_summary=" | ".join(parts) or "未绑定额外能力",
        )
    return result


def _preview_names(values: list[str], limit: int = 3) -> str:
    preview = ", ".join(values[:limit])
    return f"{preview}, …" if len(values) > limit else preview
