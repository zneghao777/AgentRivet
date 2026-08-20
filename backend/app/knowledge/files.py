"""上传文件落盘与读取（KB_MANAGEMENT_SPEC §3.3）。

存储路径：KB_UPLOAD_DIR/{tenant}/{kb_id}/{uuid}.{ext}
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from ..config import settings

logger = logging.getLogger(__name__)


def _upload_root() -> Path:
    return Path(settings.KB_UPLOAD_DIR)


def save_upload(tenant_id: str, kb_id, ext: str, content: bytes) -> str:
    """保存上传文件，返回相对路径（data/uploads/...）。"""
    rel_dir = Path(tenant_id) / str(kb_id)
    target_dir = _upload_root() / rel_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    rel_path = rel_dir / f"{uuid.uuid4().hex}.{ext}"
    (_upload_root() / rel_path).write_bytes(content)
    return str(rel_path)


def read_upload(rel_path: str) -> bytes:
    """按相对路径读取上传文件字节。"""
    return (_upload_root() / rel_path).read_bytes()


def remove_upload(rel_path: str) -> None:
    """安全删除上传文件（不存在时静默）。"""
    try:
        (_upload_root() / rel_path).unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("删除上传文件失败 %s: %s", rel_path, exc)


def build_file_url(rel_path: str) -> str:
    """构建文件对外访问 URL（预留：GLM 现为 multipart 直传，无需公网 URL）。

    - KB_PUBLIC_BASE_URL 配置时：{base}/{rel_path}。
    - 未配置时：相对路径。
    """
    if settings.KB_PUBLIC_BASE_URL:
        base = settings.KB_PUBLIC_BASE_URL.rstrip("/")
        return f"{base}/{rel_path}"
    return f"/uploads/{rel_path}"
