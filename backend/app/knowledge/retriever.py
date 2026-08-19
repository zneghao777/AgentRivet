"""检索器：embed(query) + pgvector 余弦检索（tenant/kb 过滤）。

🔒 安全红线：所有 SQL 必须带 tenant_id 过滤，禁止跨租户检索（SPEC §3.4/§3.8）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from .embedding import embed_query


@dataclass
class Hit:
    """一个检索命中片段。"""

    text: str
    source_file: str
    chunk_index: int
    score: float


async def retrieve(
    db: AsyncSession,
    *,
    tenant_id: str,
    query: str,
    kb_ids: list[uuid.UUID] | None = None,
    top_k: int | None = None,
    threshold: float | None = None,
) -> list[Hit]:
    """按租户 + 知识库白名单做余弦相似度检索。

    - kb_ids 为 None → 检索该租户全部知识库；空列表 → 不检索。
    - threshold 缺省取 settings.RAG_SCORE_THRESHOLD。
    """
    if not query:
        return []
    if kb_ids is not None and not kb_ids:
        return []
    top_k = top_k or settings.RAG_TOP_K
    threshold = settings.RAG_SCORE_THRESHOLD if threshold is None else threshold

    qvec = await embed_query(query)
    if not qvec:
        return []

    # pgvector 以文本字面量绑定并显式转型，避免 psycopg 将其绑定为 double precision[]
    qvec_literal = "[" + ",".join(str(v) for v in qvec) + "]"

    params: dict = {
        "qvec": qvec_literal,
        "tenant": tenant_id,
        "threshold": threshold,
        "top_k": max(1, min(top_k, 10)),
    }
    where = "tenant_id = :tenant"
    if kb_ids is not None:
        params["kb_ids"] = [str(k) for k in kb_ids]
        where += " AND kb_id = ANY(:kb_ids)"

    sql = f"""
        SELECT chunk_text, source_file, chunk_index,
               1 - (embedding <=> CAST(:qvec AS vector)) AS score
        FROM knowledge_chunks
        WHERE {where}
          AND 1 - (embedding <=> CAST(:qvec AS vector)) >= :threshold
        ORDER BY embedding <=> CAST(:qvec AS vector)
        LIMIT :top_k
    """
    result = await db.execute(text(sql), params)
    rows = result.fetchall()
    return [
        Hit(
            text=row[0],
            source_file=row[1],
            chunk_index=row[2],
            score=round(float(row[3]), 4),
        )
        for row in rows
    ]


def hits_to_markdown(hits: list[Hit]) -> str:
    """将命中片段拼为 markdown（带 [来源] 标注，供 Agent 引用）。"""
    if not hits:
        return "（知识库中未检索到相关内容）"
    lines: list[str] = []
    for index, hit in enumerate(hits, start=1):
        lines.append(
            f"[片段 {index} | 来源: {hit.source_file}] (相关度 {hit.score})\n"
            f"{hit.text}\n"
        )
    return "\n".join(lines)
