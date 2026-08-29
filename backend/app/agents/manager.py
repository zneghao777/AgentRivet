"""AgentHub 定义管理：校验、CRUD 与绑定全量替换。"""

from __future__ import annotations

import re
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..storage import repositories as repo
from ..storage.models import Agent

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class AgentManagerError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def sanitize_system_prompt(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _CONTROL_CHARS.sub("", value).strip()
    if len(cleaned) > settings.AGENT_SYSTEM_PROMPT_MAX_CHARS:
        raise AgentManagerError("PROMPT_TOO_LONG", "Agent 系统提示词超过长度上限")
    return cleaned or None


def validate_agent_base_url(value: str | None) -> str | None:
    if not value:
        return None
    from .model_endpoint import ModelEndpointError, validate_model_base_url

    try:
        return validate_model_base_url(value)
    except ModelEndpointError as exc:
        raise AgentManagerError("MODEL_BASE_URL_INVALID", str(exc)) from exc


class AgentManager:
    async def create(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        created_by: str | None,
        values: dict,
    ) -> Agent:
        if await repo.agent_repository.count_by_tenant(db, tenant_id) >= settings.AGENT_MAX_PER_TENANT:
            raise AgentManagerError("AGENT_LIMIT_REACHED", "已达到租户 Agent 数量上限")
        values = dict(values)
        values["name"] = values["name"].strip()
        values["description"] = values["description"].strip()
        values["system_prompt"] = sanitize_system_prompt(values.get("system_prompt"))
        values["model_base_url"] = validate_agent_base_url(values.get("model_base_url"))
        row = Agent(tenant_id=tenant_id, created_by=created_by, **values)
        try:
            return await repo.agent_repository.create(db, row)
        except IntegrityError as exc:
            raise AgentManagerError("AGENT_NAME_CONFLICT", "Agent 名称已存在") from exc

    async def update(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        agent_id: uuid.UUID,
        values: dict,
    ) -> Agent | None:
        values = dict(values)
        if "name" in values:
            values["name"] = values["name"].strip()
        if "description" in values:
            values["description"] = values["description"].strip()
        if "system_prompt" in values:
            values["system_prompt"] = sanitize_system_prompt(values["system_prompt"])
        if "model_base_url" in values:
            values["model_base_url"] = validate_agent_base_url(values["model_base_url"])
        try:
            return await repo.agent_repository.update(
                db, tenant_id=tenant_id, agent_id=agent_id, values=values
            )
        except IntegrityError as exc:
            raise AgentManagerError("AGENT_NAME_CONFLICT", "Agent 名称已存在") from exc

    async def require_active_user_agent(
        self, db: AsyncSession, *, tenant_id: str, agent_id: uuid.UUID
    ) -> Agent:
        row = await repo.agent_repository.get(db, tenant_id=tenant_id, agent_id=agent_id)
        if row is None:
            raise AgentManagerError("AGENT_NOT_FOUND", "Agent 不存在")
        if row.status != "active" or row.scope != "user":
            raise AgentManagerError("AGENT_NOT_SELECTABLE", "该 Agent 当前不可用于会话")
        return row


agent_manager = AgentManager()
