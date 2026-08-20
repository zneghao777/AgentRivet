"""记忆卫生（MEMORY_SPEC §6.3）：去重 / 冲突调和 / TTL 过期 / 强化。

- 判重：相似度 >= MEMORY_SIM_DUP → NOOP（累加原记忆 access_count）
- 冲突调和：MEMORY_SIM_CONFLICT <= 相似度 < MEMORY_SIM_DUP → 交 LLM 裁决
- TTL：created_at 超 ttl_days 且 access_count < 强化阈值 → expired（软删）
- 强化：命中记忆 access_count += 1, last_accessed_at = now
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..storage.models import MemoryEntry, MemorySettings

logger = logging.getLogger(__name__)


class HygieneError(Exception):
    """记忆卫生异常（用于区分业务错误）。"""


@dataclass
class ConsolidateResult:
    """单条候选记忆的调和结果。"""

    action: str  # ADD / UPDATE / MERGE / NOOP / PENDING
    memory_id: int | None = None  # 影响的记忆 id（UPDATE/MERGE 时的旧记忆）
    content: str = ""  # 最终写入内容
    confidence: float = 0.0


def _confidence_pending_threshold() -> float:
    return settings.MEMORY_CONFIDENCE_PENDING


def should_be_pending(confidence: float) -> bool:
    """低置信度候选 → pending（不参与检索）。"""
    return confidence < _confidence_pending_threshold()


def resolve_duplicate(memory: dict, hits: list) -> ConsolidateResult:
    """判定候选是否与现有记忆重复（纯函数，供测试）。

    hits：该 (tenant,user) 下的 top-k 相似 active 记忆，元素含 .memory_id/.content/.similarity。
    """
    if not hits:
        return ConsolidateResult(
            action="ADD",
            content=memory["content"],
            confidence=memory.get("confidence", 1.0),
        )
    top = max(hits, key=lambda h: h.similarity)
    sim = top.similarity
    if sim >= settings.MEMORY_SIM_DUP:
        # 判重 NOOP：返回命中记忆 id 供调用方累加 access_count
        return ConsolidateResult(
            action="NOOP", memory_id=top.memory_id, content=top.content, confidence=top.similarity
        )
    if sim >= settings.MEMORY_SIM_CONFLICT:
        # 落入冲突区间 → 需 LLM 裁决（action=CONFLICT 由调用方处理）
        return ConsolidateResult(
            action="CONFLICT",
            memory_id=top.memory_id,
            content=top.content,
            confidence=sim,
        )
    return ConsolidateResult(action="ADD", content=memory["content"], confidence=memory["confidence"])


async def get_settings(
    db: AsyncSession, *, tenant_id: str, user_id: str
) -> MemorySettings | None:
    """读取用户级记忆设置（不存在则返回 None，由调用方回退默认）。"""
    result = await db.execute(
        select(MemorySettings).where(
            MemorySettings.tenant_id == tenant_id,
            MemorySettings.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def get_or_create_settings(
    db: AsyncSession, *, tenant_id: str, user_id: str
) -> MemorySettings:
    """读取或创建用户级记忆设置（默认开启长期记忆）。"""
    existing = await get_settings(db, tenant_id=tenant_id, user_id=user_id)
    if existing is not None:
        return existing
    s = MemorySettings(
        tenant_id=tenant_id,
        user_id=user_id,
        long_term_enabled=True,
        auto_extract=True,
        ttl_days=settings.MEMORY_DEFAULT_TTL_DAYS,
    )
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return s


async def long_term_enabled(db: AsyncSession, *, tenant_id: str, user_id: str) -> bool:
    """长期记忆总开关：全局 + 用户级都开启才为 True。"""
    if not settings.MEMORY_ENABLED:
        return False
    s = await get_settings(db, tenant_id=tenant_id, user_id=user_id)
    return s.long_term_enabled if s is not None else True


async def auto_extract_enabled(db: AsyncSession, *, tenant_id: str, user_id: str) -> bool:
    """对话后自动提炼开关。"""
    s = await get_settings(db, tenant_id=tenant_id, user_id=user_id)
    return s.auto_extract if s is not None else True


async def strengthen(db: AsyncSession, *, memory_id: int) -> None:
    """强化命中记忆：access_count += 1, last_accessed_at = now（MEMORY_SPEC §6.2）。"""
    await db.execute(
        update(MemoryEntry)
        .where(MemoryEntry.id == memory_id)
        .values(
            access_count=MemoryEntry.access_count + 1,
            last_accessed_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()


async def expire_stale(
    db: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    ttl_days: int | None = None,
) -> int:
    """TTL 过期扫描（MEMORY_SPEC §6.3.1）。

    将 status=active、created_at 超 ttl_days、access_count < 强化阈值的记忆置为 expired。
    返回被过期的条数。调用方负责 commit。
    """
    ttl_days = ttl_days or settings.MEMORY_DEFAULT_TTL_DAYS
    cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
    threshold = settings.MEMORY_STRENGTHEN_THRESHOLD
    result = await db.execute(
        update(MemoryEntry)
        .where(
            MemoryEntry.tenant_id == tenant_id,
            MemoryEntry.user_id == user_id,
            MemoryEntry.status == "active",
            MemoryEntry.created_at < cutoff,
            MemoryEntry.access_count < threshold,
        )
        .values(status="expired", updated_at=datetime.now(timezone.utc))
    )
    await db.commit()
    return result.rowcount or 0
