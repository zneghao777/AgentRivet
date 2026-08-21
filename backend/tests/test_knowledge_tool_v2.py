"""知识检索工具 v2 测试（AGENTIC_RAG_UPGRADE_SPEC §6 / G-RAG-3 验收）：

- 工具 args 透传 strategy / rerank / expand_query（spy 校验）。
- markdown 命中标注带策略（策略: hybrid / 策略: rerank）。
- 默认参数（auto / True / False）保证旧调用兼容。
"""

from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.harness.context import ToolContext, reset_tool_context, set_tool_context
from app.knowledge.service import knowledge_service
from app.storage.db import AsyncSessionLocal
from app.tools.knowledge_search import knowledge_search

DOC_MD = (
    "# 设备手册\n\n"
    "## 交换机规格\n"
    "核心交换机 SW-9500 背板带宽 4.8Tbps，支持 48 口万兆光。\n\n"
    "## 维保服务\n"
    "提供 7x24 远程技术支持，重大故障 4 小时到场。\n"
)


def _unique(name: str) -> str:
    return f"{name}-{uuid.uuid4().hex[:6]}"


def _mock_vec(text: str, dim: int | None = None) -> list[float]:
    dim = dim or settings.EMBEDDING_DIM
    vec = [0.0] * dim
    for index, ch in enumerate(text):
        vec[(ord(ch) + index * 7) % dim] += 1.0
    return vec


async def _mock_embed_texts(texts: list[str]) -> list[list[float]]:
    return [_mock_vec(t) for t in texts]


async def _mock_embed_query(query: str) -> list[float]:
    return _mock_vec(query)


@pytest.fixture(autouse=True)
def _mock_embeddings(monkeypatch):
    from app.knowledge import ingest as ingest_mod
    from app.knowledge import retriever as retriever_mod

    monkeypatch.setattr(ingest_mod, "embed_texts", _mock_embed_texts)
    monkeypatch.setattr(retriever_mod, "embed_query", _mock_embed_query)


@pytest.fixture(autouse=True)
def _clean_context():
    reset_tool_context()
    yield
    reset_tool_context()


@pytest.fixture(autouse=True)
def _no_background_parse(monkeypatch):
    monkeypatch.setattr("app.knowledge.service._schedule_parse", lambda doc_id: None)


@pytest.fixture(autouse=True)
def _reset_rerank_probe(monkeypatch):
    from app.knowledge import reranker as reranker_mod

    reranker_mod.reset_probe_cache()
    yield
    reranker_mod.reset_probe_cache()


async def _prepare_kb(*, tenant_id: str, name: str | None = None) -> str:
    from app.knowledge.service import _run_parse

    async with AsyncSessionLocal() as db:
        kb = await knowledge_service.create(
            db, tenant_id=tenant_id, name=name or _unique("设备手册")
        )
        assert kb is not None
        doc = await knowledge_service.upload_document(
            db,
            tenant_id=tenant_id,
            kb_id=kb.id,
            filename="device.md",
            content=DOC_MD.encode("utf-8"),
            parse_method="text",
        )
        await _run_parse(doc.id)
        return str(kb.id)


@pytest.fixture
def _spy_retrieve_v2(monkeypatch):
    """包装 retrieve_v2 记录调用参数（透传校验用）。"""
    recorded: dict = {}
    original = knowledge_service.retrieve_v2

    async def _spy(db, **kwargs):
        recorded.update(kwargs)
        return await original(db, **kwargs)

    monkeypatch.setattr(knowledge_service, "retrieve_v2", _spy)
    return recorded


@pytest.mark.asyncio
async def test_tool_passes_strategy_and_rerank(_spy_retrieve_v2):
    """工具 args 透传 strategy=hybrid / rerank=False。"""
    await _prepare_kb(tenant_id="tool-v2-tenant")
    set_tool_context(ToolContext(tenant_id="tool-v2-tenant", user_id="u1"))

    result = await knowledge_search.ainvoke(
        {"query": "SW-9500 交换机 背板带宽", "top_k": 5, "strategy": "hybrid", "rerank": False}
    )
    text = str(result)
    assert _spy_retrieve_v2["strategy"] == "hybrid"
    assert _spy_retrieve_v2["rerank"] is False
    # markdown 带策略标注
    assert "策略: hybrid" in text
    assert "引用: KB-" in text
    assert "device.md" in text


@pytest.mark.asyncio
async def test_tool_default_args_backward_compatible(_spy_retrieve_v2):
    """旧式调用（仅 query/top_k）→ 默认 auto/True/False。"""
    await _prepare_kb(tenant_id="tool-v2-tenant")
    set_tool_context(ToolContext(tenant_id="tool-v2-tenant", user_id="u1"))

    result = await knowledge_search.ainvoke({"query": "维保服务 到场时间", "top_k": 3})
    assert _spy_retrieve_v2["strategy"] == "auto"
    assert _spy_retrieve_v2["rerank"] is True
    assert _spy_retrieve_v2["expand_query"] is False
    assert "device.md" in str(result)


@pytest.mark.asyncio
async def test_tool_expand_query_passthrough(_spy_retrieve_v2):
    """expand_query 参数透传（开关关闭时零开销，见 test_hybrid_retrieval）。"""
    await _prepare_kb(tenant_id="tool-v2-tenant")
    set_tool_context(ToolContext(tenant_id="tool-v2-tenant", user_id="u1"))

    await knowledge_search.ainvoke(
        {"query": "那个交换机的背板带宽是多少来着", "expand_query": True}
    )
    assert _spy_retrieve_v2["expand_query"] is True


@pytest.mark.asyncio
async def test_tool_rerank_annotation_with_mock(monkeypatch):
    """mock rerank 可用 → markdown 标注 策略: rerank（工具全链路）。"""
    from app.knowledge import retriever as retriever_mod
    from app.knowledge.retriever import Hit

    monkeypatch.setattr(settings, "RERANK_ENABLED", True)

    async def _fake_available() -> bool:
        return True

    async def _fake_rerank(query, candidates, top_n):
        return [
            Hit(
                text=c.text,
                source_file=c.source_file,
                chunk_index=c.chunk_index,
                score=0.93,
                kb_id=c.kb_id,
                chunk_id=c.chunk_id,
                strategy="rerank",
                rerank_score=0.93,
            )
            for c in candidates[:top_n]
        ]

    monkeypatch.setattr(retriever_mod, "is_rerank_available", _fake_available)
    monkeypatch.setattr(retriever_mod, "_rerank_hits", _fake_rerank)

    await _prepare_kb(tenant_id="tool-v2-tenant")
    set_tool_context(ToolContext(tenant_id="tool-v2-tenant", user_id="u1"))

    result = await knowledge_search.ainvoke(
        {"query": "SW-9500 万兆光", "top_k": 3, "rerank": True}
    )
    text = str(result)
    assert "策略: rerank" in text
    assert "0.93" in text


@pytest.mark.asyncio
async def test_tool_args_schema_exposes_new_params():
    """args schema 暴露 strategy / rerank / expand_query（LLM 自主决策入口）。"""
    schema = knowledge_search.args_schema.model_json_schema()
    props = schema.get("properties", {})
    assert {"strategy", "rerank", "expand_query"} <= set(props)
    assert (
        str(props["strategy"].get("default")) == "auto"
    )
    assert props["rerank"]["default"] is True
    assert props["expand_query"]["default"] is False
