"""MCP 工具适配器（可选，预留）。

第一版允许留空实现 + 接口占位。后续基于 langchain-mcp-adapters
将任意 MCP Server 的 tools 适配进注册中心。
"""

from __future__ import annotations

from .registry import ToolRegistry


async def register_mcp_tools(registry: ToolRegistry, *, server_url: str) -> int:
    """占位：从 MCP Server 拉取并注册工具。

    当前版本返回 0，不执行任何操作。实现后应返回注册的工具数量。
    """
    # from langchain_mcp_adapters.client import MultiServerMCPClient
    # client = MultiServerMCPClient({...})
    # tools = await client.get_tools()
    # for t in tools: registry.register(t, source="mcp")
    _ = server_url
    return 0
