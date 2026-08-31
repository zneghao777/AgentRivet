"""ChatRun Transactional Outbox publisher。"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from ..config import settings
from ..mq.envelopes import make_envelope
from ..mq.publisher import publish_envelope
from ..storage import chat_runs
from ..storage.db import AsyncSessionLocal
from ..storage.models import ChatRunOutbox

logger = logging.getLogger(__name__)
_task: asyncio.Task[None] | None = None


async def _dispatch_once() -> int:
    async with AsyncSessionLocal() as db:
        batch = list(
            (
                await db.execute(
                    select(ChatRunOutbox)
                    .where(ChatRunOutbox.published_at.is_(None))
                    .order_by(ChatRunOutbox.created_at.asc())
                    .with_for_update(skip_locked=True)
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        published = 0
        for item in batch:
            item.attempt += 1
            envelope = make_envelope(
                item.command_type,
                dict(item.payload or {}),
                message_id=str(item.message_id),
            )
            try:
                await publish_envelope(envelope)
            except Exception as exc:  # noqa: BLE001
                item.last_error = str(exc)[:2000]
                logger.warning("ChatRun Outbox 发布失败 id=%s: %s", item.id, exc)
                continue
            item.published_at = chat_runs.utcnow()
            item.last_error = None
            published += 1
        await db.commit()
        return published


async def _loop() -> None:
    while True:
        await _dispatch_once()
        await asyncio.sleep(max(0.25, settings.CHAT_RUN_EVENT_POLL_INTERVAL_MS / 1000))


def start_chat_run_outbox() -> None:
    global _task
    if (
        not settings.CHAT_RUN_ENABLED
        or not settings.CHAT_RUN_DURABLE_ENABLED
        or not settings.MQ_ENABLED
        or (_task is not None and not _task.done())
    ):
        return
    _task = asyncio.create_task(_loop())


async def stop_chat_run_outbox() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    await asyncio.gather(_task, return_exceptions=True)
    _task = None
