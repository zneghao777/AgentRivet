"""RabbitMQ management API 队列深度采集。"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote, unquote, urlparse

import httpx

from ..config import settings
from ..observability.metrics import metrics
from .connection import (
    OBS_AUDIT_QUEUE,
    OBS_DEAD_QUEUE,
    OBS_SPANS_QUEUE,
    SESSION_DEAD_QUEUE,
    SESSION_QUEUE,
)

logger = logging.getLogger(__name__)
_task: asyncio.Task[None] | None = None
_last_depths: dict[str, int] = {}


def _credentials() -> tuple[str, str, str]:
    parsed = urlparse(settings.RABBITMQ_URL)
    username = unquote(parsed.username or "guest")
    password = unquote(parsed.password or "guest")
    vhost = unquote(parsed.path.lstrip("/") or "/")
    return username, password, vhost


async def collect_queue_depths() -> None:
    username, password, vhost = _credentials()
    queues = (
        OBS_SPANS_QUEUE,
        OBS_AUDIT_QUEUE,
        SESSION_QUEUE,
        OBS_DEAD_QUEUE,
        SESSION_DEAD_QUEUE,
    )
    async with httpx.AsyncClient(
        base_url=settings.RABBITMQ_MANAGEMENT_URL,
        auth=(username, password),
        timeout=5,
    ) as client:
        for queue in queues:
            response = await client.get(
                f"/api/queues/{quote(vhost, safe='')}/{quote(queue, safe='')}"
            )
            response.raise_for_status()
            depth = int(response.json().get("messages", 0))
            previous = _last_depths.get(queue, depth)
            metrics.set_mq_queue_depth(queue, depth)
            if queue in {OBS_DEAD_QUEUE, SESSION_DEAD_QUEUE} and depth > previous:
                metrics.record_mq_deadletter(queue, depth - previous)
            _last_depths[queue] = depth


async def _monitor() -> None:
    while True:
        try:
            await collect_queue_depths()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("RabbitMQ 队列深度采集失败: %s", exc)
        await asyncio.sleep(max(5, settings.MQ_MONITOR_INTERVAL_S))


def start_mq_monitor() -> None:
    global _task
    if not settings.MQ_ENABLED or (_task is not None and not _task.done()):
        return
    _task = asyncio.create_task(_monitor())


async def stop_mq_monitor() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None
