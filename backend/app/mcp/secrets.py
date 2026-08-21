"""MCP 认证信息加密；API 永不返回解密后的值。"""

from __future__ import annotations

import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken

from ..config import settings


class McpSecretError(ValueError):
    pass


def _fernet() -> Fernet:
    raw = settings.MCP_SECRET_KEY.strip()
    if raw:
        try:
            return Fernet(raw.encode())
        except ValueError as exc:
            raise McpSecretError("MCP_SECRET_KEY 必须是有效 Fernet key") from exc
    if settings.APP_ENV == "prod":
        raise McpSecretError("生产环境配置 MCP 认证前必须设置 MCP_SECRET_KEY")
    # 开发/测试环境仍使用稳定密文，避免把认证值以明文写入数据库。
    seed = f"{settings.DEFAULT_TENANT}:{settings.API_KEYS}:mcp-dev-key".encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(seed).digest()))


def encrypt_headers(headers: dict[str, str] | None) -> str | None:
    if not headers:
        return None
    payload = json.dumps(headers, ensure_ascii=False, sort_keys=True).encode()
    return _fernet().encrypt(payload).decode()


def decrypt_headers(ciphertext: str | None) -> dict[str, str]:
    if not ciphertext:
        return {}
    try:
        value = json.loads(_fernet().decrypt(ciphertext.encode()).decode())
    except (InvalidToken, ValueError, json.JSONDecodeError) as exc:
        raise McpSecretError("MCP 凭据无法解密，请重新配置") from exc
    if not isinstance(value, dict):
        raise McpSecretError("MCP 凭据格式无效")
    return {str(key): str(item) for key, item in value.items()}
