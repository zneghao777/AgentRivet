"""记忆卫生后台任务（MEMORY_SPEC §6.3.1）：周期性 TTL 过期扫描。

- 每 24h 运行一次，遍历所有 (tenant_id, user_id) 组合，对各自用户执行 TTL 过期。
- 失败只记录日志、不抛出，绝不中断应用主进程。
- 通过 lifespan 的独立 asyncio task 调度，启动后常驻。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select

from ..storage.db import AsyncSessionLocal
from ..storage.models import MemoryEntry

logger = logging.getLogger(__name__)

# TTL 扫描周期（秒）：24h；环境变量可覆盖（测试/运维用）
MEMORY_TTL_SCAN_INTERVAL_SECONDS = 24 * 3600


async def _run_ttl_scan_once() -> int:
    """执行一轮 TTL 过期扫描，返回过期的记忆总条数（0 失败可回退为空）。"""
    expired_total = 0
    async with AsyncSessionLocal() as db:
        # 遍历所有 (tenant_id, user_id) 组合（用户级 TTL 独立计算）
        # 注意：需对组合去重（SELECT DISTINCT tenant_id, user_id），不能用两个独立 distinct
        pairs = (
            await db.execute(
                select(MemoryEntry.tenant_id, MemoryEntry.user_id).distinct()
            )
        ).fetchall()
        for tenant_id, user_id in pairs:
            if not tenant_id or not user_id:
                continue
            try:
                # 该用户的 TTL 从 memory_settings 读取；不存在则用默认值
                from ..memory.hygiene import expire_stale

                n = await expire_stale(db, tenant_id=tenant_id, user_id=user_id)
                expired_total += n
            except Exception as exc:  # noqa: BLE001
                logger.warning("记忆 TTL 扫描失败 tenant=%s user=%s: %s", tenant_id, user_id, exc)
    return expired_total


async def _ttl_scan_loop() -> None:
    """后台循环：每 MEMORY_TTL_SCAN_INTERVAL_SECONDS 扫一轮。"""
    logger.info("记忆 TTL 扫描任务已启动（周期 %ds）", MEMORY_TTL_SCAN_INTERVAL_SECONDS)
    while True:
        try:
            expired = await _run_ttl_scan_once()
            if expired:
                logger.info("记忆 TTL 扫描：过期 %d 条", expired)
        except Exception as exc:  # noqa: BLE001
            logger.warning("记忆 TTL 扫描异常: %s", exc)
        await asyncio.sleep(MEMORY_TTL_SCAN_INTERVAL_SECONDS)


_ttl_task: asyncio.Task | None = None


def start_ttl_scan() -> asyncio.Task | None:
    """启动后台 TTL 扫描任务（幂等），返回 task 供 lifespan 关闭。"""
    global _ttl_task
    if _ttl_task is not None and not _ttl_task.done():
        return _ttl_task
    _ttl_task = asyncio.create_task(_ttl_scan_loop())
    return _ttl_task


async def stop_ttl_scan() -> None:
    """停止后台 TTL 扫描任务。"""
    global _ttl_task
    if _ttl_task is not None:
        _ttl_task.cancel()
        try:
            await _ttl_task
        except asyncio.CancelledError:
            pass
        _ttl_task = None
