"""非阻塞 MQ 发布；confirm 失败时执行调用方提供的降级路径。"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..config import settings
from ..observability.metrics import metrics
from .connection import CHAT_EXCHANGE, OBS_EXCHANGE, SESSION_EXCHANGE, mq_connection
from .envelopes import MessageEnvelope, make_envelope

logger = logging.getLogger(__name__)

Fallback = Callable[[], None | Awaitable[None]]
_publish_tasks: set[asyncio.Task[None]] = set()


def _route(message_type: str) -> tuple[str, str]:
    if message_type.startswith("obs."):
        return OBS_EXCHANGE, message_type
    if message_type == "session.postprocess":
        return SESSION_EXCHANGE, "postprocess"
    if message_type in {"run.requested", "run.resume"}:
        return CHAT_EXCHANGE, message_type
    raise ValueError(f"不支持的 MQ 消息类型: {message_type}")


async def publish_envelope(envelope: MessageEnvelope) -> None:
    exchange, routing_key = _route(envelope.type)
    await mq_connection.publish(exchange, routing_key, envelope.to_bytes())


async def _run_fallback(fallback: Fallback | None) -> None:
    if fallback is None:
        return
    result = fallback()
    if inspect.isawaitable(result):
        await result


async def _publish_or_fallback(
    envelope: MessageEnvelope, fallback: Fallback | None
) -> None:
    try:
        await publish_envelope(envelope)
        metrics.record_mq_publish(envelope.type, "success")
    except Exception as exc:  # noqa: BLE001
        metrics.record_mq_publish(envelope.type, "fallback")
        logger.warning("MQ 发布失败，执行降级 type=%s: %s", envelope.type, exc)
        try:
            await _run_fallback(fallback)
        except Exception:  # noqa: BLE001
            metrics.record_mq_publish(envelope.type, "failed")
            logger.exception("MQ 发布降级路径也失败 type=%s", envelope.type)


def publish_nowait(
    message_type: str,
    payload: dict[str, Any],
    *,
    fallback: Fallback | None = None,
) -> bool:
    """调度 confirm 发布并立即返回；未启用/不在事件循环时返回 False。"""
    if not settings.MQ_ENABLED:
        return False
    if message_type.startswith("obs.") and not settings.MQ_OBS_ENABLED:
        return False
    if message_type == "session.postprocess" and not settings.MQ_SESSION_ENABLED:
        return False
    try:
        task = asyncio.create_task(
            _publish_or_fallback(make_envelope(message_type, payload), fallback)
        )
    except RuntimeError:
        return False
    _publish_tasks.add(task)
    task.add_done_callback(_publish_tasks.discard)
    return True


async def flush_publish_tasks(timeout: float = 5.0) -> None:
    if not _publish_tasks:
        return
    done, pending = await asyncio.wait(tuple(_publish_tasks), timeout=timeout)
    for task in done:
        try:
            task.result()
        except Exception:  # noqa: BLE001
            logger.exception("MQ 发布任务异常")
    if pending:
        logger.warning("MQ 发布 flush 超时，仍有 %d 个任务", len(pending))


async def shutdown_publisher() -> None:
    await flush_publish_tasks()
    await mq_connection.close()
