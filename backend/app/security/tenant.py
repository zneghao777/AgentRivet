"""租户上下文：从请求头解析当前租户。"""

from dataclasses import dataclass


@dataclass
class TenantContext:
    """一次请求的租户上下文。"""

    tenant_id: str
    api_key: str


def resolve_tenant(api_key: str | None, tenant_header: str | None) -> TenantContext:
    """解析租户。

    优先使用 X-Tenant-Id 请求头（需该校验通过），否则从 API Key 派生。
    """
    from .api_key import tenant_for_api_key

    tenant_id = tenant_for_api_key(api_key)
    if tenant_header and tenant_header.strip():
        # 第一版不做跨租户细粒度 RBAC，直接信任显式租户头。
        tenant_id = tenant_header.strip()
    return TenantContext(tenant_id=tenant_id, api_key=api_key or "")
