"""MemoryService 门面（MEMORY_SPEC §6/§7）。

- 写入：extract_from_conversation() —— 提炼 → 调和（ADD/UPDATE/MERGE/NOOP）→ 向量化入库
- 读取：retrieve_relevant() —— embed + pgvector（tenant/user 过滤 + 时间加权）
- 管理：list / timeline / overview / update / delete / clear / settings
- 卫生：去重 / 冲突调和 / TTL 过期 / 强化

🔒 安全红线：所有 SQL 同时带 tenant_id + user_id 过滤；越权返回 None/空，接口层转 404。
"""

from __future__ import annotations

import logging
import math
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..knowledge.embedder import EmbeddingError, embed_query, embed_texts
from ..storage.models import MemoryEntry, MemorySettings, Session
from . import extractor, retriever
from .hygiene import (
    ConsolidateResult,
    auto_extract_enabled,
    expire_stale,
    get_or_create_settings,
    long_term_enabled,
    resolve_duplicate,
    should_be_pending,
    strengthen,
)

logger = logging.getLogger(__name__)


class MemoryError(Exception):
    """记忆业务异常（embedding 失败等）。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _retrieve_similar(
    db: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    content: str,
    top_k: int = 5,
) -> list[Any]:
    """检索相似 active 记忆（供调和用），返回带 .similarity 的 MemoryEntry 列表。"""
    qvec = await embed_query(content)
    if not qvec:
        return []
    qvec_literal = "[" + ",".join(str(v) for v in qvec) + "]"
    sql = f"""
        SELECT id, content, memory_type, status, confidence,
               1 - (embedding <=> CAST(:qvec AS vector)) AS similarity,
               source_summary, access_count, last_accessed_at, created_at, updated_at
        FROM memory_entries
        WHERE tenant_id = :tenant AND user_id = :user AND status = 'active'
          AND embedding IS NOT NULL
          AND 1 - (embedding <=> CAST(:qvec AS vector)) >= :threshold
        ORDER BY embedding <=> CAST(:qvec AS vector)
        LIMIT :top_k
    """
    params = {
        "qvec": qvec_literal,
        "tenant": tenant_id,
        "user": user_id,
        "threshold": settings.MEMORY_SIM_CONFLICT,
        "top_k": top_k,
    }
    result = await db.execute(text(sql), params)
    return list(result.fetchall())


async def _decide_conflict(
    candidate: dict,
    old_content: str,
) -> str:
    """冲突调和 LLM 裁决（MEMORY_SPEC §6.1 步骤 3）：ADD / UPDATE / MERGE。

    仅相似度落在 0.70–0.92 区间触发；失败回退为 ADD（保守新增）。
    """
    prompt = (
        "以下是用户的两条长期记忆，判断新记忆与旧记忆的关系。\n"
        f"旧记忆：{old_content}\n"
        f"新记忆：{candidate['content']}\n"
        '请只输出一个词：ADD（新增，二者独立）/ UPDATE（新记忆覆盖旧记忆）/ '
        'MERGE（合并文本，两者都保留）。'
    )
    try:
        from langchain_openai import ChatOpenAI

        llm = ChatOpenAI(
            model=settings.LLM_MODEL,
            base_url=settings.LLM_BASE_URL,
            api_key=settings.LLM_API_KEY,
            temperature=0,
            max_tokens=64,
        )
        resp = await llm.ainvoke(prompt)
        decision = (resp.content or "").strip().upper()
        if decision in ("ADD", "UPDATE", "MERGE"):
            return decision
    except Exception as exc:  # noqa: BLE001
        logger.warning("冲突裁决 LLM 调用失败，回退 ADD: %s", exc)
    return "ADD"


class MemoryService:
    """长期记忆服务门面。"""

    # ---------- 写入（MEMORY_SPEC §6.1：extract-consolidate） ----------

    async def extract_from_conversation(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        user_id: str,
        user_msg: str,
        assistant_msg: str,
        session_id: uuid.UUID | None = None,
        session_title: str | None = None,
    ) -> int:
        """对话结束后后台提炼并调和入库。返回新增/更新的记忆条数。

        流程：extract → 逐条调和（NOOP/ADD/UPDATE/MERGE）→ 向量化入库。
        任何失败只记录日志，不影响对话主链路。
        """
        if not settings.MEMORY_ENABLED:
            return 0
        if not await long_term_enabled(db, tenant_id=tenant_id, user_id=user_id):
            return 0
        if not await auto_extract_enabled(db, tenant_id=tenant_id, user_id=user_id):
            return 0

        candidates = await extractor.extract(user_msg, assistant_msg, session_title)
        if not candidates:
            return 0

        written = 0
        for cand in candidates:
            try:
                result = await self._consolidate_one(
                    db,
                    candidate=cand,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_id=session_id,
                    session_title=session_title,
                )
                if result.action in ("ADD", "UPDATE", "MERGE"):
                    written += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("记忆调和失败 tenant=%s user=%s: %s", tenant_id, user_id, exc)
        await db.commit()
        return written

    async def _consolidate_one(
        self,
        db: AsyncSession,
        *,
        candidate: dict,
        tenant_id: str,
        user_id: str,
        session_id: uuid.UUID | None,
        session_title: str | None,
    ) -> ConsolidateResult:
        """单条候选的调和：判重 → 冲突裁决 → 入库。"""
        # 低置信度 → pending（不参与检索，管理页可人工确认）
        if should_be_pending(candidate["confidence"]):
            await self._insert(
                db,
                tenant_id=tenant_id,
                user_id=user_id,
                content=candidate["content"],
                memory_type=candidate["memory_type"],
                confidence=candidate["confidence"],
                status="pending",
                session_id=session_id,
                source_summary=candidate.get("source_summary"),
            )
            return ConsolidateResult(action="PENDING", content=candidate["content"], confidence=candidate["confidence"])

        # 检索相似记忆（复用 embedding，一次向量化缓存）
        similar = await _retrieve_similar(
            db, tenant_id=tenant_id, user_id=user_id, content=candidate["content"], top_k=5
        )
        hits = [
            _RowLike(
                memory_id=r[0],
                content=r[1],
                similarity=r[5],
                access_count=r[7],
            )
            for r in similar
        ]

        dup = resolve_duplicate(candidate, hits)
        if dup.action == "NOOP":
            # 判重：累加原记忆 access_count（强化信号）
            if dup.memory_id is not None:
                await strengthen(db, memory_id=dup.memory_id)
            return dup

        if dup.action == "CONFLICT":
            # 落入冲突区间 → LLM 裁决
            old_content = dup.content
            decision = await _decide_conflict(candidate, old_content)
            if decision == "UPDATE":
                await self._supersede_and_update(
                    db,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    old_id=dup.memory_id,
                    candidate=candidate,
                    session_id=session_id,
                    source_summary=candidate.get("source_summary"),
                )
                return ConsolidateResult(
                    action="UPDATE", memory_id=dup.memory_id, content=candidate["content"], confidence=candidate["confidence"]
                )
            if decision == "MERGE":
                merged = _merge_texts(old_content, candidate["content"])
                await self._supersede_and_update(
                    db,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    old_id=dup.memory_id,
                    candidate={**candidate, "content": merged},
                    session_id=session_id,
                    source_summary=candidate.get("source_summary"),
                )
                return ConsolidateResult(
                    action="MERGE", memory_id=dup.memory_id, content=merged, confidence=candidate["confidence"]
                )
            # ADD：新增

        # ADD
        await self._insert(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            content=candidate["content"],
            memory_type=candidate["memory_type"],
            confidence=candidate["confidence"],
            status="active",
            session_id=session_id,
            source_summary=candidate.get("source_summary"),
        )
        return ConsolidateResult(action="ADD", content=candidate["content"], confidence=candidate["confidence"])

    async def _insert(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        user_id: str,
        content: str,
        memory_type: str,
        confidence: float,
        status: str,
        session_id: uuid.UUID | None,
        source_summary: str | None,
    ) -> MemoryEntry:
        """向量化并插入一条记忆。"""
        vectors = await embed_texts([content])
        entry = MemoryEntry(
            tenant_id=tenant_id,
            user_id=user_id,
            memory_type=memory_type,
            content=content,
            status=status,
            confidence=confidence,
            embedding=vectors[0],
            source_session_id=session_id,
            source_summary=source_summary,
        )
        db.add(entry)
        await db.flush()
        return entry

    async def _supersede_and_update(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        user_id: str,
        old_id: int | None,
        candidate: dict,
        session_id: uuid.UUID | None,
        source_summary: str | None,
    ) -> None:
        """将被覆盖的旧记忆置为 superseded，并按新内容新增/更新。"""
        if old_id is not None:
            await db.execute(
                update(MemoryEntry)
                .where(
                    MemoryEntry.id == old_id,
                    MemoryEntry.tenant_id == tenant_id,
                    MemoryEntry.user_id == user_id,
                )
                .values(status="superseded", updated_at=_now())
            )
        await self._insert(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            content=candidate["content"],
            memory_type=candidate.get("memory_type", "semantic"),
            confidence=candidate.get("confidence", 1.0),
            status="active",
            session_id=session_id,
            source_summary=source_summary or None,
        )

    # ---------- 读取（MEMORY_SPEC §6.2） ----------

    async def retrieve_relevant(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        user_id: str,
        query: str,
        top_k: int | None = None,
    ) -> list[retriever.MemoryHit]:
        """对话前检索注入。仅命中 active 记忆；命中后异步强化。"""
        if not settings.MEMORY_ENABLED:
            return []
        if not await long_term_enabled(db, tenant_id=tenant_id, user_id=user_id):
            return []
        try:
            hits = await retriever.retrieve(
                db, tenant_id=tenant_id, user_id=user_id, query=query, top_k=top_k
            )
        except EmbeddingError:
            logger.warning("记忆检索 embedding 失败，跳过注入")
            return []
        # 命中强化（异步最佳，这里直接 commit 简化）
        for hit in hits:
            await strengthen(db, memory_id=hit.memory_id)
        return hits

    def format_for_prompt(self, hits: list[retriever.MemoryHit]) -> str:
        """将命中记忆格式化为 system prompt 注入文本。"""
        return retriever.memories_to_markdown(hits)

    # ---------- 管理（MEMORY_SPEC §7） ----------

    async def list_entries(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        user_id: str,
        memory_type: str | None = None,
        status: str | None = None,
        q: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """记忆列表（分页 / 类型 / 状态 / 关键词过滤，仅当前用户）。"""
        filters = [MemoryEntry.tenant_id == tenant_id, MemoryEntry.user_id == user_id]
        if memory_type:
            filters.append(MemoryEntry.memory_type == memory_type)
        if status:
            filters.append(MemoryEntry.status == status)
        if q:
            filters.append(MemoryEntry.content.ilike(f"%{q}%"))

        total = (
            await db.execute(select(func.count(MemoryEntry.id)).where(*filters))
        ).scalar_one()
        page = max(1, page)
        page_size = max(1, min(page_size, 200))
        rows = (
            (
                await db.execute(
                    select(MemoryEntry)
                    .where(*filters)
                    .order_by(MemoryEntry.updated_at.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        return {
            "items": [self._entry_to_dict(e) for e in rows],
            "total": int(total),
            "page": page,
            "page_size": page_size,
        }

    async def timeline(
        self, db: AsyncSession, *, tenant_id: str, user_id: str, days: int = 30
    ) -> list[dict[str, Any]]:
        """按天分组的记忆时间线（仅当前用户，默认近 30 天）。"""
        since = _now() - timedelta(days=days)
        rows = (
            (
                await db.execute(
                    select(MemoryEntry)
                    .where(
                        MemoryEntry.tenant_id == tenant_id,
                        MemoryEntry.user_id == user_id,
                        MemoryEntry.created_at >= since,
                    )
                    .order_by(MemoryEntry.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        groups: dict[str, list[MemoryEntry]] = {}
        for entry in rows:
            key = entry.created_at.date().isoformat() if entry.created_at else "unknown"
            groups.setdefault(key, []).append(entry)
        ordered = sorted(groups.items(), key=lambda kv: kv[0], reverse=True)
        return [
            {
                "date": key,
                "items": [self._entry_to_dict(e) for e in items],
                "count": len(items),
            }
            for key, items in ordered
        ]

    async def overview(
        self, db: AsyncSession, *, tenant_id: str, user_id: str
    ) -> dict[str, Any]:
        """记忆统计（当前用户）：总数 / 类型 / 状态 / 近7日新增 / 命中 top5。"""
        base = [MemoryEntry.tenant_id == tenant_id, MemoryEntry.user_id == user_id]
        total = (
            await db.execute(select(func.count(MemoryEntry.id)).where(*base))
        ).scalar_one()
        semantic = (
            await db.execute(
                select(func.count(MemoryEntry.id)).where(
                    *base, MemoryEntry.memory_type == "semantic"
                )
            )
        ).scalar_one()
        episodic = (
            await db.execute(
                select(func.count(MemoryEntry.id)).where(
                    *base, MemoryEntry.memory_type == "episodic"
                )
            )
        ).scalar_one()
        active = (
            await db.execute(
                select(func.count(MemoryEntry.id)).where(
                    *base, MemoryEntry.status == "active"
                )
            )
        ).scalar_one()
        pending = (
            await db.execute(
                select(func.count(MemoryEntry.id)).where(
                    *base, MemoryEntry.status == "pending"
                )
            )
        ).scalar_one()
        disabled = (
            await db.execute(
                select(func.count(MemoryEntry.id)).where(
                    *base, MemoryEntry.status == "disabled"
                )
            )
        ).scalar_one()
        since_7d = _now() - timedelta(days=7)
        added_7d = (
            await db.execute(
                select(func.count(MemoryEntry.id)).where(
                    *base, MemoryEntry.created_at >= since_7d
                )
            )
        ).scalar_one()
        top = (
            (
                await db.execute(
                    select(MemoryEntry)
                    .where(*base, MemoryEntry.status == "active")
                    .order_by(MemoryEntry.access_count.desc())
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
        return {
            "total": int(total),
            "semantic_count": int(semantic),
            "episodic_count": int(episodic),
            "active_count": int(active),
            "pending_count": int(pending),
            "disabled_count": int(disabled),
            "added_7d": int(added_7d),
            "top_accessed": [self._entry_to_dict(e) for e in top],
        }

    async def graph(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        user_id: str,
        memory_type: str | None = None,
        status: str | None = "active",
        q: str | None = None,
        limit: int = 80,
        similarity_threshold: float = 0.72,
    ) -> dict[str, Any]:
        """构建当前用户的记忆关系图谱。

        节点取最近更新的记忆；同一来源会话形成同源边，其余节点在向量余弦
        相似度达到阈值时形成语义边。计算规模由 limit 限制，避免管理页请求
        对数据库造成无界开销。
        """
        filters = [MemoryEntry.tenant_id == tenant_id, MemoryEntry.user_id == user_id]
        if memory_type:
            filters.append(MemoryEntry.memory_type == memory_type)
        if status:
            filters.append(MemoryEntry.status == status)
        if q:
            filters.append(MemoryEntry.content.ilike(f"%{q}%"))

        limit = max(10, min(limit, 120))
        threshold = max(0.5, min(similarity_threshold, 0.95))
        rows = (
            (
                await db.execute(
                    select(MemoryEntry)
                    .where(*filters)
                    .order_by(MemoryEntry.updated_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

        edges: list[dict[str, Any]] = []
        for index, source in enumerate(rows):
            for target in rows[index + 1 :]:
                same_source = bool(
                    source.source_session_id
                    and source.source_session_id == target.source_session_id
                )
                similarity = _cosine_similarity(source.embedding, target.embedding)
                if same_source:
                    edges.append(
                        {
                            "source": int(source.id),
                            "target": int(target.id),
                            "relation": "same_source",
                            "strength": round(max(similarity or 0.0, 0.82), 4),
                        }
                    )
                elif similarity is not None and similarity >= threshold:
                    edges.append(
                        {
                            "source": int(source.id),
                            "target": int(target.id),
                            "relation": "semantic",
                            "strength": round(similarity, 4),
                        }
                    )

        # 保留最强的关系，避免高密度数据下形成不可读的“毛线团”。
        edges.sort(key=lambda edge: edge["strength"], reverse=True)
        max_edges = max(limit, min(limit * 2, 180))
        return {
            "nodes": [self._entry_to_dict(entry) for entry in rows],
            "edges": edges[:max_edges],
            "similarity_threshold": round(threshold, 2),
        }

    async def get_entry(
        self, db: AsyncSession, *, tenant_id: str, user_id: str, memory_id: int
    ) -> MemoryEntry | None:
        """按 id + tenant + user 查询记忆（越权返回 None）。"""
        result = await db.execute(
            select(MemoryEntry).where(
                MemoryEntry.id == memory_id,
                MemoryEntry.tenant_id == tenant_id,
                MemoryEntry.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def update_entry(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        user_id: str,
        memory_id: int,
        content: str | None = None,
        status: str | None = None,
    ) -> MemoryEntry | None:
        """编辑记忆：content 变更自动重新向量化；status 支持 active/disabled/pending。"""
        entry = await self.get_entry(
            db, tenant_id=tenant_id, user_id=user_id, memory_id=memory_id
        )
        if entry is None:
            return None
        if content is not None and content.strip() and content.strip() != entry.content:
            try:
                vectors = await embed_texts([content])
            except EmbeddingError as exc:
                raise MemoryError(str(exc)) from exc
            entry.content = content
            entry.embedding = vectors[0]
        if status is not None and status in ("active", "disabled", "pending"):
            entry.status = status
        entry.updated_at = _now()
        await db.commit()
        await db.refresh(entry)
        return entry

    async def delete_entry(
        self, db: AsyncSession, *, tenant_id: str, user_id: str, memory_id: int
    ) -> bool:
        """单条物理删除（行 + 向量，保证 forget 语义即时生效）。"""
        result = await db.execute(
            delete(MemoryEntry).where(
                MemoryEntry.id == memory_id,
                MemoryEntry.tenant_id == tenant_id,
                MemoryEntry.user_id == user_id,
            )
        )
        await db.commit()
        return (result.rowcount or 0) > 0

    async def clear_entries(
        self, db: AsyncSession, *, tenant_id: str, user_id: str
    ) -> int:
        """清空当前用户全部记忆（物理删除）。返回删除条数。"""
        result = await db.execute(
            delete(MemoryEntry).where(
                MemoryEntry.tenant_id == tenant_id,
                MemoryEntry.user_id == user_id,
            )
        )
        await db.commit()
        return result.rowcount or 0

    # ---------- 设置（MEMORY_SPEC §7.2） ----------

    async def get_settings(
        self, db: AsyncSession, *, tenant_id: str, user_id: str
    ) -> dict[str, Any]:
        s = await get_or_create_settings(db, tenant_id=tenant_id, user_id=user_id)
        return {
            "long_term_enabled": bool(s.long_term_enabled),
            "auto_extract": bool(s.auto_extract),
            "ttl_days": int(s.ttl_days),
        }

    async def update_settings(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        user_id: str,
        long_term_enabled: bool | None = None,
        auto_extract: bool | None = None,
        ttl_days: int | None = None,
    ) -> dict[str, Any]:
        s = await get_or_create_settings(db, tenant_id=tenant_id, user_id=user_id)
        if long_term_enabled is not None:
            s.long_term_enabled = long_term_enabled
        if auto_extract is not None:
            s.auto_extract = auto_extract
        if ttl_days is not None:
            s.ttl_days = max(7, min(ttl_days, 365))
        s.updated_at = _now()
        await db.commit()
        await db.refresh(s)
        return {
            "long_term_enabled": bool(s.long_term_enabled),
            "auto_extract": bool(s.auto_extract),
            "ttl_days": int(s.ttl_days),
        }

    # ---------- 工具辅助 ----------

    async def search_as_tool(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        user_id: str,
        query: str,
        top_k: int = 5,
    ) -> list[retriever.MemoryHit]:
        """memory_search 工具使用：检索 + 强化 + 返回 hit 列表。"""
        hits = await retriever.retrieve(
            db, tenant_id=tenant_id, user_id=user_id, query=query, top_k=top_k
        )
        for hit in hits:
            await strengthen(db, memory_id=hit.memory_id)
        return hits

    async def run_ttl_cleanup(
        self, db: AsyncSession, *, tenant_id: str, user_id: str
    ) -> int:
        """触发当前用户的 TTL 过期扫描。"""
        return await expire_stale(db, tenant_id=tenant_id, user_id=user_id)

    # ---------- 序列化 ----------

    @staticmethod
    def _entry_to_dict(entry: MemoryEntry) -> dict[str, Any]:
        return {
            "memory_id": int(entry.id),
            "memory_type": entry.memory_type,
            "content": entry.content,
            "status": entry.status,
            "confidence": round(float(entry.confidence), 4),
            "source_session_id": str(entry.source_session_id) if entry.source_session_id else None,
            "source_summary": entry.source_summary,
            "access_count": int(entry.access_count),
            "last_accessed_at": entry.last_accessed_at.isoformat() if entry.last_accessed_at else None,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
            "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
        }


class _RowLike:
    """将 raw 查询行包装为带属性的对象（供 resolve_duplicate 使用）。"""

    def __init__(self, *, memory_id, content, similarity, access_count=0):
        self.memory_id = memory_id
        self.content = content
        self.similarity = similarity
        self.access_count = access_count


def _cosine_similarity(left: Any, right: Any) -> float | None:
    """兼容 pgvector 返回的 list/ndarray，计算两个已存向量的余弦相似度。"""
    if left is None or right is None:
        return None
    left_values = [float(value) for value in left]
    right_values = [float(value) for value in right]
    if not left_values or len(left_values) != len(right_values):
        return None
    dot = sum(a * b for a, b in zip(left_values, right_values))
    left_norm = math.sqrt(sum(value * value for value in left_values))
    right_norm = math.sqrt(sum(value * value for value in right_values))
    if left_norm == 0 or right_norm == 0:
        return None
    return dot / (left_norm * right_norm)


def _merge_texts(old: str, new: str) -> str:
    """合并两条记忆文本，避免重复。"""
    if old in new:
        return new
    if new in old:
        return old
    return f"{old}；{new}"


memory_service = MemoryService()
