"""聊天附件的安全落盘、读取与模型上下文准备。"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path

from ..config import settings
from ..knowledge.parser.local_parser import LocalParseError, parse_local

IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp"}
TEXT_EXTS = {
    "txt",
    "md",
    "csv",
    "json",
    "log",
    "py",
    "js",
    "jsx",
    "ts",
    "tsx",
    "html",
    "css",
    "yaml",
    "yml",
    "xml",
}
DOCUMENT_EXTS = TEXT_EXTS | {"pdf", "docx"}
ALLOWED_EXTS = IMAGE_EXTS | DOCUMENT_EXTS
MAX_FILES_PER_MESSAGE = 6
MAX_EXTRACTED_CHARS_PER_FILE = 40_000
MAX_EXTRACTED_CHARS_TOTAL = 100_000

_SAFE_FILENAME_RE = re.compile(r"[^\w.()\-\u4e00-\u9fff]+", re.UNICODE)


class ChatAttachmentError(ValueError):
    """附件校验、读取或解析失败。"""


def _root() -> Path:
    return Path(settings.KB_UPLOAD_DIR) / "chat"


def _scope_dir(tenant_id: str, session_id: uuid.UUID) -> Path:
    tenant_scope = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:16]
    return _root() / tenant_scope / str(session_id)


def _safe_name(filename: str) -> str:
    leaf = Path(filename or "attachment").name.strip() or "attachment"
    cleaned = _SAFE_FILENAME_RE.sub("_", leaf)
    return cleaned[:160] or "attachment"


def _extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _image_mime(ext: str, content: bytes) -> str:
    signatures = {
        "png": (b"\x89PNG\r\n\x1a\n", "image/png"),
        "jpg": (b"\xff\xd8\xff", "image/jpeg"),
        "jpeg": (b"\xff\xd8\xff", "image/jpeg"),
        "gif": (b"GIF8", "image/gif"),
        "webp": (b"RIFF", "image/webp"),
    }
    signature, mime_type = signatures[ext]
    if not content.startswith(signature):
        raise ChatAttachmentError("图片内容与文件扩展名不匹配")
    if ext == "webp" and content[8:12] != b"WEBP":
        raise ChatAttachmentError("图片内容与文件扩展名不匹配")
    return mime_type


def save_attachment(
    tenant_id: str,
    session_id: uuid.UUID,
    filename: str,
    content: bytes,
) -> dict:
    """校验并保存附件，返回可公开给前端的元数据。"""
    safe_name = _safe_name(filename)
    ext = _extension(safe_name)
    if ext not in ALLOWED_EXTS:
        allowed = ", ".join(f".{item}" for item in sorted(ALLOWED_EXTS))
        raise ChatAttachmentError(f"不支持的附件格式 .{ext or '(无扩展名)'}（允许: {allowed}）")
    if not content:
        raise ChatAttachmentError("附件内容为空")
    max_bytes = settings.CHAT_ATTACHMENT_MAX_SIZE_MB * 1024 * 1024
    if len(content) > max_bytes:
        raise ChatAttachmentError(
            f"单个附件不能超过 {settings.CHAT_ATTACHMENT_MAX_SIZE_MB}MB"
        )

    kind = "image" if ext in IMAGE_EXTS else "document"
    mime_type = _image_mime(ext, content) if kind == "image" else _document_mime(ext)
    attachment_id = uuid.uuid4().hex
    directory = _scope_dir(tenant_id, session_id)
    directory.mkdir(parents=True, exist_ok=True)
    content_path = directory / f"{attachment_id}.bin"
    meta_path = directory / f"{attachment_id}.json"
    content_path.write_bytes(content)
    metadata = {
        "id": attachment_id,
        "name": safe_name,
        "mime_type": mime_type,
        "size": len(content),
        "kind": kind,
        "extension": ext,
        "tenant_id": tenant_id,
        "session_id": str(session_id),
    }
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    return {key: metadata[key] for key in ("id", "name", "mime_type", "size", "kind")}


def _document_mime(ext: str) -> str:
    return {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "json": "application/json",
        "csv": "text/csv",
        "md": "text/markdown",
    }.get(ext, "text/plain")


def _load_metadata(tenant_id: str, session_id: uuid.UUID, attachment_id: str) -> tuple[dict, Path]:
    try:
        normalized_id = uuid.UUID(attachment_id).hex
    except ValueError as exc:
        raise ChatAttachmentError("附件 ID 无效") from exc
    directory = _scope_dir(tenant_id, session_id)
    meta_path = directory / f"{normalized_id}.json"
    content_path = directory / f"{normalized_id}.bin"
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ChatAttachmentError("附件不存在或已失效") from exc
    if (
        metadata.get("tenant_id") != tenant_id
        or metadata.get("session_id") != str(session_id)
        or metadata.get("id") != normalized_id
        or not content_path.is_file()
    ):
        raise ChatAttachmentError("附件不存在或无权访问")
    return metadata, content_path


def delete_attachment(tenant_id: str, session_id: uuid.UUID, attachment_id: str) -> None:
    """删除当前租户、会话范围内的附件。"""
    metadata, content_path = _load_metadata(tenant_id, session_id, attachment_id)
    content_path.unlink(missing_ok=True)
    (_scope_dir(tenant_id, session_id) / f"{metadata['id']}.json").unlink(missing_ok=True)


def prepare_attachments(
    tenant_id: str,
    session_id: uuid.UUID,
    attachment_ids: list[str] | None,
) -> list[dict]:
    """将附件转成仅驻留本次请求上下文的模型输入。"""
    ids = list(dict.fromkeys(attachment_ids or []))
    if len(ids) > MAX_FILES_PER_MESSAGE:
        raise ChatAttachmentError(f"每条消息最多添加 {MAX_FILES_PER_MESSAGE} 个附件")

    prepared: list[dict] = []
    extracted_total = 0
    for attachment_id in ids:
        metadata, content_path = _load_metadata(tenant_id, session_id, attachment_id)
        content = content_path.read_bytes()
        if metadata["kind"] == "image":
            import base64

            prepared.append(
                {
                    "id": metadata["id"],
                    "name": metadata["name"],
                    "kind": "image",
                    "mime_type": metadata["mime_type"],
                    "data_url": (
                        f"data:{metadata['mime_type']};base64,"
                        f"{base64.b64encode(content).decode('ascii')}"
                    ),
                }
            )
            continue

        remaining = MAX_EXTRACTED_CHARS_TOTAL - extracted_total
        if remaining <= 0:
            raise ChatAttachmentError("附件解析后的文本总量超过限制，请减少文件数量或大小")
        text = _extract_text(metadata["name"], metadata["extension"], content)
        text = text[: min(MAX_EXTRACTED_CHARS_PER_FILE, remaining)].strip()
        if not text:
            raise ChatAttachmentError(f"附件 {metadata['name']} 未解析出可用文本")
        extracted_total += len(text)
        prepared.append(
            {
                "id": metadata["id"],
                "name": metadata["name"],
                "kind": "document",
                "mime_type": metadata["mime_type"],
                "text": text,
            }
        )
    return prepared


def _extract_text(filename: str, ext: str, content: bytes) -> str:
    if ext in TEXT_EXTS:
        return content.decode("utf-8", errors="replace")
    try:
        return parse_local(filename, content)
    except LocalParseError as exc:
        raise ChatAttachmentError(str(exc)) from exc

