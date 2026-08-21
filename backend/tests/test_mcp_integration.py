"""真实 MCP 协议、管理 API 与租户隔离测试。"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.mcp.adapter import discover_server

AUTH = {"X-API-Key": "test-key-1", "X-Tenant-Id": "mcp-tenant-a"}
OTHER_AUTH = {"X-API-Key": "test-key-1", "X-Tenant-Id": "mcp-tenant-b"}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def real_mcp_endpoint():
    port = _free_port()
    env = {**os.environ, "MCP_TEST_PORT": str(port)}
    script = Path(__file__).parent / "fixtures" / "mcp_test_server.py"
    process = subprocess.Popen(
        [sys.executable, str(script)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read().decode() if process.stderr else ""
            raise RuntimeError(f"MCP test server exited: {stderr}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        process.terminate()
        raise RuntimeError("MCP test server did not start")
    yield f"http://127.0.0.1:{port}/mcp"
    process.terminate()
    process.wait(timeout=5)


@pytest.mark.asyncio
async def test_real_streamable_http_discovery_and_call(real_mcp_endpoint):
    discovery = await discover_server(server_name="real_test", endpoint_url=real_mcp_endpoint)
    tools = {tool.name: tool for tool in discovery.tools}
    assert set(tools) == {"echo_text", "add_numbers"}
    echo = await tools["echo_text"].ainvoke({"text": "hello"})
    total = await tools["add_numbers"].ainvoke({"left": 2, "right": 5})
    assert echo[0]["text"] == "mcp:hello"
    assert total[0]["text"] == "7"
    assert discovery.server_info["name"] == "Harness MCP Test"


@pytest.mark.asyncio
async def test_mcp_api_lifecycle_and_tenant_isolation(app_client, db_session, real_mcp_endpoint):
    existing = await app_client.get("/api/v1/mcp/servers", headers=AUTH)
    for item in existing.json()["data"]:
        if item["name"] == "real-integration":
            await app_client.delete(f"/api/v1/mcp/servers/{item['id']}", headers=AUTH)
    create = await app_client.post(
        "/api/v1/mcp/servers",
        headers=AUTH,
        json={
            "name": "real-integration",
            "endpoint_url": real_mcp_endpoint,
            "transport": "streamable_http",
            "auth": {"type": "none"},
        },
    )
    assert create.status_code == 200, create.text
    server = create.json()["data"]
    assert server["enabled"] is False
    assert server["credential_configured"] is False

    tested = await app_client.post(f"/api/v1/mcp/servers/{server['id']}/test", headers=AUTH)
    assert tested.status_code == 200, tested.text
    tools = tested.json()["data"]["capabilities"]
    assert {tool["remote_name"] for tool in tools} == {"echo_text", "add_numbers"}
    assert all(tool["enabled"] is False for tool in tools)
    assert all(tool["effective_name"].startswith("mcp_real_integration__") for tool in tools)

    reviewed = await app_client.patch(
        f"/api/v1/mcp/servers/{server['id']}/capabilities/{tools[0]['id']}",
        headers=AUTH,
        json={"enabled": True, "approval_policy": "never"},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["data"]["review_status"] == "approved"

    enabled = await app_client.post(f"/api/v1/mcp/servers/{server['id']}/enable", headers=AUTH)
    assert enabled.status_code == 200
    assert enabled.json()["data"]["enabled"] is True

    from app.config import settings
    from app.mcp.manager import mcp_manager

    before = settings.MCP_ENABLED
    settings.MCP_ENABLED = True
    try:
        runtime_tools = await mcp_manager.resolve_tools(db_session, tenant_id="mcp-tenant-a")
        assert len(runtime_tools) == 1
        assert runtime_tools[0].name.startswith("mcp_real_integration__")
        result = await runtime_tools[0].ainvoke(
            {"text": "runtime"}
            if runtime_tools[0].name.endswith("echo_text")
            else {"left": 1, "right": 2}
        )
        assert result
    finally:
        settings.MCP_ENABLED = before

    hidden = await app_client.get("/api/v1/mcp/servers", headers=OTHER_AUTH)
    assert hidden.status_code == 200
    assert all(item["id"] != server["id"] for item in hidden.json()["data"])

    removed = await app_client.delete(f"/api/v1/mcp/servers/{server['id']}", headers=AUTH)
    assert removed.status_code == 200
