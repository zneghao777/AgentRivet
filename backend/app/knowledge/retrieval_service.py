"""文本与图谱统一检索、确定性路由、融合、引用和降级。"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..storage.models import KnowledgeBase, KnowledgeChunk
from .graph.graph_retriever import graph_retriever
from .retriever import Hit, retrieve_hybrid

RELATION_QUERY = re.compile(
    r"关系|关联|之间|谁的|属于|任职|上下级|影响|路径|多跳|脉络|时间线|演变|全局|概括|"
    r"老婆|老公|妻子|丈夫|配偶|伴侣|道侣|夫人|夫君|成婚|结婚|嫁给|娶了|"
    r"父亲|母亲|父母|儿子|女儿|子女|兄弟|姐妹|亲属|师父|师傅|徒弟|师徒"
)


def classify_query(query: str) -> str:
    return "graph" if RELATION_QUERY.search(query) else "text"


def _hit_dict(hit: Hit, channels: list[str] | None = None) -> dict[str, Any]:
    return {
        "text": hit.text,
        "source_file": hit.source_file,
        "chunk_index": hit.chunk_index,
        "score": hit.score,
        "kb_id": str(hit.kb_id),
        "doc_id": hit.doc_id,
        "chunk_id": hit.chunk_id,
        "strategy": hit.strategy,
        "rerank_score": hit.rerank_score,
        "channels": channels or ["text"],
    }


async def retrieve_knowledge(
    db: AsyncSession,
    *,
    tenant_id: str,
    query: str,
    kb_ids: list[uuid.UUID] | None,
    mode: str = "auto",
    top_k: int = 5,
    rerank: bool = True,
    expand_query: bool = False,
    graph_hop: int = 2,
) -> dict[str, Any]:
    started = time.perf_counter()
    filters = [
        KnowledgeBase.tenant_id == tenant_id,
        KnowledgeBase.status == "active",
    ]
    if kb_ids is not None:
        if not kb_ids:
            return _empty_result(query, mode, "no_knowledge_base")
        filters.append(KnowledgeBase.id.in_(kb_ids))
    kbs = list((await db.execute(select(KnowledgeBase).where(*filters))).scalars().all())
    if not kbs:
        return _empty_result(query, mode, "no_knowledge_base")

    text_ids = [kb.id for kb in kbs]
    graph_ids = [
        kb.id
        for kb in kbs
        if kb.index_mode == "graph_enhanced" and kb.graph_status in {"ready", "partial"}
    ]
    requested_mode = mode if mode in {"auto", "text", "graph", "fused"} else "auto"
    if requested_mode == "auto":
        resolved_mode = "fused" if graph_ids and classify_query(query) == "graph" else "text"
    else:
        resolved_mode = requested_mode
    degradations: list[str] = []
    if resolved_mode in {"graph", "fused"} and not graph_ids:
        degradations.append("graph_not_ready")
        resolved_mode = "text" if requested_mode in {"auto", "fused"} else "graph"

    text_hits: list[Hit] = []
    text_ms = 0
    if resolved_mode in {"text", "fused"}:
        text_started = time.perf_counter()
        text_hits = await retrieve_hybrid(
            db,
            tenant_id=tenant_id,
            query=query,
            kb_ids=text_ids,
            top_k=top_k,
            strategy="auto",
            rerank=rerank,
            expand_query=expand_query,
        )
        text_ms = round((time.perf_counter() - text_started) * 1000)

    graph_context: dict[str, list] = {
        "entities": [],
        "relations": [],
        "communities": [],
        "events": [],
    }
    evidence_ids: set[int] = set()
    graph_ms = 0
    if resolved_mode in {"graph", "fused"}:
        graph_started = time.perf_counter()
        graph_strategy = "auto"
        for graph_kb_id in graph_ids:
            try:
                result = await graph_retriever.hybrid_search(
                    db,
                    tenant_id=tenant_id,
                    kb_id=graph_kb_id,
                    query=query,
                    strategy=graph_strategy,
                    hop=max(1, min(graph_hop, 3)),
                    top_k=max(5, top_k),
                )
            except Exception:  # 图通道失败不得阻断文本结果
                degradations.append("graph_channel_failed")
                continue
            for key in graph_context:
                graph_context[key].extend(result.get(key) or [])
            evidence_ids.update(int(value) for value in result.get("evidence_chunk_ids") or [])
        graph_ms = round((time.perf_counter() - graph_started) * 1000)

    evidence_rows: list[KnowledgeChunk] = []
    if evidence_ids:
        evidence_rows = list(
            (
                await db.execute(
                    select(KnowledgeChunk).where(
                        KnowledgeChunk.tenant_id == tenant_id,
                        KnowledgeChunk.id.in_(evidence_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
    evidence_by_id = {int(chunk.id): chunk for chunk in evidence_rows}
    text_by_id = {hit.chunk_id: hit for hit in text_hits}
    final_hits: list[dict[str, Any]] = []
    for hit in text_hits:
        channels = ["text", "graph"] if hit.chunk_id in evidence_by_id else ["text"]
        final_hits.append(_hit_dict(hit, channels))
    for chunk_id, chunk in evidence_by_id.items():
        if chunk_id in text_by_id:
            continue
        final_hits.append(
            {
                "text": chunk.chunk_text,
                "source_file": chunk.source_file,
                "chunk_index": chunk.chunk_index,
                "score": 0.5,
                "kb_id": str(chunk.kb_id),
                "doc_id": chunk.doc_id,
                "chunk_id": chunk_id,
                "strategy": "graph",
                "rerank_score": None,
                "channels": ["graph"],
            }
        )
    final_hits.sort(key=lambda item: (len(item["channels"]), item["score"]), reverse=True)
    final_hits = final_hits[:top_k]
    citations = [
        {
            "kb_id": item["kb_id"],
            "doc_id": item["doc_id"],
            "chunk_id": item["chunk_id"],
            "source_file": item["source_file"],
            "channels": item["channels"],
            "score": item["score"],
            "graph_paths": [],
        }
        for item in final_hits
    ]
    total_ms = round((time.perf_counter() - started) * 1000)
    return {
        "query": query,
        "requested_mode": requested_mode,
        "resolved_mode": resolved_mode,
        "hits": final_hits,
        "text_hits": [_hit_dict(hit) for hit in text_hits],
        "graph_context": graph_context,
        "evidence_chunks": [
            {
                "chunk_id": int(chunk.id),
                "kb_id": str(chunk.kb_id),
                "doc_id": chunk.doc_id,
                "source_file": chunk.source_file,
                "text": chunk.chunk_text,
            }
            for chunk in evidence_rows
        ],
        "citations": citations,
        "degradations": list(dict.fromkeys(degradations)),
        "timings": {"text_ms": text_ms, "graph_ms": graph_ms, "total_ms": total_ms},
    }


def _empty_result(query: str, requested_mode: str, degradation: str) -> dict[str, Any]:
    return {
        "query": query,
        "requested_mode": requested_mode,
        "resolved_mode": "text",
        "hits": [],
        "text_hits": [],
        "graph_context": {"entities": [], "relations": [], "communities": [], "events": []},
        "evidence_chunks": [],
        "citations": [],
        "degradations": [degradation],
        "timings": {"text_ms": 0, "graph_ms": 0, "total_ms": 0},
    }


def unified_result_to_markdown(result: dict[str, Any]) -> str:
    lines = [f"实际检索模式: {result['resolved_mode']}"]
    if result.get("degradations"):
        lines.append("降级: " + ", ".join(result["degradations"]))
    for item in result.get("hits") or []:
        channels = "+".join(item.get("channels") or [])
        lines.extend(
            [
                f"\n### {item['source_file']} · {channels}",
                item["text"],
                f"引用: KB-{item['kb_id']}/{item['chunk_id']}",
            ]
        )
    if len(lines) <= 2 and not result.get("hits"):
        lines.append("（未检索到相关知识）")
    return "\n".join(lines)
