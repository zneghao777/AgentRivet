"""知识库 API v2 测试（AGENTIC_RAG_UPGRADE_SPEC §6 / G-RAG-3 + G-RAG-4 验收）：

- test-search 携带 strategy / rerank → 命中含 strategy 字段；旧请求体兼容。
- evaluate：创建 EvaluationRun，results 含 hit_rate / mrr；expected_doc_ids 宽松匹配；
  judge=True 写 TraceScore（mock LLM）；跨租户 404；evaluations 列表。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.config import settings
from app.observability import audit
from app.storage.db import AsyncSessionLocal
from app.storage.models import AuditLog, EvaluationRun, TraceScore

AUTH = {"X-API-Key": "test-key-1"}
AUTH_A = {**AUTH, "X-Tenant-Id": "tenant-a"}
AUTH_B = {**AUTH, "X-Tenant-Id": "tenant-b"}


def _unique(name: str) -> str:
    return f"{name}-{uuid.uuid4().hex[:6]}"


DOC_MD = (
    "# 产品规格\n\n"
    "## 网关参数\n"
    "智能网关 GW-2000 支持 Wi-Fi 6 双频并发，功耗 12W。\n\n"
    "## 保修政策\n"
    "整机保修两年，电源适配器保修一年。\n"
)


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
    monkeypatch.setattr("app.knowledge.service.embed_texts", _mock_embed_texts)


@pytest.fixture(autouse=True)
async def _init_audit():
    audit.init_audit(lambda: AsyncSessionLocal())
    yield
    await asyncio.sleep(0.3)


async def _create_kb(client, *, tenant: dict[str, str], name: str) -> str:
    resp = await client.post(
        "/api/v1/knowledge-bases", json={"name": name}, headers=tenant
    )
    assert resp.status_code == 200
    return resp.json()["data"]["kb_id"]


async def _upload_doc(
    client, kb_id: str, *, tenant: dict[str, str], filename: str = "spec.md"
) -> dict:
    resp = await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents/upload",
        files={"file": (filename, DOC_MD.encode("utf-8"), "text/markdown")},
        headers=tenant,
    )
    assert resp.status_code == 200
    for _ in range(40):
        await asyncio.sleep(0.05)
        resp = await client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=tenant)
        docs = resp.json()["data"]["documents"]
        match = next((d for d in docs if d["source_file"] == filename), None)
        if match and match["parse_status"] in ("parsed", "failed"):
            assert match["parse_status"] == "parsed", match.get("error")
            return match
    raise AssertionError("后台解析未在预期时间内完成")


async def _list_chunk_ids(client, kb_id: str, *, tenant: dict[str, str]) -> list[int]:
    resp = await client.get(
        f"/api/v1/knowledge-bases/{kb_id}/chunks",
        params={"page_size": 50},
        headers=tenant,
    )
    assert resp.status_code == 200
    return [item["chunk_id"] for item in resp.json()["data"]["items"]]


# ---------- test-search v2 ----------


@pytest.mark.asyncio
async def test_test_search_with_strategy_and_rerank(app_client):
    """test-search 带 strategy/rerank → 命中含 strategy 字段（服务不可用自动降级不报错）。"""
    kb_id = await _create_kb(app_client, tenant=AUTH_A, name=_unique("v2检索库"))
    await _upload_doc(app_client, kb_id, tenant=AUTH_A)

    resp = await app_client.post(
        f"/api/v1/knowledge-bases/{kb_id}/test-search",
        json={"query": "GW-2000 网关参数", "top_k": 5, "strategy": "hybrid", "rerank": True},
        headers=AUTH_A,
    )
    assert resp.status_code == 200
    hits = resp.json()["data"]
    assert len(hits) >= 1
    for hit in hits:
        assert "strategy" in hit
        assert hit["strategy"] in ("vector", "keyword", "hybrid", "rerank")


@pytest.mark.asyncio
async def test_test_search_keyword_strategy(app_client):
    """strategy=keyword → 命中 strategy=keyword（专有名词型号查询）。"""
    kb_id = await _create_kb(app_client, tenant=AUTH_A, name=_unique("v2关键词库"))
    await _upload_doc(app_client, kb_id, tenant=AUTH_A)

    resp = await app_client.post(
        f"/api/v1/knowledge-bases/{kb_id}/test-search",
        json={"query": "GW-2000 双频并发", "top_k": 5, "strategy": "keyword", "rerank": False},
        headers=AUTH_A,
    )
    assert resp.status_code == 200
    hits = resp.json()["data"]
    assert hits, "关键词路应命中型号分块"
    assert all(h["strategy"] == "keyword" for h in hits)


@pytest.mark.asyncio
async def test_test_search_legacy_body_compatible(app_client):
    """旧前端不带新字段（仅 query/top_k）→ 200 正常返回（默认值兼容）。"""
    kb_id = await _create_kb(app_client, tenant=AUTH_A, name=_unique("v2兼容库"))
    await _upload_doc(app_client, kb_id, tenant=AUTH_A)

    resp = await app_client.post(
        f"/api/v1/knowledge-bases/{kb_id}/test-search",
        json={"query": "保修政策", "top_k": 3},
        headers=AUTH_A,
    )
    assert resp.status_code == 200
    hits = resp.json()["data"]
    assert len(hits) >= 1
    assert all("strategy" in h for h in hits)


@pytest.mark.asyncio
async def test_test_search_rerank_path_with_mock(app_client, monkeypatch):
    """mock rerank 可用 → 命中 strategy=rerank 且带 rerank_score（API 全链路）。"""
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
                score=0.88,
                kb_id=c.kb_id,
                chunk_id=c.chunk_id,
                strategy="rerank",
                rerank_score=0.88,
            )
            for c in candidates[:top_n]
        ]

    monkeypatch.setattr(retriever_mod, "is_rerank_available", _fake_available)
    monkeypatch.setattr(retriever_mod, "_rerank_hits", _fake_rerank)

    kb_id = await _create_kb(app_client, tenant=AUTH_A, name=_unique("v2精排库"))
    await _upload_doc(app_client, kb_id, tenant=AUTH_A)

    resp = await app_client.post(
        f"/api/v1/knowledge-bases/{kb_id}/test-search",
        json={"query": "网关 功耗", "top_k": 3, "strategy": "hybrid", "rerank": True},
        headers=AUTH_A,
    )
    assert resp.status_code == 200
    hits = resp.json()["data"]
    assert hits
    assert all(h["strategy"] == "rerank" for h in hits)
    assert all(h["rerank_score"] == 0.88 for h in hits)


# ---------- evaluate / evaluations ----------


@pytest.mark.asyncio
async def test_evaluate_creates_run_with_hit_rate_mrr(app_client):
    """evaluate：逐用例跑 variants 检索，results 含 hit_rate / mrr。"""
    kb_id = await _create_kb(app_client, tenant=AUTH_A, name=_unique("v2评测库"))
    await _upload_doc(app_client, kb_id, tenant=AUTH_A)
    chunk_ids = await _list_chunk_ids(app_client, kb_id, tenant=AUTH_A)
    assert chunk_ids

    resp = await app_client.post(
        f"/api/v1/knowledge-bases/{kb_id}/evaluate",
        json={
            "name": "首轮评测",
            "dataset": [
                {"query": "GW-2000 网关参数 功耗", "expected_chunk_ids": [chunk_ids[0]]},
                {"query": "整机保修多久", "expected_chunk_ids": chunk_ids},
            ],
            "variants": ["baseline", "hybrid"],
        },
        headers=AUTH_A,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["evaluation_id"]
    assert data["status"] == "done"
    results = data["results"]
    assert results["stats"]["valid"] == 2
    variant_map = {v["variant"]: v for v in results["variants"]}
    assert set(variant_map) == {"baseline", "hybrid"}
    for variant in results["variants"]:
        assert 0.0 <= variant["hit_rate"] <= 1.0
        assert 0.0 <= variant["mrr"] <= 1.0
        assert len(variant["cases"]) == 2
    # mock 向量 + 小分块集 → 全命中
    assert variant_map["baseline"]["hit_rate"] == 1.0


@pytest.mark.asyncio
async def test_evaluate_expected_doc_ids_loose_match(app_client):
    """expected_doc_ids 宽松匹配：命中该文档任一分块即算命中。"""
    kb_id = await _create_kb(app_client, tenant=AUTH_A, name=_unique("v2文档评测库"))
    doc = await _upload_doc(app_client, kb_id, tenant=AUTH_A)

    resp = await app_client.post(
        f"/api/v1/knowledge-bases/{kb_id}/evaluate",
        json={
            "name": "文档级评测",
            "dataset": [{"query": "网关 Wi-Fi", "expected_doc_ids": [doc["doc_id"]]}],
            "variants": ["baseline"],
        },
        headers=AUTH_A,
    )
    assert resp.status_code == 200
    results = resp.json()["data"]["results"]
    assert results["variants"][0]["hit_rate"] == 1.0


@pytest.mark.asyncio
async def test_evaluate_invalid_cases_skipped(app_client):
    """非法用例（缺 query / 缺 expected）跳过并计数。"""
    kb_id = await _create_kb(app_client, tenant=AUTH_A, name=_unique("v2跳过库"))
    await _upload_doc(app_client, kb_id, tenant=AUTH_A)

    resp = await app_client.post(
        f"/api/v1/knowledge-bases/{kb_id}/evaluate",
        json={
            "name": "含非法用例",
            "dataset": [
                {"query": "合法用例", "expected_doc_ids": [1]},
                {"query": ""},
                {"query": "无目标"},
            ],
        },
        headers=AUTH_A,
    )
    assert resp.status_code == 200
    results = resp.json()["data"]["results"]
    assert results["stats"] == {"total": 3, "valid": 1, "skipped": 2}
    assert len(results["variants"][0]["cases"]) == 1


@pytest.mark.asyncio
async def test_evaluate_judge_writes_trace_scores(app_client, monkeypatch):
    """judge=True → LLM-judge 分数写入 TraceScore（source=rag_eval，mock LLM）。"""
    from app.knowledge import evaluator as evaluator_mod

    async def _fake_judge(query, contexts, answer, llm=None):
        return {"faithfulness": 4.5, "relevancy": 5.0}

    async def _fake_answer(query, contexts, llm=None):
        return "生成的回答"

    monkeypatch.setattr(evaluator_mod, "judge_answer", _fake_judge)
    monkeypatch.setattr(evaluator_mod, "generate_answer", _fake_answer)

    kb_id = await _create_kb(app_client, tenant=AUTH_A, name=_unique("v2judge库"))
    await _upload_doc(app_client, kb_id, tenant=AUTH_A)

    resp = await app_client.post(
        f"/api/v1/knowledge-bases/{kb_id}/evaluate",
        json={
            "name": "judge 评测",
            "dataset": [
                {"query": "网关功耗多少", "expected_doc_ids": [1]},
                {"query": "保修政策", "expected_doc_ids": [1], "reference_answer": "两年"},
            ],
            "variants": ["baseline"],
            "judge": True,
        },
        headers=AUTH_A,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    judge = data["results"]["judge"]
    assert judge["avg_faithfulness"] == 4.5
    assert judge["avg_relevancy"] == 5.0
    assert len(judge["cases"]) == 2

    evaluation_id = data["evaluation_id"]
    async with AsyncSessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(TraceScore).where(
                        TraceScore.tenant_id == "tenant-a",
                        TraceScore.source == "rag_eval",
                    )
                )
            )
            .scalars()
            .all()
        )
    this_run = [r for r in rows if r.trace_id.startswith(f"rag-eval-{evaluation_id}-")]
    assert len(this_run) == 4  # 2 用例 × (faithfulness + relevancy)
    names = {r.score_name for r in this_run}
    assert names == {"faithfulness", "relevancy"}


@pytest.mark.asyncio
async def test_evaluate_cross_tenant_404(app_client):
    """跨租户 evaluate → 404（不泄露存在性）。"""
    kb_id = await _create_kb(app_client, tenant=AUTH_A, name=_unique("v2私有库"))

    resp = await app_client.post(
        f"/api/v1/knowledge-bases/{kb_id}/evaluate",
        json={"name": "越权评测", "dataset": [{"query": "q", "expected_doc_ids": [1]}]},
        headers=AUTH_B,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_evaluations(app_client):
    """evaluations 列表：仅返回该 kb 的评测记录。"""
    kb_id = await _create_kb(app_client, tenant=AUTH_A, name=_unique("v2列表库"))
    other_kb = await _create_kb(app_client, tenant=AUTH_A, name=_unique("v2他库"))
    await _upload_doc(app_client, kb_id, tenant=AUTH_A)

    await app_client.post(
        f"/api/v1/knowledge-bases/{kb_id}/evaluate",
        json={"name": "评测A", "dataset": [{"query": "网关", "expected_doc_ids": [1]}]},
        headers=AUTH_A,
    )

    resp = await app_client.get(
        f"/api/v1/knowledge-bases/{kb_id}/evaluations", headers=AUTH_A
    )
    assert resp.status_code == 200
    items = resp.json()["data"]
    assert len(items) == 1
    assert items[0]["name"] == "评测A"
    assert items[0]["results"]["variants"]

    # 其他 kb 无记录
    resp = await app_client.get(
        f"/api/v1/knowledge-bases/{other_kb}/evaluations", headers=AUTH_A
    )
    assert resp.json()["data"] == []


@pytest.mark.asyncio
async def test_evaluate_audit_recorded(app_client):
    """evaluate 触发 knowledge.evaluate 审计。"""
    kb_id = await _create_kb(app_client, tenant=AUTH_A, name=_unique("v2审计库"))
    await _upload_doc(app_client, kb_id, tenant=AUTH_A)

    await app_client.post(
        f"/api/v1/knowledge-bases/{kb_id}/evaluate",
        json={"name": "审计评测", "dataset": [{"query": "网关", "expected_doc_ids": [1]}]},
        headers=AUTH_A,
    )
    await asyncio.sleep(0.3)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AuditLog.action).where(
                AuditLog.tenant_id == "tenant-a",
                AuditLog.action == "knowledge.evaluate",
            )
        )
        actions = {row[0] for row in result.fetchall()}
    assert "knowledge.evaluate" in actions


@pytest.mark.asyncio
async def test_evaluation_run_persisted(app_client):
    """EvaluationRun 落库：dataset / variants / results 完整。"""
    kb_id = await _create_kb(app_client, tenant=AUTH_A, name=_unique("v2落库库"))
    await _upload_doc(app_client, kb_id, tenant=AUTH_A)

    resp = await app_client.post(
        f"/api/v1/knowledge-bases/{kb_id}/evaluate",
        json={"name": "落库验证", "dataset": [{"query": "网关", "expected_doc_ids": [1]}]},
        headers=AUTH_A,
    )
    evaluation_id = resp.json()["data"]["evaluation_id"]

    async with AsyncSessionLocal() as db:
        run = await db.get(EvaluationRun, evaluation_id)
    assert run is not None
    assert run.tenant_id == "tenant-a"
    assert run.status == "done"
    assert run.dataset["kb_id"] == kb_id
    assert run.variants == ["baseline", "hybrid", "hybrid+rerank"]
    assert run.results["variants"]
