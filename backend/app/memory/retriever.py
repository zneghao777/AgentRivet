"""记忆检索器：embed(query) + pgvector 余弦检索（tenant/user/status 过滤 + 时间加权）。

MEMORY_SPEC §6.2：
- 检索 SQL 必须同时带 tenant_id + user_id 过滤，且仅命中 status == "active"。
- 排序：final_score = similarity * recency_weight
  recency_weight = 1 / (1 + α * days_since_last_accessed)
- 命中记忆由 service 层异步强化（access_count += 1, last_accessed_at = now）
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..knowledge.embedder import embed_query

# 时间加权系数 α（默认 0.02，可经环境变量覆盖）
RECENCY_ALPHA = 0.02


@dataclass
class MemoryHit:
    """一条记忆检索命中。"""

    memory_id: int
    content: str
    memory_type: str
    similarity: float
    source_summary: str | None = None
    created_at: str | None = None


async def retrieve(
    db: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    query: str,
    top_k: int | None = None,
) -> list[MemoryHit]:
    """按 (tenant_id, user_id) + status=active 检索记忆，时间加权排序。

    嵌入服务不可用时返回 []（由调用方兜底为"无记忆注入"）。
    """
    if not query:
        return []
    top_k = max(1, min(top_k or settings.MEMORY_TOP_K, 10))

    try:
        qvec = await embed_query(query)
    except Exception as exc:  # noqa: BLE001
        from ..knowledge.embedder import EmbeddingError

        if isinstance(exc, EmbeddingError):
            raise
        return []

    if not qvec:
        return []

    qvec_literal = "[" + ",".join(str(v) for v in qvec) + "]"
    sql = f"""
        SELECT id, content, memory_type,
               1 - (embedding <=> CAST(:qvec AS vector)) AS similarity,
               source_summary, created_at,
               last_accessed_at
        FROM memory_entries
        WHERE tenant_id = :tenant AND user_id = :user AND status = 'active'
          AND embedding IS NOT NULL
          AND 1 - (embedding <=> CAST(:qvec AS vector)) >= :threshold
        ORDER BY (1 - (embedding <=> CAST(:qvec AS vector)))
                 * (1.0 / (1.0 + :alpha * GREATEST(0,
                     EXTRACT(EPOCH FROM (now() - COALESCE(last_accessed_at, created_at))) / 86400.0
                   ))) DESC
        LIMIT :top_k
    """
    params = {
        "qvec": qvec_literal,
        "tenant": tenant_id,
        "user": user_id,
        "alpha": RECENCY_ALPHA,
        "threshold": settings.RAG_SCORE_THRESHOLD,
        "top_k": top_k,
    }
    result = await db.execute(text(sql), params)
    rows = result.fetchall()
    hits = [
        MemoryHit(
            memory_id=int(row[0]),
            content=row[1],
            memory_type=row[2],
            similarity=round(float(row[3]), 4),
            source_summary=row[4],
            created_at=row[5].isoformat() if row[5] else None,
        )
        for row in rows
    ]
    return hits


def memories_to_markdown(hits: list[MemoryHit]) -> str:
    """将命中记忆拼为注入 system prompt 的 markdown 文本（MEMORY_SPEC §6.2）。"""
    if not hits:
        return ""
    lines = [
        "以下是与该用户相关的长期记忆（来源已标注，若与当前对话矛盾请以当前对话为准）："
    ]
    for index, hit in enumerate(hits, start=1):
        type_label = "语义" if hit.memory_type == "semantic" else "情景"
        src = hit.source_summary or f"会话 {hit.memory_id}"
        lines.append(f"- [{type_label}] {hit.content}（来源：{src}）")
    return "\n".join(lines)


def days_since(dt: datetime | None, ref: datetime | None = None) -> float:
    """自最近命中以来的天数（用于强化/TTL 判定）。"""
    if dt is None:
        return math.inf
    ref = ref or datetime.now(timezone.utc)
    return max(0.0, (ref - dt).total_seconds() / 86400.0)
