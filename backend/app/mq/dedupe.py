"""消费者幂等：优先 Redis SET NX，失败时使用 PostgreSQL 凭据表。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..storage.db import AsyncSessionLocal
from ..storage.models import MQProcessedMessage

logger = logging.getLogger(__name__)
ClaimResult = Literal["claimed", "duplicate", "busy"]
_PROCESSING_LEASE_S = 30


class MessageDeduplicator:
    def __init__(self) -> None:
        self._redis: Any | None = None
        self._redis_failed = False

    async def _redis_client(self) -> Any | None:
        if not settings.REDIS_URL or self._redis_failed:
            return None
        if self._redis is None:
            try:
                from redis.asyncio import from_url

                client = from_url(settings.REDIS_URL, decode_responses=True)
                await client.ping()
                self._redis = client
            except Exception as exc:  # noqa: BLE001
                self._redis_failed = True
                self._redis = None
                logger.warning("MQ 幂等 Redis 不可用，降级 PostgreSQL: %s", exc)
        return self._redis

    @staticmethod
    def _key(message_id: str) -> str:
        return f"mq:dedupe:{message_id}"

    async def claim(self, message_id: str, message_type: str) -> ClaimResult:
        redis = await self._redis_client()
        if redis is not None:
            result = await redis.set(
                self._key(message_id),
                "processing",
                nx=True,
                ex=min(settings.MQ_DEDUPE_TTL_S, _PROCESSING_LEASE_S),
            )
            if result:
                return "claimed"
            return "duplicate" if await redis.get(self._key(message_id)) == "done" else "busy"
        return await self._claim_db(message_id, message_type)

    async def complete(self, message_id: str) -> None:
        redis = await self._redis_client()
        if redis is not None:
            await redis.set(
                self._key(message_id), "done", xx=True, ex=settings.MQ_DEDUPE_TTL_S
            )
            return
        async with AsyncSessionLocal() as db:
            row = await db.get(MQProcessedMessage, message_id)
            if row is not None:
                row.status = "done"
                row.processed_at = datetime.now(UTC)
                row.expires_at = datetime.now(UTC) + timedelta(
                    seconds=settings.MQ_DEDUPE_TTL_S
                )
                await db.commit()

    async def renew(self, message_id: str) -> None:
        """续租正在处理的消息，防止长任务尚未结束时被并发重复消费。"""
        redis = await self._redis_client()
        if redis is not None:
            key = self._key(message_id)
            if await redis.get(key) == "processing":
                await redis.expire(key, min(settings.MQ_DEDUPE_TTL_S, _PROCESSING_LEASE_S))
            return
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(MQProcessedMessage)
                .where(
                    MQProcessedMessage.message_id == message_id,
                    MQProcessedMessage.status == "processing",
                )
                .values(
                    expires_at=datetime.now(UTC)
                    + timedelta(
                        seconds=min(settings.MQ_DEDUPE_TTL_S, _PROCESSING_LEASE_S)
                    )
                )
            )
            await db.commit()

    async def release(self, message_id: str) -> None:
        redis = await self._redis_client()
        if redis is not None:
            await redis.delete(self._key(message_id))
            return
        async with AsyncSessionLocal() as db:
            await db.execute(
                delete(MQProcessedMessage).where(
                    MQProcessedMessage.message_id == message_id,
                    MQProcessedMessage.status == "processing",
                )
            )
            await db.commit()

    async def _claim_db(self, message_id: str, message_type: str) -> ClaimResult:
        now = datetime.now(UTC)
        async with AsyncSessionLocal() as db:
            existing = (
                await db.execute(
                    select(MQProcessedMessage).where(
                        MQProcessedMessage.message_id == message_id
                    )
                )
            ).scalar_one_or_none()
            if existing is not None and existing.expires_at > now:
                return "duplicate" if existing.status == "done" else "busy"
            if existing is not None:
                await db.delete(existing)
                await db.flush()
            db.add(
                MQProcessedMessage(
                    message_id=message_id,
                    message_type=message_type,
                    status="processing",
                    expires_at=now
                    + timedelta(
                        seconds=min(settings.MQ_DEDUPE_TTL_S, _PROCESSING_LEASE_S)
                    ),
                )
            )
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                return "busy"
            return "claimed"

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
        self._redis = None


deduplicator = MessageDeduplicator()
