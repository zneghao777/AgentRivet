"""请求级模型端点校验与生产环境出站限制。"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from ..config import settings


class ModelEndpointError(ValueError):
    pass


def validate_model_base_url(value: str) -> str:
    """规范化 OpenAI 兼容 Base URL；生产环境额外执行出站限制。"""
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ModelEndpointError("Base URL 必须是有效的 HTTP(S) 地址")
    if parsed.username or parsed.password:
        raise ModelEndpointError("Base URL 不得包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise ModelEndpointError("Base URL 不得包含查询参数或片段")

    if settings.APP_ENV != "prod":
        return normalized
    if parsed.scheme != "https":
        raise ModelEndpointError("生产环境自定义模型必须使用 HTTPS")

    allowed_hosts = {
        host.strip().lower()
        for host in settings.LLM_ALLOWED_BASE_HOSTS.split(",")
        if host.strip()
    }
    configured_host = urlsplit(settings.LLM_BASE_URL).hostname
    if configured_host:
        allowed_hosts.add(configured_host.lower())
    if parsed.hostname.lower() not in allowed_hosts:
        raise ModelEndpointError("Base URL 主机不在 LLM_ALLOWED_BASE_HOSTS 中")

    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return normalized
    if (
        address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
    ):
        raise ModelEndpointError("Base URL 地址不符合生产出站策略")
    return normalized
