"""Graph RAG 图谱模块测试（GRAPH_RAG_SPEC）。

覆盖：实体/关系/事件抽取、实体归一化、社区发现、图谱构建流水线、
图查询原语、知识图谱工具、图谱 API。
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from app.config import settings
from app.harness.context import reset_tool_context, set_tool_context
from app.knowledge.graph.community import (
    build_communities,
    connected_communities,
)
from app.knowledge.graph.extractor import ExtractionResult, _parse_result, extract_batch
from app.knowledge.graph.graph_retriever import graph_retriever
from app.knowledge.graph.resolver import resolve_names
from app.knowledge.graph.service import graph_service
from app.knowledge.service import knowledge_service
from app.storage.db import AsyncSessionLocal
from app.storage.models import DocumentMeta

DOC_MD = (
    "# 凡人修仙传\n\n"
    "## 第一章 拜师\n"
    "韩立拜入七玄门，成为墨大夫的弟子。韩立获得了神秘的掌天瓶。\n\n"
    "## 第二章 结丹\n"
    "韩立突破至结丹期。韩立与紫灵结为道侣。\n"
    "紫灵是合欢宗出身，后离开合欢宗。\n"
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


@pytest.fixture(autouse=True)
def _mock_embeddings(monkeypatch):
    from app.knowledge import ingest as ingest_mod
    from app.knowledge import retriever as retriever_mod
    from app.knowledge.graph import builder as builder_mod
    from app.knowledge.graph import repository as repo_mod

    monkeypatch.setattr(ingest_mod, "embed_texts", _mock_embed_texts)
    monkeypatch.setattr(retriever_mod, "embed_query", lambda q: _mock_vec(q))
    monkeypatch.setattr(repo_mod, "embed_texts", _mock_embed_texts)
    monkeypatch.setattr(builder_mod, "embed_texts", _mock_embed_texts)


@pytest.fixture(autouse=True)
def _mock_llm(monkeypatch):
    """mock 抽取 + 社区 LLM：根据 prompt 内容返回对应 JSON。

    抽取 prompt 含"信息抽取引擎"，社区 prompt 含"知识图谱社区摘要器"。
    extract_batch 与 summarize_community 都在函数内 `from .llm_client import chat_completion`，
    故统一 mock llm_client.chat_completion。
    """
    _EXTRACTION_JSON = json.dumps(
        {
            "entities": [
                {"name": "韩立", "type": "person", "description": "主角"},
                {"name": "韩跑跑", "type": "person", "description": "韩立的绰号"},
                {"name": "紫灵", "type": "person", "description": "韩立的道侣"},
                {"name": "墨大夫", "type": "person", "description": "七玄门医师"},
                {"name": "七玄门", "type": "organization", "description": "门派"},
                {"name": "掌天瓶", "type": "item", "description": "法宝"},
                {"name": "结丹期", "type": "cultivation", "description": "境界"},
            ],
            "relations": [
                {"source": "韩立", "relation": "拜师于", "target": "墨大夫", "evidence": "韩立拜入墨大夫门下"},
                {"source": "韩立", "relation": "属于", "target": "七玄门", "evidence": "韩立是七玄门弟子"},
                {"source": "韩立", "relation": "获得", "target": "掌天瓶", "evidence": "韩立获得掌天瓶"},
                {"source": "韩立", "relation": "修行", "target": "结丹期", "evidence": "韩立突破结丹期"},
                {"source": "韩立", "relation": "结为道侣", "target": "紫灵", "evidence": "韩立与紫灵结为道侣"},
            ],
            "events": [
                {"subject": "韩立", "predicate": "拜入", "object": "七玄门", "summary": "韩立进入七玄门"},
                {"subject": "韩立", "predicate": "突破至", "object": "结丹期", "summary": "韩立结丹成功"},
                {"subject": "韩立", "predicate": "结为道侣", "object": "紫灵", "summary": "韩立与紫灵成婚"},
            ],
        }
    )
    _COMMUNITY_JSON = json.dumps(
        {"summary": "韩立早期师门关系", "keywords": ["韩立", "七玄门"]}
    )

    async def _fake_chat(messages, **kwargs):
        user_text = "\n".join(str(m.get("content", "")) for m in messages)
        if "社区摘要" in user_text or "社区摘要器" in user_text:
            return _COMMUNITY_JSON
        return _EXTRACTION_JSON

    monkeypatch.setattr("app.knowledge.graph.llm_client.chat_completion", _fake_chat)
    return _fake_chat


@pytest.fixture(autouse=True)
def _no_background(monkeypatch):
    """禁用后台调度，测试手动触发。"""
    monkeypatch.setattr("app.knowledge.service._schedule_parse", lambda doc_id: None)
    monkeypatch.setattr(
        "app.knowledge.graph.builder.schedule_build", lambda *a, **k: None
    )


async def _prepare_kb(*, tenant_id: str, name: str | None = None) -> str:
    """创建知识库并同步摄取文档（不打图谱构建，由测试手动触发）。"""
    from app.knowledge.service import _run_parse

    async with AsyncSessionLocal() as db:
        kb = await knowledge_service.create(
            db, tenant_id=tenant_id, name=name or _unique("图库")
        )
        assert kb is not None
        doc = await knowledge_service.upload_document(
            db,
            tenant_id=tenant_id,
            kb_id=kb.id,
            filename="fanren.txt",
            content=DOC_MD.encode("utf-8"),
            parse_method="text",
        )
        await _run_parse(doc.id)
        return str(kb.id)


# ---------- 抽取 ----------


@pytest.mark.asyncio
async def test_extract_batch_returns_structured():
    result = await extract_batch([DOC_MD])
    assert isinstance(result, ExtractionResult)
    assert len(result.entities) >= 5
    assert len(result.relations) >= 3
    assert len(result.events) >= 1


def test_parse_result_filters_low_info():
    data = {
        "entities": [
            {"name": "韩立", "type": "person", "description": "主角"},
            {"name": "天地", "type": "concept", "description": "泛化名词"},
            {"name": "韩跑跑", "type": "person", "description": "绰号"},
        ],
        "relations": [
            {"source": "韩立", "relation": "属于", "target": "七玄门"},  # 七玄门未抽取 → 丢弃
        ],
        "events": [],
    }
    result = _parse_result(data)
    names = [e.name for e in result.entities]
    assert "天地" not in names  # 低信息量被过滤
    assert "韩立" in names
    assert len(result.relations) == 0  # 悬空关系被丢弃


# ---------- 实体归一化 ----------


def test_resolve_names_alias():
    names = ["韩立", "韩跑跑", "紫灵", "紫灵仙子", "墨大夫"]
    resolved = resolve_names(names)
    assert resolved["韩跑跑"] == "韩立"
    assert resolved["紫灵仙子"] == "紫灵"


# ---------- 社区发现 ----------


def test_connected_communities():
    entities = ["a", "b", "c", "d"]
    edges = [("a", "b"), ("b", "c")]
    comps = connected_communities(entities, edges)
    sizes = sorted(len(c) for c in comps)
    assert sizes == [3]  # a,b,c 连通；d 孤立


def test_build_communities_two_levels():
    entities = ["a", "b", "c", "d", "e"]
    edges = [("a", "b"), ("b", "c"), ("d", "e")]
    cands = build_communities(entities, edges)
    levels = {c.level for c in cands}
    assert 0 in levels
    assert 1 in levels


# ---------- 图谱构建 + 检索 ----------


@pytest.mark.asyncio
async def test_build_and_retrieve_full_flow():
    tenant_id = f"graph-{uuid.uuid4().hex[:6]}"
    kb_id = await _prepare_kb(tenant_id=tenant_id)

    from app.knowledge.graph.builder import build_kb_graph

    async with AsyncSessionLocal() as db:
        stats = await build_kb_graph(db, kb_id=uuid.UUID(kb_id), tenant_id=tenant_id, full=True)
        assert stats["entities"] >= 5
        assert stats["relations"] >= 3
        assert stats["events"] >= 1

        # 同一内容、同一图谱版本重复触发时直接复用成功结果，不产生重复任务。
        doc = (
            await db.execute(
                select(DocumentMeta).where(DocumentMeta.kb_id == uuid.UUID(kb_id))
            )
        ).scalar_one()
        idempotent = await graph_service.trigger_document_build(
            db,
            tenant_id=tenant_id,
            kb_id=uuid.UUID(kb_id),
            doc_id=doc.id,
        )
        assert idempotent["status"] == "succeeded"
        assert idempotent["stage"] == "already_current"
        assert idempotent["job_id"] is None

        # 实体精确召回
        result = await graph_retriever.query_entity(
            db, tenant_id=tenant_id, kb_id=uuid.UUID(kb_id), name="韩立", hop=1
        )
        assert result is not None
        assert result.name == "韩立"
        assert len(result.relations) >= 3

        # 2 跳扩散
        expand = await graph_retriever.expand(
            db, tenant_id=tenant_id, kb_id=uuid.UUID(kb_id),
            seed_entity_ids=[result.entity_id], hop=2,
        )
        assert len(expand) > 0

        # 时间线
        events = await graph_retriever.timeline(
            db, tenant_id=tenant_id, kb_id=uuid.UUID(kb_id), entity_name="韩立"
        )
        assert len(events) >= 1
        assert events[0].subject_entity_id is not None

        # 语义检索
        search = await graph_retriever.hybrid_search(
            db, tenant_id=tenant_id, kb_id=uuid.UUID(kb_id), query="韩立的师父", strategy="auto", hop=2
        )
        assert search["entities"] or search["relations"] or search["hits"]


# ---------- 图谱 API ----------


@pytest.mark.asyncio
async def test_graph_api_status_and_search(app_client):
    tenant_id = f"api-{uuid.uuid4().hex[:6]}"
    kb_id = await _prepare_kb(tenant_id=tenant_id)

    from app.knowledge.graph.builder import build_kb_graph

    async with AsyncSessionLocal() as db:
        await build_kb_graph(db, kb_id=uuid.UUID(kb_id), tenant_id=tenant_id, full=True)

    headers = {"X-API-Key": "test-key-1", "X-Tenant-Id": tenant_id}

    # 状态
    resp = await app_client.get(f"/api/v1/kbs/{kb_id}/graph/status", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["graph_status"] == "ready"

    # 实体列表
    resp = await app_client.get(f"/api/v1/kbs/{kb_id}/graph/entities", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] >= 1
    resp = await app_client.get(
        f"/api/v1/kbs/{kb_id}/graph/entities?q=韩立", headers=headers
    )
    assert resp.status_code == 200
    assert any(item["canonical_name"] == "韩立" for item in resp.json()["data"]["items"])
    resp = await app_client.get(
        f"/api/v1/kbs/{kb_id}/graph/entities?q=主角", headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] == 0  # 描述字段不参与实体模糊搜索
    resp = await app_client.get(
        f"/api/v1/kbs/{kb_id}/graph/entities?q=person", headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] == 0  # 类型字段不参与实体模糊搜索

    # 关系 / 事件分页列表
    resp = await app_client.get(
        f"/api/v1/kbs/{kb_id}/graph/relations?page=1&page_size=1", headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] >= 1
    assert len(resp.json()["data"]["items"]) == 1
    assert resp.json()["data"]["items"][0]["source_name"]
    resp = await app_client.get(
        f"/api/v1/kbs/{kb_id}/graph/relations?q=韩立", headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] >= 1

    resp = await app_client.get(
        f"/api/v1/kbs/{kb_id}/graph/events?page=1&page_size=1", headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] >= 1
    assert len(resp.json()["data"]["items"]) == 1
    assert resp.json()["data"]["items"][0]["subject_name"]
    resp = await app_client.get(
        f"/api/v1/kbs/{kb_id}/graph/events?q=韩立", headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] >= 1

    # 语义检索
    resp = await app_client.post(
        f"/api/v1/kbs/{kb_id}/graph/search",
        headers=headers,
        json={"query": "韩立的师父是谁", "strategy": "auto", "hop": 2},
    )
    assert resp.status_code == 200
    assert resp.json()["code"] == 0

    # 越权访问 → 404
    resp = await app_client.get(
        f"/api/v1/kbs/{kb_id}/graph/status",
        headers={"X-API-Key": "test-key-1", "X-Tenant-Id": "other-tenant"},
    )
    assert resp.status_code == 404


# ---------- 图谱工具 ----------


@pytest.mark.asyncio
async def test_graph_tool_registered_and_missing_context():
    from app.tools.registry import tool_registry

    tool_registry.register_builtin()
    tool = tool_registry.get("knowledge_graph_search")
    assert tool is not None

    reset_tool_context()
    result = await tool.ainvoke({"query": "韩立的成长"})
    assert "上下文" in str(result)


@pytest.mark.asyncio
async def test_graph_tool_with_context():
    from app.harness.context import ToolContext
    from app.knowledge.graph.builder import build_kb_graph
    from app.tools.knowledge_graph_search import knowledge_graph_search

    tenant_id = f"tool-g-{uuid.uuid4().hex[:6]}"
    kb_id = await _prepare_kb(tenant_id=tenant_id)

    async with AsyncSessionLocal() as db:
        await build_kb_graph(db, kb_id=uuid.UUID(kb_id), tenant_id=tenant_id, full=True)

    set_tool_context(ToolContext(tenant_id=tenant_id, user_id="u1"))
    result = await knowledge_graph_search.ainvoke(
        {"query": "韩立的师父", "strategy": "auto", "hop": 2}
    )
    text = str(result)
    assert "未检索到" not in text
    assert "韩立" in text or "墨大夫" in text or "关系" in text
