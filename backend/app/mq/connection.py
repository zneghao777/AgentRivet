"""RabbitMQ 连接管理与幂等拓扑声明。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..config import settings

logger = logging.getLogger(__name__)

OBS_EXCHANGE = "obs.exchange"
SESSION_EXCHANGE = "session.exchange"
CHAT_EXCHANGE = "chat.exchange"
OBS_DLX = "obs.dlx"
SESSION_DLX = "session.dlx"
CHAT_DLX = "chat.dlx"
OBS_SPANS_QUEUE = "obs.spans.q"
OBS_AUDIT_QUEUE = "obs.audit.q"
SESSION_QUEUE = "session.postprocess.q"
CHAT_RUN_QUEUE = "chat.run.q"
CHAT_RESUME_QUEUE = "chat.resume.q"
OBS_DEAD_QUEUE = "obs.deadletter.q"
SESSION_DEAD_QUEUE = "session.deadletter.q"
CHAT_DEAD_QUEUE = "chat.deadletter.q"


def retry_queue_name(index: int) -> str:
    return f"session.retry.{index}.q"


async def declare_topology(channel: Any) -> None:
    """声明 durable exchange、quorum 工作队列、死信队列和分级重试队列。"""
    import aio_pika

    obs_exchange = await channel.declare_exchange(
        OBS_EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True
    )
    session_exchange = await channel.declare_exchange(
        SESSION_EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True
    )
    chat_exchange = await channel.declare_exchange(
        CHAT_EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True
    )
    obs_dlx = await channel.declare_exchange(
        OBS_DLX, aio_pika.ExchangeType.DIRECT, durable=True
    )
    session_dlx = await channel.declare_exchange(
        SESSION_DLX, aio_pika.ExchangeType.DIRECT, durable=True
    )
    chat_dlx = await channel.declare_exchange(
        CHAT_DLX, aio_pika.ExchangeType.DIRECT, durable=True
    )

    obs_dead = await channel.declare_queue(OBS_DEAD_QUEUE, durable=True)
    session_dead = await channel.declare_queue(SESSION_DEAD_QUEUE, durable=True)
    chat_dead = await channel.declare_queue(CHAT_DEAD_QUEUE, durable=True)
    await obs_dead.bind(obs_dlx, routing_key="deadletter")
    await session_dead.bind(session_dlx, routing_key="deadletter")
    await chat_dead.bind(chat_dlx, routing_key="deadletter")

    quorum_args = {
        "x-queue-type": "quorum",
        "x-dead-letter-routing-key": "deadletter",
    }
    spans = await channel.declare_queue(
        OBS_SPANS_QUEUE,
        durable=True,
        arguments={**quorum_args, "x-dead-letter-exchange": OBS_DLX},
    )
    audit = await channel.declare_queue(
        OBS_AUDIT_QUEUE,
        durable=True,
        arguments={**quorum_args, "x-dead-letter-exchange": OBS_DLX},
    )
    session = await channel.declare_queue(
        SESSION_QUEUE,
        durable=True,
        arguments={**quorum_args, "x-dead-letter-exchange": SESSION_DLX},
    )
    chat = await channel.declare_queue(
        CHAT_RUN_QUEUE,
        durable=True,
        arguments={**quorum_args, "x-dead-letter-exchange": CHAT_DLX},
    )
    chat_resume = await channel.declare_queue(
        CHAT_RESUME_QUEUE,
        durable=True,
        arguments={**quorum_args, "x-dead-letter-exchange": CHAT_DLX},
    )
    await spans.bind(obs_exchange, routing_key="obs.span.#")
    await spans.bind(obs_exchange, routing_key="obs.log")
    await audit.bind(obs_exchange, routing_key="obs.audit")
    await session.bind(session_exchange, routing_key="postprocess")
    await chat.bind(chat_exchange, routing_key="run.requested")
    await chat_resume.bind(chat_exchange, routing_key="run.resume")

    for index, ttl_ms in enumerate(settings.mq_retry_ttls_ms_list):
        await channel.declare_queue(
            retry_queue_name(index),
            durable=True,
            arguments={
                "x-message-ttl": ttl_ms,
                "x-dead-letter-exchange": SESSION_EXCHANGE,
                "x-dead-letter-routing-key": "postprocess",
            },
        )


class MQConnectionManager:
    """API 进程共享的 robust 连接与 publisher-confirm channel。"""

    def __init__(self) -> None:
        self._connection: Any | None = None
        self._channel: Any | None = None
        self._connect_lock = asyncio.Lock()
        self.last_error: str | None = None
        self._retry_delay_s = 1.0
        self._next_connect_at = 0.0

    @property
    def ready(self) -> bool:
        return bool(
            self._connection is not None
            and not self._connection.is_closed
            and self._channel is not None
            and not self._channel.is_closed
        )

    async def connect(self) -> None:
        if not settings.MQ_ENABLED or self.ready:
            return
        if time.monotonic() < self._next_connect_at:
            raise RuntimeError("RabbitMQ 重连退避中")
        async with self._connect_lock:
            if self.ready:
                return
            if time.monotonic() < self._next_connect_at:
                raise RuntimeError("RabbitMQ 重连退避中")
            import aio_pika

            try:
                connection = await asyncio.wait_for(
                    aio_pika.connect_robust(
                        settings.RABBITMQ_URL,
                        timeout=settings.MQ_CONFIRM_TIMEOUT_S,
                        client_properties={"connection_name": f"{settings.APP_NAME}-api"},
                    ),
                    timeout=settings.MQ_CONFIRM_TIMEOUT_S,
                )
                self._connection = connection
                self._channel = await connection.channel(
                    publisher_confirms=True, on_return_raises=True
                )
                await declare_topology(self._channel)
                self.last_error = None
                self._retry_delay_s = 1.0
                self._next_connect_at = 0.0
            except Exception as exc:
                self.last_error = str(exc)
                self._next_connect_at = time.monotonic() + self._retry_delay_s
                self._retry_delay_s = min(self._retry_delay_s * 2, 30.0)
                await self.close()
                raise

    async def publish(self, exchange_name: str, routing_key: str, body: bytes) -> None:
        import aio_pika

        if not self.ready:
            await self.connect()
        if self._channel is None:
            raise RuntimeError("RabbitMQ channel 未就绪")
        exchange = await self._channel.get_exchange(exchange_name, ensure=True)
        message = aio_pika.Message(
            body=body,
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        )
        await asyncio.wait_for(
            exchange.publish(message, routing_key=routing_key, mandatory=True),
            timeout=settings.MQ_CONFIRM_TIMEOUT_S,
        )

    async def publish_retry(self, queue_name: str, body: bytes) -> None:
        import aio_pika

        if not self.ready:
            await self.connect()
        if self._channel is None:
            raise RuntimeError("RabbitMQ channel 未就绪")
        await asyncio.wait_for(
            self._channel.default_exchange.publish(
                aio_pika.Message(
                    body=body,
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=queue_name,
                mandatory=True,
            ),
            timeout=settings.MQ_CONFIRM_TIMEOUT_S,
        )

    async def open_consumer_channel(self) -> Any:
        """为 Worker 返回独立手动 ack channel。"""
        if not self.ready:
            await self.connect()
        connection = self._connection
        if connection is None:
            raise RuntimeError("RabbitMQ connection 未就绪")
        channel = await connection.channel(publisher_confirms=True)
        await channel.set_qos(prefetch_count=settings.MQ_PREFETCH)
        await declare_topology(channel)
        return channel

    async def close(self) -> None:
        channel, connection = self._channel, self._connection
        self._channel = None
        self._connection = None
        if channel is not None and not channel.is_closed:
            await channel.close()
        if connection is not None and not connection.is_closed:
            await connection.close()


mq_connection = MQConnectionManager()
