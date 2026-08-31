"""ChatRun 事件保留和审批过期维护任务。"""

from __future__ import annotations

import asyncio
import logging
import time

from ..config import settings
from ..observability.metrics import metrics
from ..storage import chat_runs
from ..storage.db import AsyncSessionLocal

logger = logging.getLogger(__name__)
_task: asyncio.Task[None] | None = None


async def _loop() -> None:
    last_cleanup = 0.0
    while True:
        started = time.monotonic()
        try:
            async with AsyncSessionLocal() as db:
                requeued, recovery_failed = await chat_runs.recover_expired_leases(
                    db,
                    max_attempts=settings.CHAT_RUN_MAX_ATTEMPTS,
                    write_outbox=(
                        settings.CHAT_RUN_DURABLE_ENABLED and settings.MQ_ENABLED
                    ),
                )
            metrics.record_chat_run_recovery("requeued", requeued)
            metrics.record_chat_run_recovery("failed", recovery_failed)

            expired = queue_expired = deleted = 0
            if time.monotonic() - last_cleanup >= 3600:
                cleanup_started = time.monotonic()
                try:
                    async with AsyncSessionLocal() as db:
                        expired = await chat_runs.expire_pending_approvals(
                            db,
                            timeout_seconds=settings.CHAT_RUN_APPROVAL_TIMEOUT_SECONDS,
                        )
                    async with AsyncSessionLocal() as db:
                        queue_expired = await chat_runs.expire_queued_runs(
                            db,
                            timeout_seconds=settings.CHAT_RUN_QUEUE_TIMEOUT_SECONDS,
                        )
                    async with AsyncSessionLocal() as db:
                        deleted = await chat_runs.cleanup_terminal_events(
                            db,
                            retention_days=settings.CHAT_RUN_EVENT_RETENTION_DAYS,
                        )
                except Exception:
                    metrics.record_chat_run_event_cleanup(
                        0,
                        (time.monotonic() - cleanup_started) * 1000,
                        failed=True,
                    )
                    raise
                metrics.record_chat_run_event_cleanup(
                    deleted,
                    (time.monotonic() - cleanup_started) * 1000,
                )
                last_cleanup = time.monotonic()
            if requeued or recovery_failed or expired or queue_expired or deleted:
                logger.info(
                    "ChatRun 维护完成 requeued=%d recovery_failed=%d "
                    "approvals_expired=%d queue_expired=%d events_deleted=%d "
                    "duration_ms=%d",
                    requeued,
                    recovery_failed,
                    expired,
                    queue_expired,
                    deleted,
                    int((time.monotonic() - started) * 1000),
                )
        except Exception:  # noqa: BLE001
            logger.exception("ChatRun 维护任务失败")
        await asyncio.sleep(
            max(1, min(
                settings.CHAT_RUN_WORKER_HEARTBEAT_SECONDS,
                settings.CHAT_RUN_WORKER_LEASE_SECONDS // 3,
            ))
        )


def start_chat_run_maintenance() -> None:
    global _task
    if not settings.CHAT_RUN_ENABLED or (_task is not None and not _task.done()):
        return
    _task = asyncio.create_task(_loop())


async def stop_chat_run_maintenance() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    await asyncio.gather(_task, return_exceptions=True)
    _task = None
