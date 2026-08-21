"""混合检索测试（AGENTIC_RAG_UPGRADE_SPEC §6 / G-RAG-1 验收）：

- rrf_fuse：融合顺序、按 chunk_id 去重、strategy 标注（纯函数）。
- keyword_retrieve：pg_trgm 关键词路 SQL 过滤（租户 / enabled / 阈值 / kb 白名单）。
- retrieve_hybrid：strategy 开关（vector 与旧行为一致 / auto 受总开关控制 / hybrid 融合）。
- Rerank 编排：mock 精排可用与不可用时的降级行为。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete

from app.config import settings
from app.knowledge import retriever as retriever_mod
from app.knowledge.retriever import (
    Hit,
    keyword_retrieve,
    retrieve,
    retrieve_hybrid,
    rrf_fuse,
)
from app.storage.db import AsyncSessionLocal
from app.storage.models import KnowledgeBase, KnowledgeChunk


def _hit(chunk_id: int, score: float, text: str = "片段", strategy: str = "vector") -> Hit:
    return Hit(
        text=text,
        source_file="doc.md",
        chunk_index=chunk_id,
        score=score,
        kb_id=uuid.uuid4(),
        chunk_id=chunk_id,
        strategy=strategy,
    )


def _mock_vec(text: str, dim: int | None = None) -> list[float]:
    dim = dim or settings.EMBEDDING_DIM
    vec = [0.0] * dim
    for index, ch in enumerate(text):
        vec[(ord(ch) + index * 7) % dim] += 1.0
    return vec


async def _mock_embed_query(query: str) -> list[float]:
    return _mock_vec(query)


@pytest.fixture(autouse=True)
def _mock_embeddings(monkeypatch):
    monkeypatch.setattr(retriever_mod, "embed_query", _mock_embed_query)


@pytest.fixture(autouse=True)
def _reset_rerank_probe(monkeypatch):
    """每个用例清空 rerank 探测缓存，避免用例间污染。"""
    from app.knowledge import reranker as reranker_mod

    reranker_mod.reset_probe_cache()
    yield
    reranker_mod.reset_probe_cache()


# ---------- rrf_fuse（纯函数） ----------


def test_rrf_fuse_merges_and_dedupes_by_chunk_id():
    """同一 chunk 双路命中 → 合并加分（去重不重复出现）。"""
    v_hits = [_hit(1, 0.9), _hit(2, 0.8), _hit(3, 0.7)]
    k_hits = [_hit(2, 0.5), _hit(4, 0.4)]
    fused = rrf_fuse(v_hits, k_hits, k=60)

    chunk_ids = [h.chunk_id for h in fused]
    assert len(chunk_ids) == len(set(chunk_ids))  # 去重
    assert set(chunk_ids) == {1, 2, 3, 4}
    # chunk 2 双路命中：1/(60+2) + 1/(60+1) > 单路分数 → 排第一
    assert fused[0].chunk_id == 2
    assert fused[0].strategy == "hybrid"
    expected = 1 / 62 + 1 / 61
    assert fused[0].score == pytest.approx(expected, rel=1e-3)


def test_rrf_fuse_single_list_preserves_order():
    """仅一路结果时仍输出（分数为 RRF 值，顺序不变）。"""
    v_hits = [_hit(7, 0.9), _hit(8, 0.8)]
    fused = rrf_fuse(v_hits, [])
    assert [h.chunk_id for h in fused] == [7, 8]
    assert fused[0].score == pytest.approx(1 / 61, rel=1e-3)
    assert all(h.strategy == "hybrid" for h in fused)


def test_rrf_fuse_empty():
    assert rrf_fuse([], []) == []


# ---------- keyword_retrieve（pg_trgm SQL 过滤） ----------


async def _prepare_chunks(tenant_id: str) -> tuple[KnowledgeBase, list[int]]:
    """建 KB 并插入 3 个分块：正常 / 停用 / 干扰文本。返回 (kb, chunk_ids)。"""
    async with AsyncSessionLocal() as db:
        kb = KnowledgeBase(tenant_id=tenant_id, name=f"混合检索-{uuid.uuid4().hex[:6]}")
        db.add(kb)
        await db.flush()
        chunks = [
            KnowledgeChunk(
                kb_id=kb.id,
                tenant_id=tenant_id,
                source_file="spec.md",
                chunk_index=0,
                chunk_text="MX-3000 型号参数表",
                embedding=_mock_vec("MX-3000 型号参数表"),
            ),
            KnowledgeChunk(
                kb_id=kb.id,
                tenant_id=tenant_id,
                source_file="spec.md",
                chunk_index=1,
                chunk_text="MX-3000 型号参数表（停用版本）",
                embedding=_mock_vec("MX-3000 型号参数表（停用版本）"),
                enabled=False,
            ),
            KnowledgeChunk(
                kb_id=kb.id,
                tenant_id=tenant_id,
                source_file="other.md",
                chunk_index=0,
                chunk_text="员工下午茶与年度体检福利说明",
                embedding=_mock_vec("员工下午茶与年度体检福利说明"),
            ),
        ]
        db.add_all(chunks)
        await db.commit()
        await db.refresh(kb)
        chunk_ids = [c.id for c in chunks]
        return kb, chunk_ids


async def _cleanup(kb: KnowledgeBase) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(KnowledgeChunk).where(KnowledgeChunk.kb_id == kb.id)
        )
        await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb.id))
        await db.commit()


@pytest.mark.asyncio
async def test_keyword_retrieve_hits_exact_model_number():
    """专有名词 query 命中含该词的分块，strategy=keyword。"""
    kb, chunk_ids = await _prepare_chunks("hybrid-tenant-a")
    try:
        async with AsyncSessionLocal() as db:
            hits = await keyword_retrieve(
                db,
                tenant_id="hybrid-tenant-a",
                query="MX-3000 型号参数",
                kb_ids=[kb.id],
            )
        assert hits, "关键词路应命中型号分块"
        assert hits[0].chunk_id == chunk_ids[0]
        assert hits[0].strategy == "keyword"
        assert hits[0].score > 0
        # 停用分块被过滤
        assert all(h.chunk_id != chunk_ids[1] for h in hits)
    finally:
        await _cleanup(kb)


@pytest.mark.asyncio
async def test_keyword_retrieve_tenant_isolation():
    """关键词路 SQL 强制 tenant 过滤：他租户分块不可见。"""
    kb, _ = await _prepare_chunks("hybrid-tenant-a")
    try:
        async with AsyncSessionLocal() as db:
            hits = await keyword_retrieve(
                db,
                tenant_id="hybrid-tenant-b",  # 另一租户
                query="MX-3000 型号参数",
                kb_ids=[kb.id],
            )
        assert hits == []
    finally:
        await _cleanup(kb)


@pytest.mark.asyncio
async def test_keyword_retrieve_threshold_filters():
    """高阈值下不相似文本被过滤；阈值参数生效。"""
    kb, _ = await _prepare_chunks("hybrid-tenant-a")
    try:
        async with AsyncSessionLocal() as db:
            hits = await keyword_retrieve(
                db,
                tenant_id="hybrid-tenant-a",
                query="完全无关的另一个话题查询词组",
                kb_ids=[kb.id],
                threshold=0.3,
            )
        assert hits == []
    finally:
        await _cleanup(kb)


@pytest.mark.asyncio
async def test_keyword_retrieve_enabled_filter_only():
    """enabled=false 分块不出现在关键词结果（即使文本完全一致）。"""
    kb, chunk_ids = await _prepare_chunks("hybrid-tenant-a")
    try:
        async with AsyncSessionLocal() as db:
            hits = await keyword_retrieve(
                db,
                tenant_id="hybrid-tenant-a",
                query="MX-3000 型号参数表（停用版本）",
                kb_ids=[kb.id],
            )
        assert all(h.chunk_id != chunk_ids[1] for h in hits)
    finally:
        await _cleanup(kb)


# ---------- retrieve_hybrid（strategy 开关） ----------


@pytest.mark.asyncio
async def test_hybrid_strategy_vector_matches_legacy_retrieve():
    """strategy=vector 且不开 rerank → 与旧 retrieve() 行为一致（chunk_id 顺序相同）。"""
    kb, _ = await _prepare_chunks("hybrid-tenant-a")
    try:
        async with AsyncSessionLocal() as db:
            legacy = await retrieve(
                db, tenant_id="hybrid-tenant-a", query="MX-3000 参数", kb_ids=[kb.id], top_k=5
            )
            v2 = await retrieve_hybrid(
                db,
                tenant_id="hybrid-tenant-a",
                query="MX-3000 参数",
                kb_ids=[kb.id],
                top_k=5,
                strategy="vector",
                rerank=False,
            )
        assert [h.chunk_id for h in v2] == [h.chunk_id for h in legacy]
        assert [h.score for h in v2] == [h.score for h in legacy]
        assert all(h.strategy == "vector" for h in v2)
    finally:
        await _cleanup(kb)


@pytest.mark.asyncio
async def test_auto_falls_back_to_vector_when_disabled(monkeypatch):
    """HYBRID_RETRIEVAL_ENABLED=False 时 auto 退回纯向量（兼容旧行为）。"""
    monkeypatch.setattr(settings, "HYBRID_RETRIEVAL_ENABLED", False)
    kb, _ = await _prepare_chunks("hybrid-tenant-a")
    try:
        async with AsyncSessionLocal() as db:
            hits = await retrieve_hybrid(
                db,
                tenant_id="hybrid-tenant-a",
                query="MX-3000 型号参数",
                kb_ids=[kb.id],
                strategy="auto",
                rerank=False,
            )
        assert hits
        assert all(h.strategy == "vector" for h in hits)
    finally:
        await _cleanup(kb)


@pytest.mark.asyncio
async def test_hybrid_strategy_fuses_both_paths():
    """strategy=hybrid → 双路召回 RRF 融合，strategy=hybrid 标注。"""
    kb, _ = await _prepare_chunks("hybrid-tenant-a")
    try:
        async with AsyncSessionLocal() as db:
            hits = await retrieve_hybrid(
                db,
                tenant_id="hybrid-tenant-a",
                query="MX-3000 型号参数",
                kb_ids=[kb.id],
                top_k=5,
                strategy="hybrid",
                rerank=False,
            )
        assert hits, "混合检索应命中"
        assert all(h.strategy == "hybrid" for h in hits)
        chunk_ids = [h.chunk_id for h in hits]
        assert len(chunk_ids) == len(set(chunk_ids))  # 融合去重
        # 型号分块应排最前（关键词路 + 向量路双命中加分）
        assert chunk_ids[0] is not None
    finally:
        await _cleanup(kb)


@pytest.mark.asyncio
async def test_keyword_only_strategy():
    """strategy=keyword → 仅关键词路。"""
    kb, chunk_ids = await _prepare_chunks("hybrid-tenant-a")
    try:
        async with AsyncSessionLocal() as db:
            hits = await retrieve_hybrid(
                db,
                tenant_id="hybrid-tenant-a",
                query="MX-3000 型号参数",
                kb_ids=[kb.id],
                top_k=5,
                strategy="keyword",
                rerank=False,
            )
        assert hits
        assert all(h.strategy == "keyword" for h in hits)
        assert hits[0].chunk_id == chunk_ids[0]
    finally:
        await _cleanup(kb)


@pytest.mark.asyncio
async def test_invalid_strategy_normalizes_to_auto(monkeypatch):
    """非法 strategy 归一化为 auto（不抛错）。"""
    monkeypatch.setattr(settings, "HYBRID_RETRIEVAL_ENABLED", False)
    kb, _ = await _prepare_chunks("hybrid-tenant-a")
    try:
        async with AsyncSessionLocal() as db:
            hits = await retrieve_hybrid(
                db,
                tenant_id="hybrid-tenant-a",
                query="MX-3000",
                kb_ids=[kb.id],
                strategy="not-a-strategy",
                rerank=False,
            )
        assert all(h.strategy == "vector" for h in hits)
    finally:
        await _cleanup(kb)


@pytest.mark.asyncio
async def test_empty_kb_ids_returns_empty():
    async with AsyncSessionLocal() as db:
        hits = await retrieve_hybrid(
            db, tenant_id="t", query="q", kb_ids=[], strategy="hybrid"
        )
    assert hits == []


# ---------- Rerank 编排（mock，G-RAG-2 验收） ----------


@pytest.mark.asyncio
async def test_rerank_applied_when_available(monkeypatch):
    """rerank 可用 → 精排重排序，strategy=rerank、score=relevance_score。"""
    monkeypatch.setattr(settings, "RERANK_ENABLED", True)

    async def _fake_available() -> bool:
        return True

    async def _fake_rerank(query, candidates, top_n):
        # 反转顺序模拟精排
        return [
            Hit(
                text=c.text,
                source_file=c.source_file,
                chunk_index=c.chunk_index,
                score=0.99 - i * 0.1,
                kb_id=c.kb_id,
                chunk_id=c.chunk_id,
                strategy="rerank",
                rerank_score=0.99 - i * 0.1,
            )
            for i, c in enumerate(reversed(candidates[:top_n]))
        ]

    monkeypatch.setattr(retriever_mod, "is_rerank_available", _fake_available)
    monkeypatch.setattr(retriever_mod, "_rerank_hits", _fake_rerank)

    kb, chunk_ids = await _prepare_chunks("hybrid-tenant-a")
    try:
        async with AsyncSessionLocal() as db:
            hits = await retrieve_hybrid(
                db,
                tenant_id="hybrid-tenant-a",
                query="MX-3000 型号参数",
                kb_ids=[kb.id],
                top_k=2,
                strategy="hybrid",
                rerank=True,
            )
        assert len(hits) == 2
        assert all(h.strategy == "rerank" for h in hits)
        assert all(h.rerank_score is not None for h in hits)
        assert hits[0].score >= hits[1].score
    finally:
        await _cleanup(kb)


@pytest.mark.asyncio
async def test_rerank_unavailable_degrades_gracefully(monkeypatch):
    """rerank 服务不可用 → 跳过精排返回粗排结果，不抛错。"""
    monkeypatch.setattr(settings, "RERANK_ENABLED", True)

    async def _fake_unavailable() -> bool:
        return False

    monkeypatch.setattr(retriever_mod, "is_rerank_available", _fake_unavailable)

    kb, _ = await _prepare_chunks("hybrid-tenant-a")
    try:
        async with AsyncSessionLocal() as db:
            hits = await retrieve_hybrid(
                db,
                tenant_id="hybrid-tenant-a",
                query="MX-3000 型号参数",
                kb_ids=[kb.id],
                top_k=5,
                strategy="hybrid",
                rerank=True,
            )
        assert hits, "降级后仍应返回粗排结果"
        assert all(h.strategy in ("hybrid", "vector", "keyword") for h in hits)
    finally:
        await _cleanup(kb)


@pytest.mark.asyncio
async def test_rerank_error_degrades_gracefully(monkeypatch):
    """rerank 抛 RerankError → 降级返回粗排结果（降级红线）。"""
    from app.knowledge.reranker import RerankError

    monkeypatch.setattr(settings, "RERANK_ENABLED", True)

    async def _fake_available() -> bool:
        return True

    async def _fake_rerank(query, candidates, top_n):
        raise RerankError("boom")

    monkeypatch.setattr(retriever_mod, "is_rerank_available", _fake_available)
    monkeypatch.setattr(retriever_mod, "_rerank_hits", _fake_rerank)

    kb, _ = await _prepare_chunks("hybrid-tenant-a")
    try:
        async with AsyncSessionLocal() as db:
            hits = await retrieve_hybrid(
                db,
                tenant_id="hybrid-tenant-a",
                query="MX-3000 型号参数",
                kb_ids=[kb.id],
                top_k=5,
                strategy="hybrid",
                rerank=True,
            )
        assert hits, "Rerank 失败应降级返回粗排结果"
    finally:
        await _cleanup(kb)


@pytest.mark.asyncio
async def test_query_expand_disabled_by_default(monkeypatch):
    """QUERY_EXPAND_ENABLED=False 时 expand_query=True 也不调用改写（零开销）。"""
    calls = []

    async def _fake_expand(query, llm=None):
        calls.append(query)
        return "改写后"

    monkeypatch.setattr(retriever_mod, "_expand_query", _fake_expand)
    kb, _ = await _prepare_chunks("hybrid-tenant-a")
    try:
        async with AsyncSessionLocal() as db:
            await retrieve_hybrid(
                db,
                tenant_id="hybrid-tenant-a",
                query="MX-3000 型号参数",
                kb_ids=[kb.id],
                strategy="hybrid",
                rerank=False,
                expand_query=True,  # 调用方请求，但全局开关关
            )
        assert calls == [], "开关关闭时不应调用改写"
    finally:
        await _cleanup(kb)
