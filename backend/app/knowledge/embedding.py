"""Embedding 客户端封装：OpenAI 兼容协议，幂等单例。

复用 settings 中的 EMBEDDING_* 配置，缺省回退到 LLM_* 配置。
"""

from __future__ import annotations

import asyncio
import logging

from langchain_openai import OpenAIEmbeddings

from ..config import settings

logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """Embedding 调用异常（缺少凭据 / 网络错误 / 维度不匹配等）。"""


_embeddings: OpenAIEmbeddings | None = None
_lock = asyncio.Lock()


def get_embeddings() -> OpenAIEmbeddings:
    """返回全局幂等的 Embeddings 实例（懒加载 + 线程安全）。"""
    global _embeddings
    if _embeddings is None:
        _embeddings = OpenAIEmbeddings(
            model=settings.EMBEDDING_MODEL,
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
        )
    return _embeddings


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量向量化文本。

    MUST：embedding 模型输出维度必须与 EMBEDDING_DIM 一致，
    不一致会在入库/检索时报错（见 SPEC §9）。
    """
    if not texts:
        return []
    try:
        # OpenAI 兼容协议为同步 HTTP，放入线程池避免阻塞事件循环
        vectors = await asyncio.to_thread(get_embeddings().embed_documents, texts)
    except Exception as exc:  # noqa: BLE001
        logger.error("Embedding 批量向量化失败: %s", exc)
        raise EmbeddingError(
            "Embedding 服务调用失败，请检查 EMBEDDING_API_KEY / EMBEDDING_BASE_URL 配置"
        ) from exc
    return [list(map(float, v)) for v in vectors]


async def embed_query(query: str) -> list[float]:
    """向量化单个检索 query。"""
    if not query:
        return []
    try:
        vector = await asyncio.to_thread(get_embeddings().embed_query, query)
    except Exception as exc:  # noqa: BLE001
        logger.error("Embedding 查询向量化失败: %s", exc)
        raise EmbeddingError(
            "Embedding 服务调用失败，请检查 EMBEDDING_API_KEY / EMBEDDING_BASE_URL 配置"
        ) from exc
    return list(map(float, vector))


def _reset() -> None:
    """测试辅助：重置单例。"""
    global _embeddings
    _embeddings = None
