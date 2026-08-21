"""LangChain MCP 适配器：单 Server、Streamable HTTP、故障隔离。"""

from __future__ import annotations

import asyncio
import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from ..config import settings


class McpAdapterError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class McpDiscovery:
    tools: list[BaseTool]
    server_info: dict
    protocol_version: str | None


def validate_endpoint(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise McpAdapterError("MCP_CONFIG_INVALID", "Endpoint 必须是有效 HTTP(S) URL")
    if parsed.username or parsed.password:
        raise McpAdapterError("MCP_ENDPOINT_BLOCKED", "Endpoint 不得包含用户名或密码")
    if settings.APP_ENV == "prod" and parsed.scheme != "https":
        raise McpAdapterError("MCP_ENDPOINT_BLOCKED", "生产环境 MCP Endpoint 必须使用 HTTPS")
    allowed = {
        item.strip().lower() for item in settings.MCP_ALLOWED_HOSTS.split(",") if item.strip()
    }
    if allowed and parsed.hostname.lower() not in allowed:
        raise McpAdapterError("MCP_ENDPOINT_BLOCKED", "Endpoint 主机不在 MCP allowlist")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return
    if settings.APP_ENV == "prod" and (
        address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
    ):
        raise McpAdapterError("MCP_ENDPOINT_BLOCKED", "Endpoint 地址不符合生产出站策略")


async def discover_server(
    *, server_name: str, endpoint_url: str, headers: dict[str, str] | None = None
) -> McpDiscovery:
    validate_endpoint(endpoint_url)
    connection = {
        "transport": "streamable_http",
        "url": endpoint_url,
        "headers": headers or None,
        "timeout": settings.MCP_CONNECT_TIMEOUT_SECONDS,
        "sse_read_timeout": settings.MCP_DISCOVERY_TIMEOUT_SECONDS,
    }
    client = MultiServerMCPClient({server_name: connection}, handle_tool_errors=True)
    try:
        info_map, tools = await asyncio.wait_for(
            asyncio.gather(
                client.get_server_info(server_name=server_name),
                client.get_tools(server_name=server_name),
            ),
            timeout=settings.MCP_DISCOVERY_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise McpAdapterError("MCP_CONNECT_TIMEOUT", "MCP 连接或能力发现超时") from exc
    except Exception as exc:  # SDK transport/protocol errors are normalized for API/UI
        raise McpAdapterError("MCP_DISCOVERY_FAILED", str(exc)[:500]) from exc
    info = info_map.get(server_name)
    if info is None:
        return McpDiscovery(tools=tools, server_info={}, protocol_version=None)
    server_info = info.serverInfo.model_dump(mode="json") if info.serverInfo else {}
    return McpDiscovery(
        tools=tools,
        server_info=server_info,
        protocol_version=getattr(info, "protocolVersion", None),
    )
