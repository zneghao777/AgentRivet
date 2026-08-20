"""GLM 文件解析封装单元测试（mock HTTP，无真实外部依赖）。

真实协议（智谱 files/parser）：
- create: POST multipart（file 字节 / tool_type / file_type）→ task_id
- poll:   GET /files/parser/result/{taskId}/text → status: processing/succeeded/failed
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.knowledge.parser.glm_parser import (
    FILE_TYPES,
    GlmParseError,
    ParseResult,
    create_parse_task,
    file_type_for,
    parse_file,
    poll_parse_status,
    tool_supports,
)

B64 = b"fake-file-bytes"


class FakeTransport(httpx.AsyncBaseTransport):
    """可编程 mock transport：按 URL 后缀返回预置响应，或调用 handler。"""

    def __init__(self, responses: dict[str, dict] | None = None, handler=None):
        self.responses = responses or {}
        self.handler = handler  # 可选：callable(request) -> dict
        self.calls: list[tuple[str, str, bytes]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # multipart 为流式 body，需先读取再记录
        content = await request.aread()
        self.calls.append((str(request.url), request.method, content))
        if self.handler is not None:
            return httpx.Response(200, json=self.handler(request))
        for key, payload in self.responses.items():
            if key in str(request.url):
                return httpx.Response(200, json=payload)
        return httpx.Response(500, json={"error": "unexpected"})


@pytest.fixture
def monkeypatch_client(monkeypatch):
    """将 httpx.AsyncClient 替换为 FakeTransport 客户端。"""
    original = httpx.AsyncClient

    def _install(
        responses: dict[str, dict] | None = None, handler=None
    ) -> FakeTransport:
        transport = FakeTransport(responses, handler)

        def _factory(**kwargs):
            return original(transport=transport)

        monkeypatch.setattr(httpx, "AsyncClient", _factory)
        return transport

    return _install


def test_file_type_mapping():
    """扩展名 → GLM file_type 大写枚举。"""
    assert file_type_for("png") == "PNG"
    assert file_type_for("JPG") == "JPG"
    assert file_type_for("jpeg") == "JPEG"
    assert file_type_for("pdf") == "PDF"
    assert file_type_for("md") == "MD"
    assert file_type_for("exe") is None
    assert "png" in FILE_TYPES


def test_tool_support_matrix():
    """expert 仅 pdf；lite 支持图片；prime 支持全部。"""
    assert tool_supports("expert", "pdf")
    assert not tool_supports("expert", "png")
    assert tool_supports("lite", "png")
    assert tool_supports("prime", "png")
    assert tool_supports("prime", "tiff")
    assert not tool_supports("lite", "tiff")


async def test_create_parse_task_returns_id(monkeypatch_client):
    """multipart 上传创建任务成功返回 task_id。"""
    transport = monkeypatch_client({"create": {"success": True, "task_id": "task_123"}})
    task_id = await create_parse_task("key-1", B64, "scan.png", "prime")
    assert task_id == "task_123"
    # 校验 multipart 内容包含文件字段与类型
    _, _, body = transport.calls[0]
    assert b"scan.png" in body
    assert b"name=\"file\"" in body
    assert b"tool_type" in body
    assert b"prime" in body
    assert b"file_type" in body
    assert b"PNG" in body


async def test_create_parse_task_infers_file_type(monkeypatch_client):
    """未传 tool 时按配置 prime，multipart 含 file_type。"""
    transport = monkeypatch_client({"create": {"success": True, "task_id": "t1"}})
    await create_parse_task("key-1", B64, "doc.pdf")
    _, _, body = transport.calls[0]
    assert b"PDF" in body


async def test_create_parse_task_unsupported_ext(monkeypatch_client):
    """GLM 不支持 .exe → GlmParseError。"""
    monkeypatch_client({})
    with pytest.raises(GlmParseError, match="不支持"):
        await create_parse_task("key-1", B64, "evil.exe")


async def test_create_parse_task_tool_mismatch(monkeypatch_client):
    """expert 工具不支持 PNG → GlmParseError（提示换工具）。"""
    monkeypatch_client({})
    with pytest.raises(GlmParseError, match="不支持"):
        await create_parse_task("key-1", B64, "scan.png", "expert")


async def test_create_parse_task_http_error(monkeypatch_client):
    """HTTP 非 200 抛 GlmParseError。"""
    monkeypatch_client({})
    with pytest.raises(GlmParseError):
        await create_parse_task("key-1", B64, "a.pdf")


async def test_create_parse_task_network_error(monkeypatch):
    """网络异常抛 GlmParseError。"""
    original = httpx.AsyncClient

    class Boom(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

    def _factory(**kwargs):
        return original(transport=Boom())

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    with pytest.raises(GlmParseError):
        await create_parse_task("key-1", B64, "a.pdf")


async def test_poll_success_returns_content(monkeypatch_client):
    """GET result 返回 succeeded + content（OCR 文本）。"""
    transport = monkeypatch_client(
        {"result": {"status": "succeeded", "content": "员工手册\n休假制度", "task_id": "t1"}}
    )
    result = await poll_parse_status("key-1", "t1")
    assert isinstance(result, ParseResult)
    assert result.text == "员工手册\n休假制度"
    # 请求方法是 GET，URL 含 /result/t1/text
    url, method, _ = transport.calls[0]
    assert method == "GET"
    assert "/files/parser/result/t1/text" in url


async def test_poll_in_progress_then_success(monkeypatch_client):
    """processing → 轮询 → succeeded。"""
    states = iter(
        [
            {"status": "processing", "message": "解析中"},
            {"status": "processing", "message": "解析中"},
            {"status": "succeeded", "content": "# 完成", "task_id": "t1"},
        ]
    )
    transport = monkeypatch_client(handler=lambda _request: next(states))
    result = await poll_parse_status("key-1", "t1", poll_seconds=0)
    assert result.text == "# 完成"
    assert len(transport.calls) == 3


async def test_poll_failed_raises(monkeypatch_client):
    """failed 抛 GlmParseError。"""
    monkeypatch_client({"result": {"status": "failed", "message": "file corrupt"}})
    with pytest.raises(GlmParseError, match="file corrupt"):
        await poll_parse_status("key-1", "t1")


async def test_poll_timeout_raises(monkeypatch_client):
    """轮询超时抛 GlmParseError。"""
    monkeypatch_client({"result": {"status": "processing"}})
    with pytest.raises(GlmParseError, match="超时"):
        await poll_parse_status("key-1", "t1", poll_seconds=0, timeout_seconds=0)


async def test_parse_file_full_flow(monkeypatch_client):
    """create → poll 完整流程（图片 OCR 场景）。"""
    transport = monkeypatch_client(
        {
            "create": {"success": True, "task_id": "t9"},
            "result": {"status": "succeeded", "content": "扫描件文字内容", "task_id": "t9"},
        }
    )
    result = await parse_file("key-1", B64, "scan.png", "prime")
    assert result.text == "扫描件文字内容"
    assert result.task_id == "t9"
    assert result.tool_type == "prime"
    assert len(transport.calls) == 2
    # create 为 POST，result 为 GET
    assert transport.calls[0][1] == "POST"
    assert transport.calls[1][1] == "GET"
