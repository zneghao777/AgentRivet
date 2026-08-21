"""检索器：向量（pgvector 余弦）+ 关键词（pg_trgm）+ RRF 融合 + Rerank 编排。

AGENTIC_RAG_UPGRADE_SPEC §3.3：
- retrieve()：纯向量检索（保留；供 HYBRID_RETRIEVAL_ENABLED=False 退回与旧调用兼容）。
- keyword_retrieve()：pg_trgm similarity 关键词路（专有名词/型号/数值/代码场景）。
- rrf_fuse()：Reciprocal Rank Fusion 纯函数（score(d) = Σ 1/(k + rank_r(d))）。
- retrieve_hybrid()：统一入口（strategy=auto|vector|keyword|hybrid + rerank + expand_query）。

KB_MANAGEMENT_SPEC §3.6（沿用）：
- 检索 SQL 必须带 tenant_id 过滤 + `enabled = true` 过滤（安全红线）。
- Hit 携带 kb_id + chunk_id（稳定主键），供溯源深链定位。
- hits_to_markdown 输出机器可读引用锚点 `引用: KB-{kb_id}/{chunk_id}`，
  升级后附策略标注（AGENTIC_RAG_UPGRADE_SPEC §3.6）。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, replace

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..observability.metrics import metrics
from .embedder import embed_query
from .query_expander import expand_query as _expand_query
from .reranker import RerankError, is_rerank_available
from .reranker import rerank as _rerank_hits

logger = logging.getLogger(__name__)

VALID_STRATEGIES = {"auto", "vector", "keyword", "hybrid"}


@dataclass
class Hit:
    """一个检索命中片段（携带溯源所需的稳定主键）。

    - score：最终展示分（rerank 后为 relevance_score；否则为融合/相似分）。
    - strategy：命中来源 vector | keyword | hybrid | rerank（向后兼容默认 vector）。
    - rerank_score：rerank 原始分（未精排时为 None）。
    """

    text: str
    source_file: str
    chunk_index: int
    score: float
    kb_id: uuid.UUID  # 知识库 id（深链用）
    chunk_id: int  # 分块稳定主键（定位/编辑用）
    strategy: str = "vector"
    rerank_score: float | None = None
    doc_id: int | None = None
    heading: str | None = None
    line_start: int | None = None
    line_end: int | None = None


def _line_span(raw_text: str | None, chunk_text: str) -> tuple[int | None, int | None]:
    """计算分块在解析文本中的 1-based 行范围；找不到时安全降级。"""
    if not raw_text or not chunk_text:
        return None, None
    position = raw_text.find(chunk_text)
    if position < 0:
        position = raw_text.find(chunk_text.strip())
    if position < 0:
        return None, None
    line_start = raw_text.count("\n", 0, position) + 1
    line_end = line_start + chunk_text.count("\n")
    return line_start, max(line_start, line_end)


async def _vector_search(
    db: AsyncSession,
    *,
    tenant_id: str,
    query: str,
    kb_ids: list[uuid.UUID] | None,
    top_k: int,
    threshold: float,
) -> list[Hit]:
    """pgvector 余弦检索内部实现（无 clamp，粗排可取更大候选池）。"""
    qvec = await embed_query(query)
    if not qvec:
        return []

    # pgvector 以文本字面量绑定并显式转型，避免 psycopg 将其绑定为 double precision[]
    qvec_literal = "[" + ",".join(str(v) for v in qvec) + "]"

    params: dict = {
        "qvec": qvec_literal,
        "tenant": tenant_id,
        "threshold": threshold,
        "top_k": max(1, top_k),
    }
    where = "kc.tenant_id = :tenant AND kc.enabled = true"
    if kb_ids is not None:
        params["kb_ids"] = [str(k) for k in kb_ids]
        where += " AND kc.kb_id = ANY(:kb_ids)"

    sql = f"""
        SELECT kc.chunk_text, kc.source_file, kc.chunk_index,
               1 - (kc.embedding <=> CAST(:qvec AS vector)) AS score,
               kc.kb_id, kc.id, kc.doc_id, kc.metadata, dm.raw_text
        FROM knowledge_chunks kc
        LEFT JOIN document_meta dm ON dm.id = kc.doc_id
        WHERE {where}
          AND 1 - (kc.embedding <=> CAST(:qvec AS vector)) >= :threshold
        ORDER BY kc.embedding <=> CAST(:qvec AS vector)
        LIMIT :top_k
    """
    result = await db.execute(text(sql), params)
    rows = result.fetchall()
    hits: list[Hit] = []
    for row in rows:
        line_start, line_end = _line_span(row[8], row[0])
        meta = row[7] or {}
        hits.append(Hit(
            text=row[0],
            source_file=row[1],
            chunk_index=row[2],
            score=round(float(row[3]), 4),
            kb_id=uuid.UUID(str(row[4])),
            chunk_id=int(row[5]),
            strategy="vector",
            doc_id=int(row[6]) if row[6] is not None else None,
            heading=meta.get("heading") if isinstance(meta, dict) else None,
            line_start=line_start,
            line_end=line_end,
        ))
    return hits


async def retrieve(
    db: AsyncSession,
    *,
    tenant_id: str,
    query: str,
    kb_ids: list[uuid.UUID] | None = None,
    top_k: int | None = None,
    threshold: float | None = None,
) -> list[Hit]:
    """按租户 + 知识库白名单做余弦相似度检索（仅 enabled 分块）。

    - kb_ids 为 None → 检索该租户全部知识库；空列表 → 不检索。
    - threshold 缺省取 settings.RAG_SCORE_THRESHOLD。
    - 兼容旧入口（HYBRID_RETRIEVAL_ENABLED=False 时 retrieve_hybrid 亦退回此路径）。
    """
    if not query:
        return []
    if kb_ids is not None and not kb_ids:
        return []
    top_k = top_k or settings.RAG_TOP_K
    threshold = settings.RAG_SCORE_THRESHOLD if threshold is None else threshold
    return await _vector_search(
        db,
        tenant_id=tenant_id,
        query=query,
        kb_ids=kb_ids,
        top_k=max(1, min(top_k, 10)),
        threshold=threshold,
    )


async def keyword_retrieve(
    db: AsyncSession,
    *,
    tenant_id: str,
    query: str,
    kb_ids: list[uuid.UUID] | None = None,
    top_k: int | None = None,
    threshold: float | None = None,
) -> list[Hit]:
    """pg_trgm 关键词检索（AGENTIC_RAG_UPGRADE_SPEC §1.1/§3.3）。

    - similarity(chunk_text, query) 按 3-gram 切分，对中文/英文/数字/型号均有效，
      短查询、专有名词命中优于纯向量。
    - threshold 缺省取 settings.KEYWORD_SCORE_THRESHOLD。
    - 安全红线：SQL 强制 tenant_id + enabled 过滤。
    """
    if not query:
        return []
    if kb_ids is not None and not kb_ids:
        return []
    top_k = top_k or settings.KEYWORD_TOP_K
    threshold = settings.KEYWORD_SCORE_THRESHOLD if threshold is None else threshold

    params: dict = {
        "q": query,
        "tenant": tenant_id,
        "threshold": threshold,
        "top_k": max(1, top_k),
    }
    where = "kc.tenant_id = :tenant AND kc.enabled = true"
    if kb_ids is not None:
        params["kb_ids"] = [str(k) for k in kb_ids]
        where += " AND kc.kb_id = ANY(:kb_ids)"

    sql = f"""
        SELECT kc.chunk_text, kc.source_file, kc.chunk_index,
               similarity(kc.chunk_text, :q) AS score,
               kc.kb_id, kc.id, kc.doc_id, kc.metadata, dm.raw_text
        FROM knowledge_chunks kc
        LEFT JOIN document_meta dm ON dm.id = kc.doc_id
        WHERE {where}
          AND similarity(kc.chunk_text, :q) >= :threshold
        ORDER BY score DESC
        LIMIT :top_k
    """
    result = await db.execute(text(sql), params)
    rows = result.fetchall()
    hits: list[Hit] = []
    for row in rows:
        line_start, line_end = _line_span(row[8], row[0])
        meta = row[7] or {}
        hits.append(Hit(
            text=row[0],
            source_file=row[1],
            chunk_index=row[2],
            score=round(float(row[3]), 4),
            kb_id=uuid.UUID(str(row[4])),
            chunk_id=int(row[5]),
            strategy="keyword",
            doc_id=int(row[6]) if row[6] is not None else None,
            heading=meta.get("heading") if isinstance(meta, dict) else None,
            line_start=line_start,
            line_end=line_end,
        ))
    return hits


def rrf_fuse(
    vector_hits: list[Hit],
    keyword_hits: list[Hit],
    k: int | None = None,
) -> list[Hit]:
    """Reciprocal Rank Fusion：score(d) = Σ 1/(k + rank_r(d))。

    - 按 chunk_id 合并去重；输出融合分排序的 Hit（strategy="hybrid"）。
    - 纯函数，无 IO。
    """
    k = settings.RRF_K if k is None else k
    scores: dict[int, float] = {}
    by_chunk: dict[int, Hit] = {}
    for hits in (vector_hits, keyword_hits):
        for rank, hit in enumerate(hits, start=1):
            contribution = 1.0 / (k + rank)
            if hit.chunk_id in by_chunk:
                scores[hit.chunk_id] += contribution
            else:
                by_chunk[hit.chunk_id] = hit
                scores[hit.chunk_id] = contribution
    fused = sorted(by_chunk.values(), key=lambda h: scores[h.chunk_id], reverse=True)
    return [
        replace(hit, score=round(scores[hit.chunk_id], 6), strategy="hybrid")
        for hit in fused
    ]


async def retrieve_hybrid(
    db: AsyncSession,
    *,
    tenant_id: str,
    query: str,
    kb_ids: list[uuid.UUID] | None = None,
    top_k: int | None = None,
    threshold: float | None = None,
    strategy: str = "auto",  # auto | vector | keyword | hybrid
    rerank: bool = True,  # 是否 Rerank 精排（受 RERANK_ENABLED 与服务可用性约束）
    expand_query: bool = False,  # 是否 Query 改写（受 QUERY_EXPAND_ENABLED 约束）
) -> list[Hit]:
    """统一检索入口（AGENTIC_RAG_UPGRADE_SPEC §3.3）。

    - strategy=auto：HYBRID_RETRIEVAL_ENABLED 时走 hybrid，否则 vector。
    - strategy=vector：仅向量路（与旧 retrieve 行为一致），score=余弦。
    - strategy=keyword：仅 pg_trgm 关键词路，score=similarity。
    - strategy=hybrid：双路召回 + RRF 融合，score=RRF 融合分。
    - rerank=True 且服务可用：粗排候选（VECTOR_TOP_K/KEYWORD_TOP_K 池）→ cross-encoder
      精排 → 取 top（RERANK_TOP_N 与 top_k 取大），score=relevance_score，strategy="rerank"。
    - rerank 不可用 / 失败：降级返回粗排 top_k（绝不抛 5xx）。
    - expand_query=True 且 QUERY_EXPAND_ENABLED：先 LLM 改写 query（失败原样）。
    """
    if not query:
        return []
    if kb_ids is not None and not kb_ids:
        return []
    top_k = top_k or settings.RAG_TOP_K
    top_k = max(1, min(top_k, 10))

    # strategy 决策
    if strategy not in VALID_STRATEGIES:
        strategy = "auto"
    if strategy == "auto":
        strategy = "hybrid" if settings.HYBRID_RETRIEVAL_ENABLED else "vector"

    # Query 改写（可选）：开关关闭时零开销；失败原样降级
    if expand_query and settings.QUERY_EXPAND_ENABLED:
        query = await _expand_query(query)

    metrics.record_retrieval(strategy)

    threshold = settings.RAG_SCORE_THRESHOLD if threshold is None else threshold

    # ---- 粗排：rerank 开启时取大候选池，否则直接取 top_k ----
    if rerank and settings.RERANK_ENABLED:
        vector_k = max(top_k, settings.VECTOR_TOP_K)
        keyword_k = max(top_k, settings.KEYWORD_TOP_K)
    else:
        vector_k, keyword_k = top_k, top_k

    if strategy == "vector":
        candidates = await _vector_search(
            db, tenant_id=tenant_id, query=query, kb_ids=kb_ids,
            top_k=vector_k, threshold=threshold,
        )
    elif strategy == "keyword":
        candidates = await keyword_retrieve(
            db, tenant_id=tenant_id, query=query, kb_ids=kb_ids, top_k=keyword_k
        )
    else:  # hybrid：双路召回（同一 session 顺序执行，避免并发共用 session）
        v_hits = await _vector_search(
            db, tenant_id=tenant_id, query=query, kb_ids=kb_ids,
            top_k=vector_k, threshold=threshold,
        )
        k_hits = await keyword_retrieve(
            db, tenant_id=tenant_id, query=query, kb_ids=kb_ids, top_k=keyword_k
        )
        candidates = rrf_fuse(v_hits, k_hits)[
            : max(settings.VECTOR_TOP_K, settings.KEYWORD_TOP_K)
        ]

    if not candidates:
        return []

    # ---- Rerank 精排（可选，降级红线：不可用/失败 → 返回粗排结果）----
    if rerank and settings.RERANK_ENABLED:
        try:
            if await is_rerank_available():
                rerank_top_n = max(top_k, settings.RERANK_TOP_N)
                reranked = await _rerank_hits(query, candidates, top_n=rerank_top_n)
                if reranked:
                    return reranked[:top_k]
                metrics.record_rerank("skipped")
            else:
                metrics.record_rerank("skipped")
        except RerankError as exc:
            logger.warning("Rerank 失败，降级返回粗排结果: %s", exc)
            metrics.record_rerank("skipped")

    return candidates[:top_k]


def hits_to_markdown(hits: list[Hit]) -> str:
    """将命中片段拼为 markdown，带策略标注 + 结构化引用锚点。

    格式（AGENTIC_RAG_UPGRADE_SPEC §3.6）：
    [片段 N | 来源: file | 相关度 0.87 | 策略: rerank | 引用: KB-{kb_id}/{chunk_id}]
    前端据此解析锚点渲染可点击的来源徽标，实现答案 → 来源 → 管理页分块闭环。
    """
    if not hits:
        return "（知识库中未检索到相关内容）"
    lines: list[str] = []
    for index, hit in enumerate(hits, start=1):
        location = (
            f"第 {hit.line_start}-{hit.line_end} 行"
            if hit.line_start is not None and hit.line_end is not None
            else "行号不可用"
        )
        heading = hit.heading or "未标注章节"
        doc_id = hit.doc_id if hit.doc_id is not None else "未知"
        lines.append(
            f"[片段 {index} | 来源: {hit.source_file} | 分块: #{hit.chunk_index + 1}"
            f" | 位置: {location} | 章节: {heading} | 文档ID: {doc_id}"
            f" | 相关度 {hit.score}"
            f" | 策略: {hit.strategy} | 引用: KB-{hit.kb_id}/{hit.chunk_id}]\n"
            f"{hit.text}\n"
        )
    return "\n".join(lines)
