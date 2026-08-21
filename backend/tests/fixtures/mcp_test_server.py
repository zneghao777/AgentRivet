"""真实 Streamable HTTP MCP 测试 Server。"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "Harness MCP Test",
    host="127.0.0.1",
    port=int(os.environ["MCP_TEST_PORT"]),
    stateless_http=True,
    json_response=True,
)


@mcp.tool()
def echo_text(text: str) -> str:
    """Return text from the real MCP server."""
    return f"mcp:{text}"


@mcp.tool()
def add_numbers(left: int, right: int) -> int:
    """Add two integers."""
    return left + right


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
