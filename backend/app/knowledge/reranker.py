"""Rerank 精排封装：OpenAI 兼容 /rerank HTTP 端点（bge-reranker-v2-m3）。

AGENTIC_RAG_UPGRADE_SPEC §1.2 / §3.4：
- 首选 HTTP 端点：POST {RERANK_BASE_URL}/rerank {"model","query","documents","top_n"}
  → {"results": [{"index", "relevance_score"}]}。
- 探测缓存：is_rerank_available() 结果缓存 60s，避免每次检索都探测。
- 降级红线：服务不可达 / 未配置 / 响应异常 → 抛 RerankError，由调用方
  （retriever.retrieve_hybrid）降级返回粗排结果，绝不阻塞主流程。
- 进程内 FlagEmbedding 后端仅在 RERANK_USE_FLAG_EMBEDDING=True 且依赖可选时启用。
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import TYPE_CHECKING

import httpx

from ..config import settings
from ..observability.metrics import metrics

if TYPE_CHECKING:  # 仅为类型标注，避免与 retriever 循环导入
    from .retriever import Hit

logger = logging.getLogger(__name__)

# 探测缓存（AGENTIC_RAG_UPGRADE_SPEC §3.4：缓存探测结果，如 60s）
_PROBE_TTL_SECONDS = 60.0
_available_cache: bool | None = None
_available_checked_at: float = 0.0


class RerankError(Exception):
    """Rerank 调用异常（服务不可达 / HTTP 错误 / 响应格式异常等）。"""


def _headers() -> dict[str, str]:
    return {"Content-Type": "application/json"}


def reset_probe_cache() -> None:
    """测试辅助：清空探测缓存。"""
    global _available_cache, _available_checked_at
    _available_cache = None
    _available_checked_at = 0.0


async def is_rerank_available() -> bool:
    """探测 Rerank 服务可用性，结果缓存 60s。

    - RERANK_ENABLED=False 或 RERANK_BASE_URL 为空 → 恒 False（零开销）。
    - 探测方式：GET {base}/models（OpenAI 兼容服务通用轻量端点）。
    """
    global _available_cache, _available_checked_at
    if not settings.RERANK_ENABLED:
        return False
    if not settings.RERANK_BASE_URL:
        return False
    now = time.monotonic()
    if _available_cache is not None and now - _available_checked_at < _PROBE_TTL_SECONDS:
        return _available_cache
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(
                f"{settings.RERANK_BASE_URL}/models", headers=_headers()
            )
        ok = resp.status_code == 200
    except httpx.HTTPError:
        ok = False
    _available_cache = ok
    _available_checked_at = now
    if not ok:
        logger.debug("Rerank 服务不可用（%s），本周期内将跳过精排", settings.RERANK_BASE_URL)
    return ok


async def rerank(query: str, candidates: list[Hit], top_n: int) -> list[Hit]:
    """对粗排候选做 cross-encoder 精排，返回按 relevance_score 降序的前 top_n Hit。

    - index 映射回候选列表，保留 chunk_id / kb_id 溯源主键。
    - 失败抛 RerankError，由调用方降级。
    - 指标：kb_rerank_total{status=ok|error} / kb_rerank_latency_ms。
    """
    if not candidates:
        return []
    if settings.RERANK_USE_FLAG_EMBEDDING:
        return await _rerank_flag_embedding(query, candidates, top_n)
    return await _rerank_http(query, candidates, top_n)


async def _rerank_http(query: str, candidates: list[Hit], top_n: int) -> list[Hit]:
    """HTTP /rerank 端点（OpenAI 兼容）。"""
    payload = {
        "model": settings.RERANK_MODEL,
        "query": query,
        "documents": [c.text for c in candidates],
        "top_n": top_n,
    }
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=settings.RERANK_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{settings.RERANK_BASE_URL}/rerank", json=payload, headers=_headers()
            )
    except httpx.HTTPError as exc:
        metrics.record_rerank("error")
        raise RerankError(f"Rerank 服务不可达: {exc}") from exc
    latency_ms = (time.perf_counter() - started) * 1000
    if resp.status_code != 200:
        metrics.record_rerank("error")
        raise RerankError(f"Rerank 服务返回 HTTP {resp.status_code}")
    try:
        body = resp.json()
        results = body["results"]
    except (KeyError, ValueError, TypeError) as exc:
        metrics.record_rerank("error")
        raise RerankError("Rerank 响应格式异常（缺少 results 字段）") from exc

    ranked: list[Hit] = []
    for item in results:
        try:
            index = int(item["index"])
            score = float(item.get("relevance_score", 0.0))
        except (KeyError, ValueError, TypeError) as exc:
            raise RerankError("Rerank 响应 results 项格式异常") from exc
        if not 0 <= index < len(candidates):
            continue
        hit = candidates[index]
        ranked.append(
            replace(
                hit,
                score=round(score, 4),
                strategy="rerank",
                rerank_score=round(score, 4),
            )
        )
    ranked.sort(key=lambda h: h.score, reverse=True)
    metrics.record_rerank("ok", latency_ms)
    return ranked[:top_n]


async def _rerank_flag_embedding(
    query: str, candidates: list[Hit], top_n: int
) -> list[Hit]:
    """进程内 FlagEmbedding 后端（可选依赖，默认不启用）。"""
    started = time.perf_counter()
    try:
        # 可选依赖：requirements.txt 中标注 optional，导入失败即降级
        from FlagEmbedding import FlagReranker  # type: ignore[import-not-found]
    except ImportError as exc:
        metrics.record_rerank("error")
        raise RerankError("FlagEmbedding 未安装（可选依赖）") from exc
    try:
        reranker = FlagReranker(settings.RERANK_MODEL, use_fp16=True)
        pairs = [[query, c.text] for c in candidates]
        scores = reranker.compute_score(pairs)  # type: ignore[union-attr]
        if not isinstance(scores, list):
            scores = [scores]
    except Exception as exc:  # noqa: BLE001
        metrics.record_rerank("error")
        raise RerankError(f"FlagEmbedding 精排失败: {exc}") from exc
    latency_ms = (time.perf_counter() - started) * 1000
    ranked = [
        replace(c, score=round(float(s), 4), strategy="rerank", rerank_score=round(float(s), 4))
        for c, s in zip(candidates, scores, strict=False)
    ]
    ranked.sort(key=lambda h: h.score, reverse=True)
    metrics.record_rerank("ok", latency_ms)
    return ranked[:top_n]
