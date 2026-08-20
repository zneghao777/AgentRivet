"""本地解析降级（KB_MANAGEMENT_SPEC §3.3）：pypdf / python-docx / txt / md。

GLM key 缺失、GLM 解析失败或文件类型不受 GLM 支持时使用。
"""

from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)


class LocalParseError(Exception):
    """本地解析异常（格式不支持 / 文件损坏等）。"""


def parse_local(filename: str, content: bytes) -> str:
    """按扩展名将文件解析为纯文本。"""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    try:
        if ext in ("md", "txt"):
            return content.decode("utf-8", errors="replace")
        if ext == "pdf":
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(content))
            pages = [page.extract_text() or "" for page in reader.pages]
            return "\n".join(pages)
        if ext == "docx":
            import docx

            doc = docx.Document(io.BytesIO(content))
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            return "\n".join(paragraphs)
        raise LocalParseError(f"不支持的文件格式 .{ext}")
    except LocalParseError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise LocalParseError(f"文件解析失败: {exc}") from exc
