"""内置工具：memory_search —— 用户长期记忆检索（MEMORY_SPEC §6.2 方式 B）。

与 knowledge_search 同款写法：StructuredTool.from_function + pydantic args_schema。
运行上下文（tenant_id / user_id）经 harness/context.py 的 ContextVar 注入。

🔒 安全项：工具内部按 (tenant_id, user_id) 过滤，只能访问当前用户记忆。
"""

from __future__ import annotations

import asyncio
import logging

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from ..config import settings
from ..harness.context import get_tool_context
from ..memory.hygiene import long_term_enabled
from ..memory.service import memory_service
from ..observability import audit
from ..observability.metrics import metrics
from ..storage.db import AsyncSessionLocal

logger = logging.getLogger(__name__)


class MemorySearchArgs(BaseModel):
    query: str = Field(description="回忆的关键问题（关于用户的偏好/事实/历史）")
    top_k: int = Field(default=5, description="返回记忆条数，1-10")


async def _memory_search_async(query: str, top_k: int = 5) -> str:
    """异步实现：读取工具上下文 → 检索当前用户记忆 → 拼 markdown。"""
    ctx = get_tool_context()
    if not ctx.tenant_id:
        logger.warning("memory_search 缺少运行上下文（tenant_id 为空）")
        return "（未配置工具运行上下文，无法检索）"
    if not settings.MEMORY_ENABLED or not settings.MEMORY_SEARCH_TOOL:
        return "（长期记忆功能未开启）"

    top_k = max(1, min(int(top_k or 5), 10))
    async with AsyncSessionLocal() as db:
        if not await long_term_enabled(
            db, tenant_id=ctx.tenant_id, user_id=ctx.user_id or settings.ANONYMOUS_USER_ID
        ):
            return "（该用户长期记忆已关闭）"
        hits = await memory_service.search_as_tool(
            db,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id or settings.ANONYMOUS_USER_ID,
            query=query,
            top_k=top_k,
        )

    metrics.record_tool("memory_search")
    await audit.audit(
        ctx.tenant_id,
        "memory.search",
        user_id=ctx.user_id or None,
        detail={"hits": len(hits), "top_k": top_k},
    )
    return memory_service.format_for_prompt(hits) or "（未检索到相关长期记忆）"


def _memory_search(query: str, top_k: int = 5) -> str:
    """同步入口（非 async 环境 fallback）。"""
    return asyncio.run(_memory_search_async(query, top_k))


memory_search = StructuredTool.from_function(
    func=_memory_search,
    coroutine=_memory_search_async,
    name="memory_search",
    description=(
        "检索该用户的长期记忆（跨会话记住的事实、偏好、历史决策）。"
        "当需要回忆用户之前透露过的信息、偏好或处理过的事项时使用。"
    ),
    args_schema=MemorySearchArgs,
)
