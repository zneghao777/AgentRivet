"""内置工具：knowledge_search —— 企业知识库混合检索（RAG-as-Tool，L2 Agentic）。

严格模仿 builtin/calculator.py 写法：StructuredTool.from_function + pydantic args_schema。
运行上下文（tenant_id / 知识库白名单）经 harness/context.py 的 ContextVar 注入，
由 router_chat 在调用 harness runtime 前设置。

AGENTIC_RAG_UPGRADE_SPEC §3.6（L2 升级）：
- 工具暴露 strategy / rerank / expand_query 参数，由 LLM 自主决定检索策略与是否精排。
- 多路召回（向量 + 关键词 + RRF）在同一工具内编排；命中 markdown 带策略标注。

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
    mode: str = Field(
        default="auto",
        description="统一检索模式：auto|text|graph|fused；默认 auto 由索引状态确定路由",
    )
    strategy: str = Field(
        default="auto",
        description=(
            "检索策略：auto|vector|keyword|hybrid。"
            "专有名词/型号/编号/数值类问题建议 keyword 或 hybrid；"
            "语义概述类问题可用 vector；默认 auto 由系统决定"
        ),
    )
    rerank: bool = Field(
        default=True,
        description="是否对结果做 Rerank 精排（排序更准但略慢；服务不可用时自动跳过）",
    )
    expand_query: bool = Field(
        default=False,
        description="是否先对 query 做 LLM 改写（成本较高，仅口语化/指代不清时使用）",
    )


async def _knowledge_search_async(
    query: str,
    knowledge_base: str | None = None,
    top_k: int = 5,
    strategy: str = "auto",
    rerank: bool = True,
    expand_query: bool = False,
    mode: str = "auto",
) -> str:
    """异步实现：读取工具上下文 → KnowledgeService.retrieve_v2 → 拼 markdown（带策略标注）。"""
    ctx = get_tool_context()
    if not ctx.tenant_id:
        logger.warning("knowledge_search 缺少运行上下文（tenant_id 为空）")
        return "（未配置工具运行上下文，无法检索）"

    top_k = min(
        max(1, min(int(top_k or 5), 10)),
        ctx.effective_rag_top_k,
    )
    try:
        async with AsyncSessionLocal() as db:
            hits = await knowledge_service.retrieve_v2(
                db,
                tenant_id=ctx.tenant_id,
                query=query,
                kb_ids=ctx.allowed_kb_ids,
                kb_name=knowledge_base,
                top_k=top_k,
                strategy=strategy,
                rerank=rerank,
                expand_query=expand_query,
                mode=mode,
            )
            # D2 必须在所有文本/图谱/融合与 rerank 分支之后统一截断。
            hits = hits[:top_k]
    except EmbeddingError as exc:
        return f"（检索失败: {exc}）"

    metrics.record_tool("knowledge_search")
    await audit.audit(
        ctx.tenant_id,
        "knowledge.search",
        user_id=ctx.user_id or None,
        detail={
            "kb": knowledge_base,
            "hits": len(hits),
            "top_k": top_k,
            "strategy": strategy,
            "rerank": rerank,
            "mode": mode,
        },
    )
    return hits_to_markdown(hits)


def _knowledge_search(
    query: str,
    knowledge_base: str | None = None,
    top_k: int = 5,
    strategy: str = "auto",
    rerank: bool = True,
    expand_query: bool = False,
    mode: str = "auto",
) -> str:
    """同步入口（非 async 环境 fallback，如本地脚本）。"""
    return asyncio.run(
        _knowledge_search_async(
            query, knowledge_base, top_k, strategy, rerank, expand_query, mode
        )
    )


knowledge_search = StructuredTool.from_function(
    func=_knowledge_search,
    coroutine=_knowledge_search_async,
    name="knowledge_search",
    description=(
        "在企业知识库中统一编排文本与知识图谱检索（可自动融合和降级）。"
        "当用户询问文档、制度、FAQ、内部知识，或需要基于给定文档回答时使用。"
        "人物关系、婚姻亲属、师徒归属等关系问题优先使用 mode=fused，同时检索文本与图谱。"
        "返回片段会标注来源文件与检索策略；专有名词/型号类问题建议指定 strategy=hybrid 或 keyword。"
    ),
    args_schema=KnowledgeSearchArgs,
)
