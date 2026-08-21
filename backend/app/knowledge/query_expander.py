"""Query 改写：复用主 LLM 把口语/指代不清的问题改写为检索友好表述。

AGENTIC_RAG_UPGRADE_SPEC §1.3 / §3.5：
- 默认关闭（QUERY_EXPAND_ENABLED=False），开启后仅作用于检索 query，不回写对话。
- 改写失败 / 超时 / 空白 → 原样返回，不影响主流程。
- QUERY_EXPAND_TEMPLATE 非空时优先用配置模板。
"""

from __future__ import annotations

import logging

from langchain_core.language_models.chat_models import BaseChatModel

from ..config import settings

logger = logging.getLogger(__name__)

BUILTIN_TEMPLATE = "把以下用户问题改写为适合知识库检索的关键词化表述，只输出改写结果：\n{query}"


def _content_str(response) -> str:
    """LLM 响应 content 转 str（兼容 str 与多模态 list 内容）。"""
    content = getattr(response, "content", "")
    if isinstance(content, list):
        return "".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part)
            for part in content
        ).strip()
    return str(content or "").strip()


async def expand_query(query: str, llm: BaseChatModel | None = None) -> str:
    """LLM 单轮改写：把口语问题改写为检索友好 query。

    - 失败 / 超时 / 空白结果 → 原样返回原始 query（降级红线）。
    - 仅当 QUERY_EXPAND_ENABLED 时由 retrieve_hybrid 调用。
    """
    if not query or not query.strip():
        return query
    try:
        if llm is None:
            # 延迟导入避免与 agents 模块的潜在循环依赖
            from ..agents.base_agent import create_llm

            llm = create_llm()
        template = settings.QUERY_EXPAND_TEMPLATE or BUILTIN_TEMPLATE
        prompt = template.format(query=query)
        response = await llm.ainvoke(prompt)
        rewritten = _content_str(response)
        if not rewritten:
            return query
        return rewritten
    except Exception as exc:  # noqa: BLE001
        logger.warning("Query 改写失败，使用原始 query: %s", exc)
        return query
