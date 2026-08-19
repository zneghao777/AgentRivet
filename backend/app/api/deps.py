"""依赖注入：鉴权、租户、DB Session。"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..security.api_key import verify_api_key
from ..security.tenant import TenantContext, resolve_tenant
from ..storage.db import get_db


@dataclass
class AuthContext:
    """已鉴权请求的上下文。"""

    tenant: TenantContext
    user_id: str


def get_client_ip(request: Request) -> str:
    """获取客户端 IP。"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def require_auth(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
) -> AuthContext:
    """鉴权 + 租户解析依赖。

    未带/非法 API Key → 401。
    """
    if not verify_api_key(x_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API key",
        )
    tenant = resolve_tenant(x_api_key, x_tenant_id)
    # 用户 ID：第一版固定 "api-user"，可后续扩展
    return AuthContext(tenant=tenant, user_id="api-user")


# 便于引用
DbDep = AsyncSession
