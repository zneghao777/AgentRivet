"""Redis bridge for guard metrics emitted by independent worker processes."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import uuid
from typing import Any

from ..config import settings
from ..observability.metrics import metrics

logger = logging.getLogger(__name__)
_KEY_PREFIX = "guard_metrics:v1:"


class GuardMetricsBridge:
    """Publish local snapshots and optionally aggregate live remote snapshots."""

    def __init__(self) -> None:
        self.instance_id = (
            f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        )
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._collect_remote = False

    @property
    def key(self) -> str:
        return f"{_KEY_PREFIX}{self.instance_id}"

    def start(self, *, collect_remote: bool) -> None:
        if not settings.GUARD_ENABLED or not settings.REDIS_URL:
            return
        self._collect_remote = self._collect_remote or collect_remote
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(
            self._run(),
            name="guard-metrics-redis-bridge",
        )

    async def close(self) -> None:
        self._stop.set()
        task = self._task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        self._task = None
        metrics.set_remote_guard_snapshots({})

    async def _run(self) -> None:
        warned = False
        while not self._stop.is_set():
            client: Any | None = None
            try:
                from redis.asyncio import from_url

                client = from_url(
                    settings.REDIS_URL,
                    decode_responses=True,
                    socket_connect_timeout=0.5,
                    socket_timeout=0.5,
                )
                await client.ping()
                warned = False
                while not self._stop.is_set():
                    await client.set(
                        self.key,
                        json.dumps(
                            metrics.local_guard_snapshot(),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        ex=5,
                    )
                    if self._collect_remote:
                        await self._collect(client)
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=1.0)
                    except TimeoutError:
                        pass
                await client.delete(self.key)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                metrics.set_remote_guard_snapshots({})
                if not warned:
                    logger.warning("Guard 指标 Redis 桥接不可用，保留本进程指标: %s", exc)
                    warned = True
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=5.0)
                except TimeoutError:
                    pass
            finally:
                if client is not None:
                    await client.aclose()

    async def _collect(self, client: Any) -> None:
        snapshots: dict[str, dict] = {}
        async for key in client.scan_iter(match=f"{_KEY_PREFIX}*", count=100):
            if key == self.key:
                continue
            payload = await client.get(key)
            if not payload:
                continue
            try:
                snapshot = json.loads(payload)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(snapshot, dict):
                snapshots[str(key).removeprefix(_KEY_PREFIX)] = snapshot
        metrics.set_remote_guard_snapshots(snapshots)


guard_metrics_bridge = GuardMetricsBridge()


def start_guard_metrics_bridge(*, collect_remote: bool) -> None:
    guard_metrics_bridge.start(collect_remote=collect_remote)


async def stop_guard_metrics_bridge() -> None:
    await guard_metrics_bridge.close()
