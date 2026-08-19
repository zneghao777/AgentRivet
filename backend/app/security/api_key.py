"""API Key 校验。

🔒 安全项：只保存哈希，不存明文。请求头传输 X-API-Key。
"""

import hashlib
import hmac

from ..config import settings


def hash_api_key(key: str) -> str:
    """对 API Key 做不可逆哈希（用于 tenants 表存储）。"""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def verify_api_key(key: str | None) -> bool:
    """校验 API Key 是否合法（常量时间比较防时序攻击）。

    合法 Key 来源：settings.API_KEYS（逗号分隔）。
    若未配置任何 Key，出于开发便利在 dev 环境放行，prod 拒绝。
    """
    if not key:
        return False
    keys = settings.api_keys_list
    if not keys:
        # 未配置 API_KEYS 时：dev 允许启动，但接口校验仍应通过开发默认 key
        return settings.APP_ENV != "prod" and key == "sk-dev-key-1"
    return any(hmac.compare_digest(k, key) for k in keys)


def tenant_for_api_key(key: str | None) -> str:
    """根据 API Key 解析租户（第一版：所有 key 映射默认租户，或前缀映射）。

    规则：允许 API Key 形如 `tenantname_xxx` 映射到对应租户，否则用默认租户。
    """
    if not key:
        return settings.DEFAULT_TENANT
    if "_" in key:
        prefix = key.split("_", 1)[0]
        if prefix and prefix != "sk" and prefix != "sk-dev":
            return prefix
    return settings.DEFAULT_TENANT
