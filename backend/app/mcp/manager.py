"""MCP Server 生命周期、能力快照与请求态工具解析。"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime

from langchain_core.tools import BaseTool
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..storage.models import McpCapability, McpServer
from .adapter import McpAdapterError, McpDiscovery, discover_server
from .secrets import decrypt_headers


def _now() -> datetime:
    return datetime.now(UTC)


def _slug(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_").lower()
    return normalized[:48] or "server"


def _schema_for(tool: BaseTool) -> dict:
    schema = getattr(tool, "args_schema", None)
    if schema is None:
        return {"type": "object", "properties": {}}
    if isinstance(schema, dict):
        return schema
    return schema.model_json_schema()


def _effective_name(server_name: str, remote_name: str) -> str:
    base = f"mcp_{_slug(server_name)}__{_slug(remote_name)}"
    if len(base) <= 64:
        return base
    suffix = hashlib.sha256(base.encode()).hexdigest()[:8]
    return f"{base[:55]}_{suffix}"


class McpManager:
    def __init__(self) -> None:
        self._runtime_tools: dict[tuple[str, uuid.UUID], dict[str, BaseTool]] = {}

    async def get_server(
        self, db: AsyncSession, *, tenant_id: str, server_id: uuid.UUID
    ) -> McpServer | None:
        return (
            await db.execute(
                select(McpServer).where(McpServer.id == server_id, McpServer.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()

    async def list_servers(self, db: AsyncSession, *, tenant_id: str) -> list[McpServer]:
        return list(
            (
                await db.execute(
                    select(McpServer)
                    .where(McpServer.tenant_id == tenant_id)
                    .order_by(McpServer.updated_at.desc())
                )
            )
            .scalars()
            .all()
        )

    async def list_capabilities(
        self, db: AsyncSession, *, tenant_id: str, server_id: uuid.UUID
    ) -> list[McpCapability]:
        server = await self.get_server(db, tenant_id=tenant_id, server_id=server_id)
        if server is None:
            return []
        return list(
            (
                await db.execute(
                    select(McpCapability)
                    .where(McpCapability.server_id == server_id)
                    .order_by(McpCapability.remote_name.asc())
                )
            )
            .scalars()
            .all()
        )

    async def _connect(self, server: McpServer) -> McpDiscovery:
        if server.transport != "streamable_http":
            raise McpAdapterError("MCP_CONFIG_INVALID", "当前版本仅支持 Streamable HTTP")
        return await discover_server(
            server_name=f"server_{str(server.id).replace('-', '')[:12]}",
            endpoint_url=server.endpoint_url,
            headers=decrypt_headers(server.secret_ciphertext),
        )

    async def probe(self, db: AsyncSession, server: McpServer) -> McpDiscovery:
        server.health_status = "checking"
        await db.commit()
        try:
            discovery = await self._connect(server)
        except McpAdapterError as exc:
            server.health_status = "error"
            server.last_error_code = exc.code
            server.last_error_message = str(exc)[:500]
            server.last_checked_at = _now()
            await db.commit()
            raise
        server.health_status = "ready"
        server.protocol_version = discovery.protocol_version
        server.server_info = discovery.server_info
        server.last_error_code = None
        server.last_error_message = None
        server.last_checked_at = _now()
        await db.commit()
        return discovery

    async def discover(self, db: AsyncSession, server: McpServer) -> list[McpCapability]:
        discovery = await self.probe(db, server)
        existing = {
            row.remote_name: row
            for row in await self.list_capabilities(
                db, tenant_id=server.tenant_id, server_id=server.id
            )
        }
        seen: set[str] = set()
        runtime: dict[str, BaseTool] = {}
        for tool in discovery.tools:
            remote_name = tool.name
            seen.add(remote_name)
            schema = _schema_for(tool)
            schema_hash = hashlib.sha256(
                json.dumps(schema, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            row = existing.get(remote_name)
            if row is None:
                row = McpCapability(
                    server_id=server.id,
                    remote_name=remote_name,
                    effective_name=_effective_name(server.name, remote_name),
                    description=tool.description or "",
                    input_schema=schema,
                    schema_hash=schema_hash,
                )
                db.add(row)
            else:
                if row.schema_hash != schema_hash:
                    row.enabled = False
                    row.review_status = "changed"
                row.description = tool.description or ""
                row.effective_name = _effective_name(server.name, remote_name)
                row.input_schema = schema
                row.schema_hash = schema_hash
                row.removed_at = None
            row.last_seen_at = _now()
            tool.name = row.effective_name
            runtime[remote_name] = tool
        for remote_name, row in existing.items():
            if remote_name not in seen:
                row.enabled = False
                row.review_status = "changed"
                row.removed_at = _now()
        server.capability_revision += 1
        await db.commit()
        self._runtime_tools[(server.tenant_id, server.id)] = runtime
        return await self.list_capabilities(db, tenant_id=server.tenant_id, server_id=server.id)

    async def resolve_tools(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        requested_capability_ids: set[uuid.UUID] | None = None,
    ) -> list[BaseTool]:
        if not settings.MCP_ENABLED:
            return []
        servers = list(
            (
                await db.execute(
                    select(McpServer).where(
                        McpServer.tenant_id == tenant_id,
                        McpServer.enabled.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        resolved: list[BaseTool] = []
        for server in servers:
            key = (tenant_id, server.id)
            if key not in self._runtime_tools:
                try:
                    await self.discover(db, server)
                except McpAdapterError:
                    continue
            enabled = {
                row.remote_name
                for row in await self.list_capabilities(
                    db, tenant_id=tenant_id, server_id=server.id
                )
                if row.enabled
                and row.review_status == "approved"
                and row.removed_at is None
                and (requested_capability_ids is None or row.id in requested_capability_ids)
            }
            resolved.extend(
                tool
                for remote, tool in self._runtime_tools.get(key, {}).items()
                if remote in enabled
            )
            if len(resolved) >= settings.CAPABILITY_MAX_TOOLS_PER_RUN:
                break
        return resolved[: settings.CAPABILITY_MAX_TOOLS_PER_RUN]

    async def delete_server(
        self, db: AsyncSession, *, tenant_id: str, server_id: uuid.UUID
    ) -> bool:
        server = await self.get_server(db, tenant_id=tenant_id, server_id=server_id)
        if server is None:
            return False
        await db.execute(delete(McpCapability).where(McpCapability.server_id == server_id))
        await db.delete(server)
        await db.commit()
        self._runtime_tools.pop((tenant_id, server_id), None)
        return True


mcp_manager = McpManager()
