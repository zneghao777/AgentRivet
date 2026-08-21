"""图查询 / 检索原语（GRAPH_RAG_SPEC §4.2）。

提供 Cypher-free 的语义入口：
- query_entity: 实体精确/向量召回 → 1 跳邻居关系
- expand: 递归 CTE 2~3 跳图扩散，收集证据 chunk
- community_summary: 向量召回相关社区 → 聚合社区摘要
- timeline: 实体事件时间线

结果统一携带证据 chunk_id，便于与向量通道融合（§4.4）。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from .models import GraphEvent
from .repository import repository

logger = logging.getLogger(__name__)


@dataclass
class EntityResult:
    """实体查询结果：实体 + 邻居关系 + 证据 chunk。"""

    entity_id: str
    name: str
    canonical_name: str
    entity_type: str
    description: str | None = None
    neighbors: list[dict] = field(default_factory=list)
    relations: list[dict] = field(default_factory=list)
    chunk_ids: list[int] = field(default_factory=list)


@dataclass
class GraphHit:
    """图谱通道的一个命中（统一承载证据 chunk 与图信息）。"""

    text: str  # 实体名/关系描述/社区摘要等
    score: float
    kind: str  # entity | relation | community | timeline
    source_chunk_id: int | None = None
    entity_id: str | None = None
    kb_id: str | None = None
    extra: dict = field(default_factory=dict)


class GraphRetriever:
    """图谱检索器（GraphRetriever，GRAPH_RAG_SPEC §4.2）。"""

    async def query_entity(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        name: str,
        hop: int = 1,
    ) -> EntityResult | None:
        """实体精确 + 向量召回 → 返回实体 + 1~N 跳邻居关系。"""
        entity = await repository.get_entity_by_name(
            db, tenant_id=tenant_id, kb_id=kb_id, name=name
        )
        if entity is None:
            candidates = await repository.search_entities(
                db, tenant_id=tenant_id, kb_id=kb_id, query=name, top_k=1
            )
            if not candidates:
                return None
            entity = candidates[0]

        neighbors = await repository.neighbors(
            db, tenant_id=tenant_id, kb_id=kb_id, entity_id=entity.id, hop=hop
        )
        neighbor_ids = [uuid.UUID(n.entity_id) for n in neighbors]
        edges = await repository.relations_between(
            db,
            tenant_id=tenant_id,
            kb_id=kb_id,
            entity_ids=[entity.id, *neighbor_ids],
        )
        return EntityResult(
            entity_id=str(entity.id),
            name=entity.name,
            canonical_name=entity.canonical_name,
            entity_type=entity.entity_type,
            description=entity.description,
            neighbors=[
                {
                    "entity_id": n.entity_id,
                    "name": n.name,
                    "canonical_name": n.canonical_name,
                    "entity_type": n.entity_type,
                    "relation_type": n.relation_type,
                    "hop": n.hop,
                    "source_chunk_id": n.source_chunk_id,
                }
                for n in neighbors
            ],
            relations=[
                {
                    "relation_id": e.relation_id,
                    "source_entity_id": e.source_entity_id,
                    "source_name": e.source_name,
                    "target_entity_id": e.target_entity_id,
                    "target_name": e.target_name,
                    "relation_type": e.relation_type,
                    "description": e.description,
                    "weight": e.weight,
                    "source_chunk_id": e.source_chunk_id,
                }
                for e in edges
            ],
            chunk_ids=[int(c) for c in (entity.chunk_ids or [])],
        )

    async def expand(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        seed_entity_ids: list[str],
        hop: int = 2,
    ) -> list[GraphHit]:
        """递归 CTE 沿关系边做 2~3 跳扩散，收集证据 chunk。"""
        hits: list[GraphHit] = []
        seen_chunks: set[int] = set()
        for seed in seed_entity_ids:
            try:
                seed_uuid = uuid.UUID(str(seed))
            except ValueError:
                continue
            nodes = await repository.neighbors(
                db, tenant_id=tenant_id, kb_id=kb_id, entity_id=seed_uuid, hop=hop
            )
            for node in nodes:
                if node.source_chunk_id is not None and node.source_chunk_id not in seen_chunks:
                    seen_chunks.add(node.source_chunk_id)
                hits.append(
                    GraphHit(
                        text=f"{node.name}（{node.entity_type}）"
                        + (f"，关系：{node.relation_type}" if node.relation_type else ""),
                        score=max(0.0, 1.0 - node.hop * 0.3),
                        kind="entity",
                        source_chunk_id=node.source_chunk_id,
                        entity_id=node.entity_id,
                        kb_id=str(kb_id),
                        extra={"hop": node.hop, "relation_type": node.relation_type},
                    )
                )
        return hits

    async def community_summary(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        question: str,
        top_k: int = 5,
    ) -> list[GraphHit]:
        """先向量召回相关社区，聚合社区摘要回答问题（全局性问题）。"""
        communities = await repository.search_communities(
            db, tenant_id=tenant_id, kb_id=kb_id, query=question, top_k=top_k
        )
        hits: list[GraphHit] = []
        for community in communities:
            if not community.summary:
                continue
            hits.append(
                GraphHit(
                    text=community.summary,
                    score=1.0,
                    kind="community",
                    source_chunk_id=None,
                    kb_id=str(kb_id),
                    extra={"level": community.level, "keywords": community.keywords or []},
                )
            )
        return hits

    async def timeline(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        entity_name: str,
    ) -> list[GraphEvent]:
        """按 order_no 返回实体的事件时间线。"""
        return await repository.timeline(
            db, tenant_id=tenant_id, kb_id=kb_id, entity_name=entity_name
        )

    async def hybrid_search(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        query: str,
        strategy: str = "auto",
        hop: int = 2,
        top_k: int = 10,
    ) -> dict:
        """统一图检索入口（供 Agent 工具 / API 使用，GRAPH_RAG_SPEC §4.3）。

        strategy: auto | entity | hop | global | timeline
        - auto: 先用实体召回判断是否含明确实体；含实体走图扩散，否则走社区摘要。
        - entity: 仅实体 1 跳召回。
        - hop: 实体召回 → 图扩散。
        - global: 社区摘要。
        - timeline: 实体事件时间线。

        返回 {entities, relations, communities, events, evidence_chunk_ids, hits}。
        """
        strategy = strategy or "auto"
        # 先做实体召回（精确 + 向量）
        candidates = await repository.search_entities(
            db, tenant_id=tenant_id, kb_id=kb_id, query=query, top_k=top_k
        )
        entity_hits: list[GraphHit] = []
        for e in candidates[:top_k]:
            entity_hits.append(
                GraphHit(
                    text=f"{e.name}（{e.entity_type}）"
                    + (f"：{e.description}" if e.description else ""),
                    score=1.0,
                    kind="entity",
                    source_chunk_id=int(e.chunk_ids[0]) if e.chunk_ids else None,
                    entity_id=str(e.id),
                    kb_id=str(kb_id),
                    extra={"canonical_name": e.canonical_name},
                )
            )

        results: dict = {
            "entities": [
                {
                    "id": str(entity.id),
                    "kb_id": str(entity.kb_id),
                    "doc_id": entity.doc_id,
                    "entity_type": entity.entity_type,
                    "name": entity.name,
                    "canonical_name": entity.canonical_name,
                    "description": entity.description,
                    "chunk_ids": [int(chunk_id) for chunk_id in (entity.chunk_ids or [])],
                }
                for entity in candidates[:top_k]
            ],
            "relations": [],
            "communities": [],
            "events": [],
            "evidence_chunk_ids": [],
            "hits": entity_hits,
            "strategy": strategy,
        }

        # auto 策略路由：有明确实体 → 图扩散；否则社区摘要
        if strategy == "auto":
            if candidates:
                strategy = "hop"
            else:
                strategy = "global"

        if strategy in ("entity", "hop"):
            seed_ids = [str(e.id) for e in candidates[:3]]
            if seed_ids:
                expand_hits = await self.expand(
                    db,
                    tenant_id=tenant_id,
                    kb_id=kb_id,
                    seed_entity_ids=seed_ids,
                    hop=max(1, hop),
                )
                results["hits"].extend(expand_hits)
            # 收集关系边（供前端渲染）
            if candidates:
                all_ids = [e.id for e in candidates]
                for n in entity_hits:
                    if n.entity_id:
                        all_ids.append(uuid.UUID(n.entity_id))
                edges = await repository.relations_between(
                    db, tenant_id=tenant_id, kb_id=kb_id, entity_ids=list(dict.fromkeys(all_ids))
                )
                results["relations"] = [
                    {
                        "source_entity_id": e.source_entity_id,
                        "source_name": e.source_name,
                        "target_entity_id": e.target_entity_id,
                        "target_name": e.target_name,
                        "relation_type": e.relation_type,
                        "description": e.description,
                        "weight": e.weight,
                        "source_chunk_id": e.source_chunk_id,
                    }
                    for e in edges
                ]

        if strategy == "global":
            comm_hits = await self.community_summary(
                db, tenant_id=tenant_id, kb_id=kb_id, question=query, top_k=top_k
            )
            results["hits"].extend(comm_hits)
            results["communities"] = [
                {"summary": h.text, "keywords": h.extra.get("keywords", []), "level": h.extra.get("level")}
                for h in comm_hits
            ]

        if strategy == "timeline":
            # 尝试以 query 作为实体名取时间线
            events = await self.timeline(
                db, tenant_id=tenant_id, kb_id=kb_id, entity_name=query
            )
            results["events"] = [
                {
                    "event_id": str(ev.id),
                    "order_no": ev.order_no,
                    "predicate": ev.predicate,
                    "object": ev.object,
                    "summary": ev.summary,
                    "event_type": ev.event_type,
                    "source_chunk_id": ev.source_chunk_id,
                }
                for ev in events
            ]

        # 汇总证据 chunk_id 去重
        results["strategy"] = strategy
        seen: set[int] = set()
        for h in results["hits"]:
            if h.source_chunk_id is not None:
                seen.add(h.source_chunk_id)
        results["evidence_chunk_ids"] = sorted(seen)
        return results


graph_retriever = GraphRetriever()
