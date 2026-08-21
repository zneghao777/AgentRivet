"""文档三阶段状态到知识库聚合健康状态的统一计算。"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..storage.models import DocumentMeta, KnowledgeBase


def aggregate_graph_status(index_mode: str, statuses: Iterable[str]) -> str:
    if index_mode == "standard":
        return "disabled"
    values = [status for status in statuses if status != "excluded"]
    if not values:
        return "none"
    if "building" in values:
        return "building"
    if "pending" in values:
        return "pending"
    if all(status == "ready" for status in values):
        return "ready"
    if "ready" in values and any(status in {"failed", "stale"} for status in values):
        return "partial"
    if all(status == "failed" for status in values):
        return "failed"
    if "stale" in values:
        return "stale"
    return "failed"


def aggregate_index_status(vector_statuses: Iterable[str]) -> str:
    values = list(vector_statuses)
    if not values:
        return "empty"
    if any(status in {"pending", "building"} for status in values):
        return "indexing"
    if all(status == "ready" for status in values):
        return "ready"
    if "ready" in values and any(status in {"failed", "stale"} for status in values):
        return "partial"
    if all(status == "failed" for status in values):
        return "failed"
    if "stale" in values:
        return "stale"
    return "failed"


async def refresh_kb_status(
    db: AsyncSession, *, tenant_id: str, kb_id: uuid.UUID, commit: bool = True
) -> KnowledgeBase | None:
    kb = (
        await db.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.id == kb_id,
                KnowledgeBase.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if kb is None:
        return None
    docs = list(
        (
            await db.execute(
                select(DocumentMeta).where(
                    DocumentMeta.kb_id == kb_id,
                    DocumentMeta.tenant_id == tenant_id,
                )
            )
        )
        .scalars()
        .all()
    )
    kb.index_status = aggregate_index_status(doc.vector_status for doc in docs)
    kb.graph_status = aggregate_graph_status(kb.index_mode, (doc.graph_status for doc in docs))
    if commit:
        await db.commit()
        await db.refresh(kb)
    else:
        await db.flush()
    return kb
