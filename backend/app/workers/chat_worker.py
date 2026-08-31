"""独立 ChatRun Worker：至少一次命令投递 + 数据库租约幂等领取。"""

from __future__ import annotations

import asyncio
import logging
import signal
import uuid

from ..config import settings
from ..harness.app_runtime import harness_app
from ..harness.run_coordinator import RunCoordinator
from ..mq.connection import CHAT_RESUME_QUEUE, CHAT_RUN_QUEUE, mq_connection
from ..mq.envelopes import EnvelopeError, decode_envelope
from ..observability import audit
from ..observability.tracing import init_tracing
from ..skills.manager import skill_manager
from ..storage.checkpointer import get_checkpointer
from ..storage.db import AsyncSessionLocal, create_tables
from ..tools.registry import tool_registry

logger = logging.getLogger(__name__)


async def _bootstrap_runtime() -> None:
    await create_tables()
    tool_registry.register_builtin()
    async with AsyncSessionLocal() as db:
        await skill_manager.discover(db)
    checkpointer = await get_checkpointer()
    harness_app.build(tool_registry, checkpointer=checkpointer)
    init_tracing(lambda: AsyncSessionLocal())
    audit.init_audit(lambda: AsyncSessionLocal())


async def process_message(message, coordinator: RunCoordinator) -> None:
    try:
        envelope = decode_envelope(message.body)
        if envelope.type not in {"run.requested", "run.resume"}:
            raise EnvelopeError("消息类型不属于 ChatRun 队列")
        run_id = uuid.UUID(str(envelope.payload.get("run_id") or ""))
    except (EnvelopeError, ValueError) as exc:
        logger.warning("非法 ChatRun 命令进入死信: %s", exc)
        await message.reject(requeue=False)
        return
    try:
        await coordinator.execute(run_id)
    except Exception:  # noqa: BLE001
        logger.exception("ChatRun Worker 未完成命令 run=%s", run_id)
        await message.nack(requeue=True)
        return
    await message.ack()


async def _run(stop: asyncio.Event) -> None:
    await _bootstrap_runtime()
    coordinator = RunCoordinator()
    coordinator.worker_id = f"{settings.APP_NAME}-worker-{uuid.uuid4().hex[:12]}"
    async with AsyncSessionLocal() as db:
        from ..storage import chat_runs

        await chat_runs.recover_expired_leases(
            db,
            max_attempts=settings.CHAT_RUN_MAX_ATTEMPTS,
            write_outbox=True,
        )
    channel = await mq_connection.open_consumer_channel()
    queues = [
        await channel.get_queue(CHAT_RUN_QUEUE, ensure=True),
        await channel.get_queue(CHAT_RESUME_QUEUE, ensure=True),
    ]
    tasks: set[asyncio.Task[None]] = set()

    async def on_message(message) -> None:
        task = asyncio.create_task(process_message(message, coordinator))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    consumers = [
        (queue, await queue.consume(on_message, no_ack=False)) for queue in queues
    ]
    logger.info("chat-worker 已启动 prefetch=%d", settings.MQ_PREFETCH)
    await stop.wait()
    for queue, tag in consumers:
        await queue.cancel(tag)
    if tasks:
        _, pending = await asyncio.wait(tuple(tasks), timeout=30)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    await mq_connection.close()


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not settings.MQ_ENABLED or not settings.CHAT_RUN_DURABLE_ENABLED:
        raise RuntimeError("chat-worker 需要 MQ_ENABLED 和 CHAT_RUN_DURABLE_ENABLED")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await _run(stop)


if __name__ == "__main__":
    asyncio.run(main())
