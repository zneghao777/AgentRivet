"""知识库索引任务的持久化、去重、进度、重试和恢复。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..storage.models import KnowledgeIndexJob

ACTIVE_STATUSES = {"queued", "running"}


def _now() -> datetime:
    return datetime.now(UTC)


def job_to_dict(job: KnowledgeIndexJob) -> dict[str, Any]:
    return {
        "job_id": str(job.id),
        "kb_id": str(job.kb_id),
        "doc_id": job.doc_id,
        "job_type": job.job_type,
        "status": job.status,
        "stage": job.stage,
        "progress": job.progress,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "meta": job.meta or {},
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


class IndexJobService:
    async def enqueue(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        job_type: str,
        doc_id: int | None = None,
        meta: dict[str, Any] | None = None,
        max_attempts: int = 3,
    ) -> tuple[KnowledgeIndexJob, bool]:
        existing = (
            await db.execute(
                select(KnowledgeIndexJob).where(
                    KnowledgeIndexJob.tenant_id == tenant_id,
                    KnowledgeIndexJob.kb_id == kb_id,
                    KnowledgeIndexJob.doc_id.is_(None)
                    if doc_id is None
                    else KnowledgeIndexJob.doc_id == doc_id,
                    KnowledgeIndexJob.job_type == job_type,
                    KnowledgeIndexJob.status.in_(ACTIVE_STATUSES),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False
        job = KnowledgeIndexJob(
            tenant_id=tenant_id,
            kb_id=kb_id,
            doc_id=doc_id,
            job_type=job_type,
            status="queued",
            stage="queued",
            progress=0,
            max_attempts=max_attempts,
            meta=meta or {},
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)
        return job, True

    async def get(
        self, db: AsyncSession, *, tenant_id: str, kb_id: uuid.UUID, job_id: uuid.UUID
    ) -> KnowledgeIndexJob | None:
        return (
            await db.execute(
                select(KnowledgeIndexJob).where(
                    KnowledgeIndexJob.id == job_id,
                    KnowledgeIndexJob.tenant_id == tenant_id,
                    KnowledgeIndexJob.kb_id == kb_id,
                )
            )
        ).scalar_one_or_none()

    async def active_for_doc(
        self, db: AsyncSession, *, doc_id: int, job_type: str
    ) -> KnowledgeIndexJob | None:
        return (
            await db.execute(
                select(KnowledgeIndexJob)
                .where(
                    KnowledgeIndexJob.doc_id == doc_id,
                    KnowledgeIndexJob.job_type == job_type,
                    KnowledgeIndexJob.status.in_(ACTIVE_STATUSES),
                )
                .order_by(KnowledgeIndexJob.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def list(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        kb_id: uuid.UUID,
        status: str | None = None,
        limit: int = 100,
    ) -> list[KnowledgeIndexJob]:
        stmt = select(KnowledgeIndexJob).where(
            KnowledgeIndexJob.tenant_id == tenant_id,
            KnowledgeIndexJob.kb_id == kb_id,
        )
        if status == "active":
            stmt = stmt.where(KnowledgeIndexJob.status.in_(ACTIVE_STATUSES))
        elif status:
            stmt = stmt.where(KnowledgeIndexJob.status == status)
        result = await db.execute(
            stmt.order_by(KnowledgeIndexJob.created_at.desc()).limit(max(1, min(limit, 200)))
        )
        return list(result.scalars().all())

    async def update(
        self,
        db: AsyncSession,
        job: KnowledgeIndexJob | None,
        *,
        status: str | None = None,
        stage: str | None = None,
        progress: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        meta: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> None:
        if job is None:
            return
        now = _now()
        if status:
            job.status = status
            if status == "running" and job.started_at is None:
                job.started_at = now
                job.attempt += 1
            if status in {"succeeded", "failed", "cancelled"}:
                job.finished_at = now
        if stage is not None:
            job.stage = stage
        if progress is not None:
            job.progress = max(0, min(100, progress))
        job.error_code = error_code
        job.error_message = error_message
        if meta:
            job.meta = {**(job.meta or {}), **meta}
        job.updated_at = now
        if commit:
            await db.commit()
        else:
            await db.flush()

    async def cancel(self, db: AsyncSession, job: KnowledgeIndexJob) -> bool:
        if job.status != "queued":
            return False
        await self.update(db, job, status="cancelled", stage="cancelled")
        return True

    async def recover_stale(
        self, db: AsyncSession, *, stale_after_seconds: int = 300
    ) -> list[KnowledgeIndexJob]:
        cutoff = _now() - timedelta(seconds=max(30, stale_after_seconds))
        rows = list(
            (
                await db.execute(
                    select(KnowledgeIndexJob).where(
                        KnowledgeIndexJob.status == "running",
                        KnowledgeIndexJob.updated_at < cutoff,
                    )
                )
            )
            .scalars()
            .all()
        )
        for job in rows:
            if job.attempt < job.max_attempts:
                job.status = "queued"
                job.stage = "recovered"
                job.error_code = "INDEX_JOB_INTERRUPTED"
                job.error_message = "服务中断，任务已重新排队"
            else:
                job.status = "failed"
                job.stage = "failed"
                job.finished_at = _now()
                job.error_code = "INDEX_JOB_FAILED"
                job.error_message = "任务重试次数已耗尽"
            job.updated_at = _now()
        await db.commit()
        return rows


index_job_service = IndexJobService()
