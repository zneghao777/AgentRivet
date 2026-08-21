"""KBX 文本/图谱融合、证据回查与降级测试。"""

import uuid

import pytest

from app.knowledge.retrieval_service import classify_query, retrieve_knowledge
from app.knowledge.retriever import Hit
from app.storage.models import DocumentMeta, KnowledgeBase, KnowledgeChunk


@pytest.mark.parametrize(
    "query",
    [
        "韩立老婆是谁",
        "韩立的妻子是谁",
        "南宫婉是谁的道侣",
        "韩立和南宫婉是什么关系",
    ],
)
def test_relationship_intent_routes_to_graph(query):
    assert classify_query(query) == "graph"


@pytest.mark.asyncio
async def test_relation_query_fuses_channels_by_chunk_id(db_session, monkeypatch):
    tenant_id = f"unified-{uuid.uuid4().hex[:8]}"
    kb = KnowledgeBase(
        tenant_id=tenant_id,
        name=f"graph-{uuid.uuid4().hex[:8]}",
        index_mode="graph_enhanced",
        graph_build_policy="manual",
        index_status="ready",
        graph_status="ready",
        status="active",
    )
    db_session.add(kb)
    await db_session.flush()
    doc = DocumentMeta(
        kb_id=kb.id,
        tenant_id=tenant_id,
        source_file="org.md",
        parse_status="parsed",
        vector_status="ready",
        graph_status="ready",
    )
    db_session.add(doc)
    await db_session.flush()
    chunk = KnowledgeChunk(
        kb_id=kb.id,
        tenant_id=tenant_id,
        doc_id=doc.id,
        source_file="org.md",
        chunk_index=0,
        chunk_text="Alice belongs to Example Org.",
        enabled=True,
    )
    db_session.add(chunk)
    await db_session.commit()

    async def fake_text(*args, **kwargs):
        return [
            Hit(
                text=chunk.chunk_text,
                source_file=chunk.source_file,
                chunk_index=0,
                score=0.9,
                kb_id=kb.id,
                chunk_id=chunk.id,
                strategy="hybrid",
                doc_id=doc.id,
            )
        ]

    async def fake_graph(*args, **kwargs):
        return {
            "entities": [],
            "relations": [
                {
                    "source_entity_id": str(uuid.uuid4()),
                    "source_name": "Alice",
                    "target_entity_id": str(uuid.uuid4()),
                    "target_name": "Example Org",
                    "relation_type": "belongs_to",
                    "weight": 0.8,
                    "source_chunk_id": chunk.id,
                }
            ],
            "communities": [],
            "events": [],
            "evidence_chunk_ids": [chunk.id],
        }

    monkeypatch.setattr("app.knowledge.retrieval_service.retrieve_hybrid", fake_text)
    monkeypatch.setattr(
        "app.knowledge.retrieval_service.graph_retriever.hybrid_search", fake_graph
    )
    result = await retrieve_knowledge(
        db_session,
        tenant_id=tenant_id,
        query="韩立老婆是谁",
        kb_ids=[kb.id],
        mode="auto",
    )
    assert result["resolved_mode"] == "fused"
    assert result["hits"][0]["channels"] == ["text", "graph"]
    assert result["citations"][0]["chunk_id"] == chunk.id
    assert result["graph_context"]["relations"][0]["relation_type"] == "belongs_to"


@pytest.mark.asyncio
async def test_graph_not_ready_degrades_fused_to_text(db_session, monkeypatch):
    tenant_id = f"degrade-{uuid.uuid4().hex[:8]}"
    kb = KnowledgeBase(
        tenant_id=tenant_id,
        name=f"pending-{uuid.uuid4().hex[:8]}",
        index_mode="graph_enhanced",
        graph_build_policy="manual",
        index_status="ready",
        graph_status="pending",
        status="active",
    )
    db_session.add(kb)
    await db_session.commit()

    async def fake_text(*args, **kwargs):
        return []

    monkeypatch.setattr("app.knowledge.retrieval_service.retrieve_hybrid", fake_text)
    result = await retrieve_knowledge(
        db_session,
        tenant_id=tenant_id,
        query="关系问题",
        kb_ids=[kb.id],
        mode="fused",
    )
    assert result["resolved_mode"] == "text"
    assert result["degradations"] == ["graph_not_ready"]
