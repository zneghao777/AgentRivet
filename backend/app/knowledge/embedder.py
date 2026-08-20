"""Embedding 客户端封装：BGE-M3 本地（OpenAI 兼容，1024 维）。

直接以 httpx 调用 OpenAI 兼容的 /embeddings 接口：
    POST {EMBEDDING_BASE_URL}/embeddings
    {"model": "text-embedding-bge-m3", "input": [text...]}

选择纯 httpx 而非 langchain-openai / openai SDK 的原因：
- openai SDK 默认发送 `encoding_format=float`，部分本地推理服务（LM Studio /
  Ollama 兼容层）会拒绝该参数；自定义 transport 与新版本 httpx 的 timeout 传递
  存在兼容问题。
- 直接控制请求体，可保证兼容任意 OpenAI 格式的服务端。

MUST：输出维度必须与 EMBEDDING_DIM(1024) 一致，不符抛 EmbeddingError（SPEC §1.2）。
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from ..config import settings

logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """Embedding 调用异常（缺少凭据 / 网络错误 / 维度不匹配等）。"""


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.embedding_api_key}",
        "Content-Type": "application/json",
    }


def _validate_dim(vectors: list[list[float]]) -> list[list[float]]:
    """校验向量维度 == EMBEDDING_DIM，不符抛 EmbeddingError。"""
    for vec in vectors:
        if len(vec) != settings.EMBEDDING_DIM:
            raise EmbeddingError(
                f"Embedding 输出维度 {len(vec)} != 配置维度 {settings.EMBEDDING_DIM}，"
                "请检查 EMBEDDING_MODEL / EMBEDDING_DIM 配置（BGE-M3 为 1024）"
            )
    return vectors


async def _post_embeddings(texts: list[str]) -> list[list[float]]:
    """POST /embeddings，返回向量列表。"""
    payload = {"model": settings.EMBEDDING_MODEL, "input": texts}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{settings.embedding_base_url}/embeddings",
                json=payload,
                headers=_headers(),
            )
    except httpx.HTTPError as exc:
        logger.error("Embedding 网络错误: %s", exc)
        raise EmbeddingError(
            "Embedding 服务不可达，请检查 EMBEDDING_BASE_URL / EMBEDDING_API_KEY 配置"
        ) from exc
    if resp.status_code != 200:
        logger.error("Embedding HTTP %s: %s", resp.status_code, resp.text[:300])
        raise EmbeddingError(
            f"Embedding 服务返回错误 HTTP {resp.status_code}，"
            "请检查 EMBEDDING_MODEL 是否与本地服务一致"
        )
    try:
        body = resp.json()
        return [list(map(float, item["embedding"])) for item in body["data"]]
    except (KeyError, ValueError, TypeError) as exc:
        raise EmbeddingError("Embedding 响应格式异常（缺少 data/embedding 字段）") from exc


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量向量化文本（校验维度）。"""
    if not texts:
        return []
    vectors = await _post_embeddings(texts)
    return _validate_dim(vectors)


async def embed_query(query: str) -> list[float]:
    """向量化单个检索 query（校验维度）。"""
    if not query:
        return []
    vectors = await _post_embeddings([query])
    return _validate_dim(vectors)[0]


def _reset() -> None:
    """测试辅助：无状态实现无需重置，保留以兼容旧测试。"""
