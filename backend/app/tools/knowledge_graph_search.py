"""内置工具：knowledge_graph_search —— 知识图谱语义检索（Graph RAG，L2 Agentic）。

GRAPH_RAG_SPEC §4.3：
- 新增并行的图谱检索路径，与现有 knowledge_search 互补。
- 处理全局性问题 / 多跳推理 / 关系型问题（实体→关系→事件时间线）。
- 低侵入接入：StructuredTool，运行上下文经 harness/context.py ContextVar 注入。

🔒 安全项：工具内部再次校验租户归属，防止越权检索他租户图谱（SPEC §3.8）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from ..harness.context import get_tool_context
from ..knowledge.graph.graph_retriever import graph_retriever
from ..observability import audit
from ..observability.metrics import metrics
from ..storage.db import AsyncSessionLocal

logger = logging.getLogger(__name__)

VALID_STRATEGIES = {"auto", "entity", "hop", "global", "timeline"}


class KnowledgeGraphSearchArgs(BaseModel):
    query: str = Field(description="自然语言问题（实体/关系/全局性/时间线类）")
    knowledge_base: str | None = Field(
        default=None, description="知识库名称；不指定则检索当前租户全部已启用知识库"
    )
    strategy: str = Field(
        default="auto",
        description=(
            "图检索策略：auto|entity|hop|global|timeline。"
            "global 用于全局性问题（如'成长的主要阶段'）；hop 用于多跳推理；"
            "timeline 用于实体事件时间线；默认 auto 由系统判断是否含明确实体"
        ),
    )
    hop: int = Field(default=2, description="图扩散跳数（1~3）", ge=1, le=3)


async def _graph_search_async(
    query: str,
    knowledge_base: str | None = None,
    strategy: str = "auto",
    hop: int = 2,
) -> str:
    """异步实现：解析 kb 白名单 → GraphRetriever.hybrid_search → 拼 markdown。"""
    ctx = get_tool_context()
    if not ctx.tenant_id:
        logger.warning("knowledge_graph_search 缺少运行上下文（tenant_id 为空）")
        return "（未配置工具运行上下文，无法检索）"

    strategy = strategy if strategy in VALID_STRATEGIES else "auto"
    try:
        async with AsyncSessionLocal() as db:
            results = await _run(db, ctx.tenant_id, ctx.allowed_kb_ids, knowledge_base,
                                  query, strategy, hop)
    except Exception as exc:  # noqa: BLE001
        logger.error("图谱检索失败: %s", exc)
        return f"（图谱检索失败: {exc}）"

    metrics.record_tool("knowledge_graph_search")
    await audit.audit(
        ctx.tenant_id,
        "knowledge.graph_search",
        user_id=ctx.user_id or None,
        detail={
            "kb": knowledge_base,
            "strategy": strategy,
            "hop": hop,
            "entities": len(results.get("entities", [])),
            "relations": len(results.get("relations", [])),
        },
    )
    return _to_markdown(results, strategy)


async def _run(db, tenant_id: str, allowed_kb_ids, kb_name: str | None,
               query: str, strategy: str, hop: int) -> dict:
    """解析知识库并执行图检索。"""
    from ..knowledge.service import knowledge_service

    resolved = await knowledge_service._resolve_kb_ids(
        db, tenant_id=tenant_id, kb_ids=allowed_kb_ids, kb_name=kb_name
    )
    if not resolved:
        return {
            "entities": [], "relations": [], "communities": [],
            "events": [], "evidence_chunk_ids": [], "hits": [], "strategy": strategy,
        }
    # 合并多库结果（通常单库）
    merged: dict = {
        "entities": [], "relations": [], "communities": [],
        "events": [], "evidence_chunk_ids": [], "hits": [], "strategy": strategy,
    }
    seen_entities: set[str] = set()
    seen_relations: set[str] = set()
    seen_chunks: set[int] = set()
    for kb_id in resolved:
        r = await graph_retriever.hybrid_search(
            db, tenant_id=tenant_id, kb_id=kb_id,
            query=query, strategy=strategy, hop=hop, top_k=10,
        )
        merged["entities"].extend(
            e for e in r["entities"] if str(e) not in seen_entities
        )
        seen_entities.update(str(e) for e in r["entities"])
        merged["relations"].extend(
            rel for rel in r["relations"]
            if f"{rel['source_entity_id']}-{rel['relation_type']}-{rel['target_entity_id']}" not in seen_relations
        )
        seen_relations.update(
            f"{rel['source_entity_id']}-{rel['relation_type']}-{rel['target_entity_id']}"
            for rel in r["relations"]
        )
        merged["communities"].extend(r["communities"])
        merged["events"].extend(r["events"])
        for c in r["evidence_chunk_ids"]:
            if c not in seen_chunks:
                seen_chunks.add(c)
        merged["hits"].extend(r["hits"])
    merged["evidence_chunk_ids"] = sorted(seen_chunks)
    return merged


def _to_markdown(results: dict, strategy: str) -> str:
    """将图检索结果拼为 markdown（供 Agent 阅读）。"""
    parts: list[str] = []
    entities = results.get("entities", [])
    relations = results.get("relations", [])
    communities = results.get("communities", [])
    events = results.get("events", [])

    if communities:
        parts.append("【社区摘要】")
        for c in communities:
            kw = "、".join(c.get("keywords", []) or [])
            parts.append(f"- {c.get('summary')}" + (f"（关键词：{kw}）" if kw else ""))

    if entities:
        parts.append("【相关实体】")
        for e in entities:
            name = e.get("canonical_name") or e.get("name")
            parts.append(f"- {name}（{e.get('entity_type')}）")

    if relations:
        parts.append("【实体关系】")
        for r in relations:
            parts.append(
                f"- {r.get('source_name')} —[{r.get('relation_type')}]→ {r.get('target_name')}"
            )

    if events:
        parts.append("【事件时间线】")
        for ev in events:
            parts.append(
                f"- {ev.get('order_no')}. {ev.get('summary') or ''}"
                f"（{ev.get('subject_entity_id')} {ev.get('predicate')} {ev.get('object') or ''}）"
            )

    hits = results.get("hits", [])
    evidence = [h for h in hits if h.source_chunk_id]
    if evidence:
        parts.append("【证据片段】")
        for h in evidence[:5]:
            parts.append(f"- chunk #{h.source_chunk_id}: {h.text}")

    if not parts:
        return "（知识图谱中未检索到相关内容）"
    return "\n".join(parts)


def _graph_search(
    query: str,
    knowledge_base: str | None = None,
    strategy: str = "auto",
    hop: int = 2,
) -> str:
    """同步入口（非 async 环境 fallback，如本地脚本）。"""
    return asyncio.run(_graph_search_async(query, knowledge_base, strategy, hop))


knowledge_graph_search = StructuredTool.from_function(
    func=_graph_search,
    coroutine=_graph_search_async,
    name="knowledge_graph_search",
    description=(
        "在知识库的知识图谱中进行语义检索（实体/关系/社区摘要/事件时间线）。"
        "当问题涉及实体关系、多跳推理、全局性问题（如'成长经历了哪些阶段'）"
        "或事件时间线时使用，与 knowledge_search 互补。"
        "返回实体、关系、社区摘要与事件时间线。"
    ),
    args_schema=KnowledgeGraphSearchArgs,
)
