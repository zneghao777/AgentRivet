"""审计日志：异步批量写入，避免阻塞请求。

detail 只允许非敏感摘要；禁止完整 prompt / API Key。

实现说明：queue/writer 按「事件循环」隔离（惰性创建），
避免在测试等多事件循环环境中出现 "Queue is bound to a different event loop"。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from ..storage import repositories as repo

logger = logging.getLogger(__name__)

# 审计写入策略：异步队列 + 后台任务；队列溢出降级同步写入。
_QUEUE_MAX = 1000
_db_factory: Callable[[], AsyncSession] | None = None

# 事件循环 → (queue, writer task)。key 用 id(loop)，writer done 后按需重建。
_state: dict[int, tuple[asyncio.Queue, asyncio.Task]] = {}


def init_audit(db_factory: Callable[[], AsyncSession]) -> None:
    """初始化审计数据库工厂（writer 按事件循环惰性启动）。"""
    global _db_factory
    _db_factory = db_factory


def _loop_state() -> tuple[asyncio.Queue, asyncio.Task]:
    """获取当前事件循环的 (queue, writer)；首次使用或 writer 终止时重建。"""
    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    state = _state.get(loop_id)
    if state is None or state[1].done():
        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        writer = asyncio.create_task(_write_loop(queue))
        state = (queue, writer)
        _state[loop_id] = state
    return state


async def _write_loop(queue: asyncio.Queue) -> None:
    """后台批量写审计日志。"""
    while True:
        item = await queue.get()
        try:
            if _db_factory is None:
                continue
            async with _db_factory() as db:
                await repo.add_audit(
                    db,
                    tenant_id=item["tenant_id"],
                    action=item["action"],
                    user_id=item.get("user_id"),
                    session_id=item.get("session_id"),
                    detail=item.get("detail"),
                    ip=item.get("ip"),
                    trace_id=item.get("trace_id"),
                    request_id=item.get("request_id"),
                    resource_id=item.get("resource_id"),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("审计日志写入失败: %s", exc)
        finally:
            queue.task_done()


async def audit(
    tenant_id: str,
    action: str,
    *,
    user_id: str | None = None,
    session_id: str | uuid.UUID | None = None,
    detail: dict | None = None,
    ip: str | None = None,
    trace_id: str | None = None,
    request_id: str | None = None,
    resource_id: str | None = None,
) -> None:
    """写入审计日志（异步队列）。

    detail 仅允许非敏感摘要。若队列已满（溢出），降级为同步写入。
    trace_id / request_id 未显式传入时，从当前 TraceContext 自动注入（§8.2 C-12）。
    """
    if (trace_id is None or request_id is None):
        try:
            from .tracing import get_trace_context

            ctx = get_trace_context()
            if ctx is not None:
                trace_id = trace_id or ctx.trace_id
                request_id = request_id or ctx.request_id
        except Exception:  # noqa: BLE001
            pass
    item = {
        "tenant_id": tenant_id,
        "action": action,
        "user_id": user_id,
        "session_id": session_id,
        "detail": detail,
        "ip": ip,
        "trace_id": trace_id,
        "request_id": request_id,
        "resource_id": resource_id,
    }
    try:
        queue, _ = _loop_state()
        queue.put_nowait(item)
    except asyncio.QueueFull:
        # 溢出降级：同步写
        logger.warning("审计队列已满，降级同步写入 action=%s", action)
        if _db_factory is not None:
            async with _db_factory() as db:
                await repo.add_audit(db, **item)


def sanitize_detail(detail: dict) -> dict:
    """过滤 detail 中的敏感字段。"""
    allowed = {"model", "token", "tools", "session_id", "status", "action"}
    return {k: v for k, v in detail.items() if k in allowed}
