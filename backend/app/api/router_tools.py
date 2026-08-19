"""工具查询路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..tools.registry import tool_registry
from .deps import AuthContext, require_auth
from .schemas import ApiResponse, ToolResponse

router = APIRouter(prefix="/tools", tags=["tools"])


@router.get("", response_model=ApiResponse)
async def list_tools(
    auth: AuthContext = Depends(require_auth),
):
    infos = tool_registry.snapshot()
    return ApiResponse(
        data=[ToolResponse(**i.__dict__) for i in infos]
    )
