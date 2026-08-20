"""GLM 文件解析封装（KB_MANAGEMENT_SPEC §1.1 / §3.3；智谱 files/parser 真实协议）。

异步二段式：
1. POST /files/parser/create  —— multipart 直传文件字节（无需公网 URL）
     form: file=<bytes> / tool_type=lite|expert|prime / file_type=PNG|PDF|...
     → 返回 task_id
2. GET  /files/parser/result/{taskId}/text —— 轮询结果
     → status: processing（继续轮询）/ succeeded（返回 content）/ failed（抛错）

图片（PNG/JPG/JPEG 等）走同一接口，由 GLM 侧完成 OCR（content 为识别出的文本）。

注意：与规格草稿的差异——
- create 接口为 multipart 上传文件，而非 JSON + file_url；
- 结果查询为 GET result 接口，而非 POST status；
- 返回 content（纯文本）+ parsing_result_url（Markdown zip 下载链接），无 md_results 字段。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

import httpx

from ...config import settings

logger = logging.getLogger(__name__)


class GlmParseError(Exception):
    """GLM 解析异常（任务创建失败 / 轮询超时 / 最终失败）。"""


class ParseResult:
    """一次文件解析的结果（content 为识别文本；md_results 保留字段以兼容旧调用方）。"""

    __slots__ = ("content", "md_results", "tool_type", "task_id")

    def __init__(
        self,
        *,
        content: str = "",
        md_results: str = "",
        tool_type: str = "prime",
        task_id: str = "",
    ) -> None:
        self.content = content
        self.md_results = md_results
        self.tool_type = tool_type
        self.task_id = task_id

    @property
    def text(self) -> str:
        """content 即解析文本（图片 OCR 也为该字段）。"""
        return (self.content or self.md_results or "").strip()


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


# GLM create 接口支持的 file_type 枚举（大写）
FILE_TYPES = {
    "pdf": "PDF",
    "docx": "DOCX",
    "doc": "DOC",
    "xls": "XLS",
    "xlsx": "XLSX",
    "ppt": "PPT",
    "pptx": "PPTX",
    "png": "PNG",
    "jpg": "JPG",
    "jpeg": "JPEG",
    "csv": "CSV",
    "txt": "TXT",
    "md": "MD",
    "html": "HTML",
    "bmp": "BMP",
    "gif": "GIF",
    "webp": "WEBP",
    "heic": "HEIC",
    "tiff": "TIFF",
    "heif": "HEIF",
    "jp2": "JP2",
}

# 各 tool_type 支持的扩展名子集（限制工具能力，避免接口 4xx）
TOOL_SUPPORTED = {
    "lite": {
        "pdf", "docx", "doc", "xls", "xlsx", "ppt", "pptx",
        "png", "jpg", "jpeg", "csv", "txt", "md",
    },
    "expert": {"pdf"},
    "prime": set(FILE_TYPES),
}


def file_type_for(ext: str) -> str | None:
    """按小写扩展名返回 GLM file_type 大写枚举；不支持返回 None。"""
    return FILE_TYPES.get(ext.lower())


def tool_supports(tool_type: str, ext: str) -> bool:
    """工具类型是否支持该扩展名。"""
    return ext.lower() in TOOL_SUPPORTED.get(tool_type, set())


async def create_parse_task(
    api_key: str,
    file_bytes: bytes,
    filename: str,
    tool_type: str = "prime",
    *,
    base_url: str | None = None,
) -> str:
    """multipart 上传文件创建异步解析任务，返回任务 id。

    - 无扩展名的文件尝试按 mime 推断；无法识别抛 GlmParseError。
    - tool_type 与扩展名不匹配（如 expert+PNG）时抛错，由调用方降级。
    """
    base_url = base_url or settings.GLM_FILES_BASE_URL
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    file_type = file_type_for(ext)
    if file_type is None:
        raise GlmParseError(f"GLM 不支持解析 .{ext} 文件（filename={filename}）")
    if not tool_supports(tool_type, ext):
        raise GlmParseError(
            f"GLM 工具 {tool_type} 不支持 {file_type}，请改用 prime/lite 或换文件类型"
        )

    files = {
        "file": (filename, file_bytes),
        "tool_type": (None, tool_type),
        "file_type": (None, file_type),
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{base_url}/files/parser/create",
                files=files,
                headers=_headers(api_key),
            )
    except httpx.HTTPError as exc:
        raise GlmParseError(f"GLM 创建解析任务网络错误: {exc}") from exc
    if resp.status_code != 200:
        raise GlmParseError(
            f"GLM 创建解析任务失败: HTTP {resp.status_code} {resp.text[:300]}"
        )
    body = resp.json()
    task_id = body.get("task_id")
    if not task_id or body.get("success") is False:
        raise GlmParseError(f"GLM 未返回任务 id: {body}")
    logger.info("GLM 解析任务已创建 task_id=%s file=%s tool=%s", task_id, filename, tool_type)
    return str(task_id)


async def poll_parse_status(
    api_key: str,
    task_id: str,
    *,
    base_url: str | None = None,
    format_type: str = "text",
    poll_seconds: float | None = None,
    timeout_seconds: float | None = None,
) -> ParseResult:
    """轮询 GET /files/parser/result/{taskId}/{format_type} 直至 succeeded / failed / 超时。

    format_type 可选 text | download_link（download_link 返回 parsing_result_url）。
    """
    base_url = base_url or settings.GLM_FILES_BASE_URL
    poll_seconds = (
        settings.GLM_FILES_POLL_SECONDS if poll_seconds is None else poll_seconds
    )
    timeout_seconds = (
        settings.GLM_FILES_TIMEOUT_SECONDS
        if timeout_seconds is None
        else timeout_seconds
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                resp = await client.get(
                    f"{base_url}/files/parser/result/{task_id}/{format_type}",
                    headers=_headers(api_key),
                )
            except httpx.HTTPError as exc:
                raise GlmParseError(f"GLM 查询解析状态网络错误: {exc}") from exc
            if resp.status_code != 200:
                raise GlmParseError(
                    f"GLM 查询解析状态失败: HTTP {resp.status_code} {resp.text[:300]}"
                )
            body = resp.json()
            status = str(body.get("status", "")).lower()
            if status == "succeeded":
                logger.info("GLM 解析完成 task_id=%s", task_id)
                return ParseResult(
                    content=str(body.get("content") or ""),
                    md_results=str(body.get("md_results") or ""),
                    tool_type=settings.GLM_FILES_TOOL,
                    task_id=task_id,
                )
            if status == "failed":
                raise GlmParseError(
                    f"GLM 解析失败: {body.get('message') or body.get('error') or '未知错误'}"
                )
            # processing / pending / 未知状态均继续轮询
            if loop.time() + poll_seconds > deadline:
                raise GlmParseError(
                    f"GLM 解析超时（>{int(timeout_seconds)}s），任务 {task_id}"
                )
            await asyncio.sleep(poll_seconds)


async def parse_file(
    api_key: str,
    file_bytes: bytes,
    filename: str,
    tool_type: str | None = None,
    *,
    base_url: str | None = None,
    poll_seconds: float | None = None,
    timeout_seconds: float | None = None,
) -> ParseResult:
    """创建 + 轮询的完整异步解析流程（multipart 直传文件）。"""
    tool_type = tool_type or settings.GLM_FILES_TOOL
    task_id = await create_parse_task(
        api_key, file_bytes, filename, tool_type, base_url=base_url
    )
    return await poll_parse_status(
        api_key,
        task_id,
        base_url=base_url,
        poll_seconds=poll_seconds,
        timeout_seconds=timeout_seconds,
    )
