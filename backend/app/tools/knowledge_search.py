"""内置工具：knowledge_search —— 企业知识库语义检索（RAG-as-Tool）。

严格模仿 builtin/calculator.py 写法：StructuredTool.from_function + pydantic args_schema。
运行上下文（tenant_id / 知识库白名单）经 harness/context.py 的 ContextVar 注入，
由 router_chat 在调用 harness runtime 前设置。

🔒 安全项：工具内部再次校验租户归属，防止 Agent 越权检索他租户知识（SPEC §3.8）。
"""

from __future__ import annotations

import asyncio
import logging

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from ..harness.context import get_tool_context
from ..knowledge.embedding import EmbeddingError
from ..knowledge.retriever import hits_to_markdown
from ..knowledge.service import knowledge_service
from ..observability import audit
from ..observability.metrics import metrics
from ..storage.db import AsyncSessionLocal

logger = logging.getLogger(__name__)


class KnowledgeSearchArgs(BaseModel):
    query: str = Field(description="检索问题（建议拆分为单个明确意图）")
    knowledge_base: str | None = Field(
        default=None, description="知识库名称；不指定则检索当前租户全部已启用知识库"
    )
    top_k: int = Field(default=5, description="返回片段数，1-10")


async def _knowledge_search_async(
    query: str, knowledge_base: str | None = None, top_k: int = 5
) -> str:
    """异步实现：读取工具上下文 → KnowledgeService.retrieve → 拼 markdown。"""
    ctx = get_tool_context()
    if not ctx.tenant_id:
        logger.warning("knowledge_search 缺少运行上下文（tenant_id 为空）")
        return "（未配置工具运行上下文，无法检索）"

    top_k = max(1, min(int(top_k or 5), 10))
    try:
        async with AsyncSessionLocal() as db:
            hits = await knowledge_service.retrieve(
                db,
                tenant_id=ctx.tenant_id,
                query=query,
                kb_ids=ctx.allowed_kb_ids,
                kb_name=knowledge_base,
                top_k=top_k,
            )
    except EmbeddingError as exc:
        return f"（检索失败: {exc}）"

    metrics.record_tool("knowledge_search")
    await audit.audit(
        ctx.tenant_id,
        "knowledge.search",
        user_id=ctx.user_id or None,
        detail={"kb": knowledge_base, "hits": len(hits), "top_k": top_k},
    )
    return hits_to_markdown(hits)


def _knowledge_search(query: str, knowledge_base: str | None = None, top_k: int = 5) -> str:
    """同步入口（非 async 环境 fallback，如本地脚本）。"""
    return asyncio.run(_knowledge_search_async(query, knowledge_base, top_k))


knowledge_search = StructuredTool.from_function(
    func=_knowledge_search,
    coroutine=_knowledge_search_async,
    name="knowledge_search",
    description=(
        "在企业知识库中进行语义检索。当用户询问文档、制度、FAQ、内部知识，"
        "或需要基于给定文档回答时使用。返回片段会标注来源文件。"
    ),
    args_schema=KnowledgeSearchArgs,
)
