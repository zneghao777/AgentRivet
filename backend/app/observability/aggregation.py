"""指标聚合与数据清理后台任务（OBSERVABILITY_SPEC §2.4 / §2.7）。

- 每日指标聚合：从 agent_spans 聚合写入 agent_metrics_daily（重启不丢）。
- 数据保留清理：按 TRACE_RETENTION_DAYS 删除过期 span。
- 启动时立即跑一次兜底聚合 + 定期轮询（简单轮询实现 cron）。
"""

from __future__ import annotations

import asyncio
import logging
import statistics
from datetime import UTC, date, datetime, timedelta

from ..config import settings

logger = logging.getLogger(__name__)

# 轮询间隔（秒）：5 分钟，兼顾 Trace 汇总、小时聚合与成本校准的时效性
_POLL_SECONDS = 300
# 记录上次聚合/清理日期，避免重复执行
_last_agg_day: date | None = None


def _today() -> date:
    return datetime.now(UTC).date()


async def _run_aggregation() -> None:
    """聚合最近一天（含今天）的 span 数据到 agent_metrics_daily。"""
    from ..storage import repositories as repo
    from ..storage.db import AsyncSessionLocal

    today = _today()
    start = datetime.combine(today, datetime.min.time(), tzinfo=UTC)
    end = start + timedelta(days=1)

    async with AsyncSessionLocal() as db:
        from sqlalchemy import select

        from ..storage.models import AgentSpan

        result = await db.execute(
            select(AgentSpan).where(
                AgentSpan.span_type == "root",
                AgentSpan.started_at >= start,
                AgentSpan.started_at < end,
            )
        )
        roots = list(result.scalars().all())
        # 今日所有 tool spans
        result = await db.execute(
            select(AgentSpan).where(
                AgentSpan.span_type == "tool",
                AgentSpan.started_at >= start,
                AgentSpan.started_at < end,
            )
        )
        tools = list(result.scalars().all())

        if not roots:
            logger.info("指标聚合：今日暂无 root span，跳过")
            return

        # 按租户分组聚合（C-11：多租户指标隔离，避免串租）
        tenant_groups: dict[str, list[AgentSpan]] = {}
        for s in roots:
            tenant_groups.setdefault(s.tenant_id, []).append(s)
        tool_tenants: dict[str, list[AgentSpan]] = {}
        for s in tools:
            tool_tenants.setdefault(s.tenant_id, []).append(s)

        for tenant_id, tenant_roots in tenant_groups.items():
            tenant_tools = tool_tenants.get(tenant_id, [])
            requests = len(tenant_roots)
            errors = sum(1 for s in tenant_roots if s.status in ("error", "timeout", "canceled"))
            tokens_prompt = sum((s.token_usage or {}).get("prompt", 0) for s in tenant_roots)
            tokens_completion = sum((s.token_usage or {}).get("completion", 0) for s in tenant_roots)
            tokens_total = tokens_prompt + tokens_completion
            tool_calls = len(tenant_tools)
            durations = [s.duration_ms for s in tenant_roots if s.duration_ms > 0]
            latency_p50 = int(statistics.median(durations)) if durations else 0
            latency_p95 = int(_percentile(durations, 95)) if durations else 0
            duration_avg = int(sum(durations) / len(durations)) if durations else 0

            await repo.upsert_metric_day(
                db,
                tenant_id=tenant_id,
                day=datetime.combine(today, datetime.min.time(), tzinfo=UTC),
                values={
                    "requests": requests,
                    "errors": errors,
                    "tokens_prompt": tokens_prompt,
                    "tokens_completion": tokens_completion,
                    "tokens_total": tokens_total,
                    "tool_calls": tool_calls,
                    "latency_p50_ms": latency_p50,
                    "latency_p95_ms": latency_p95,
                    "duration_avg_ms": duration_avg,
                },
            )
            logger.info(
                "指标聚合完成 tenant=%s day=%s requests=%d errors=%d tokens_total=%d",
                tenant_id,
                today,
                requests,
                errors,
                tokens_total,
            )


async def _run_cleanup() -> None:
    """按保留策略清理过期数据（OBSERVABILITY_SPEC §9.4）。"""
    from ..storage import repositories as repo
    from ..storage.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        if settings.TRACE_RETENTION_DAYS > 0:
            before_span = datetime.now(UTC) - timedelta(days=settings.TRACE_RETENTION_DAYS)
            deleted = await repo.delete_expired_spans(db, before=before_span)
            await repo.delete_expired_traces(db, before=before_span)
            if deleted:
                logger.info("数据保留清理：删除 %d 条过期 span", deleted)
        if settings.LOG_RETENTION_DAYS > 0:
            before_log = datetime.now(UTC) - timedelta(days=settings.LOG_RETENTION_DAYS)
            deleted_logs = await repo.delete_expired_logs(db, before=before_log)
            if deleted_logs:
                logger.info("数据保留清理：删除 %d 条过期日志", deleted_logs)
        if settings.HOURLY_RETENTION_DAYS > 0:
            before_metric = datetime.now(UTC) - timedelta(days=settings.HOURLY_RETENTION_DAYS)
            deleted_metrics = await repo.delete_expired_metrics(db, before=before_metric)
            if deleted_metrics:
                logger.info("数据保留清理：删除 %d 条过期小时聚合", deleted_metrics)


async def _run_trace_summary() -> None:
    """为缺失的 Trace 生成汇总行（agent_traces，OBSERVABILITY_SPEC §9.1）。

    以 agent_spans 根 span 为准，聚合 span 数、工具数、token、耗时、成本、模型列表。
    """
    from ..storage import repositories as repo
    from ..storage.db import AsyncSessionLocal
    from sqlalchemy import select

    from ..storage.models import AgentSpan, AgentTrace

    async with AsyncSessionLocal() as db:
        existing = set(
            (await db.execute(select(AgentTrace.trace_id))).scalars().all()
        )
        # 取最近 24h 的根 span（避免全表扫描）
        start = datetime.now(UTC) - timedelta(hours=24)
        roots = list(
            (
                await db.execute(
                    select(AgentSpan).where(
                        AgentSpan.span_type == "root",
                        AgentSpan.started_at >= start,
                    )
                )
            ).scalars().all()
        )
        written = 0
        for root in roots:
            if root.trace_id in existing:
                continue
            # 该 trace 的全部 span
            spans = await repo.query_trace_spans(db, root.trace_id)
            llm_spans = [s for s in spans if s.span_type in ("llm", "embedding")]
            tool_spans = [s for s in spans if s.span_type == "tool"]
            tokens = {"input": 0, "output": 0, "total": 0}
            for s in llm_spans:
                tu = s.token_usage or {}
                tokens["input"] += tu.get("input", 0)
                tokens["output"] += tu.get("output", 0)
                tokens["total"] += tu.get("total", 0)
            model_names = sorted({s.model for s in llm_spans if s.model})
            cost = sum(float(s.cost_total or 0) for s in llm_spans)
            attrs = root.attributes or {}
            await repo.upsert_trace_summary(
                db,
                trace_id=root.trace_id,
                values={
                    "request_id": getattr(root, "request_id", None),
                    "tenant_id": root.tenant_id,
                    "user_id": root.user_id,
                    "session_id": root.session_id,
                    "thread_id": getattr(root, "thread_id", None),
                    "name": root.span_name,
                    "status": root.status,
                    "model_names": model_names or None,
                    "span_count": len(spans),
                    "llm_calls": len(llm_spans),
                    "tool_calls": len(tool_spans),
                    "token_usage": tokens if any(tokens.values()) else None,
                    "cost": round(cost, 8),
                    "duration_ms": root.duration_ms,
                    "ttft_ms": getattr(root, "ttft_ms", 0) or 0,
                    "input_preview": attrs.get("user_message") or root.input_summary,
                    "output_preview": root.output_summary,
                    "error_type": root.error.split(":")[0] if root.error else None,
                    "error_message": root.error,
                    "started_at": root.started_at,
                    "ended_at": root.ended_at,
                    "sampled": True,
                    "content_capture_mode": getattr(root, "content_capture_mode", None),
                },
            )
            existing.add(root.trace_id)
            written += 1
        if written:
            logger.info("Trace 汇总：新增 %d 条", written)


async def _run_hourly_aggregation() -> None:
    """生成最近 24h 的小时聚合（agent_metrics_hourly，OBSERVABILITY_SPEC §9.3）。"""
    import statistics as _stats

    from ..storage import repositories as repo
    from ..storage.db import AsyncSessionLocal
    from sqlalchemy import select

    from ..storage.models import AgentSpan

    now = datetime.now(UTC)
    start = now - timedelta(hours=24)
    async with AsyncSessionLocal() as db:
        roots = list(
            (
                await db.execute(
                    select(AgentSpan).where(
                        AgentSpan.span_type == "root",
                        AgentSpan.started_at >= start,
                    )
                )
            ).scalars().all()
        )
        by_bucket: dict[tuple[str, datetime], list[AgentSpan]] = {}
        for root in roots:
            bucket = root.started_at.replace(minute=0, second=0, microsecond=0)
            by_bucket.setdefault((root.tenant_id, bucket), []).append(root)
        for (tenant_id, bucket), spans in by_bucket.items():
            requests = len(spans)
            errors = sum(1 for s in spans if s.status in ("error", "timeout", "canceled"))
            tokens_input = sum((s.token_usage or {}).get("input", 0) for s in spans)
            tokens_output = sum((s.token_usage or {}).get("output", 0) for s in spans)
            tokens_total = sum((s.token_usage or {}).get("total", 0) for s in spans)
            cost = sum(float(s.cost_total or 0) for s in spans)
            durations = [s.duration_ms for s in spans if s.duration_ms > 0]
            ttfts = [s.ttft_ms for s in spans if s.ttft_ms]
            tool_calls = sum(
                1
                for s in await db.execute(
                    select(AgentSpan).where(
                        AgentSpan.tenant_id == tenant_id,
                        AgentSpan.span_type == "tool",
                        AgentSpan.started_at >= bucket,
                        AgentSpan.started_at < bucket + timedelta(hours=1),
                    )
                ).scalars().all()
            )
            await repo.upsert_metric_hourly(
                db,
                tenant_id=tenant_id,
                bucket=bucket,
                values={
                    "requests": requests,
                    "errors": errors,
                    "timeouts": sum(1 for s in spans if s.status == "timeout"),
                    "canceled": sum(1 for s in spans if s.status == "canceled"),
                    "tokens_input": tokens_input,
                    "tokens_output": tokens_output,
                    "tokens_total": tokens_total,
                    "cost_total": round(cost, 8),
                    "tool_calls": tool_calls,
                    "latency_p50_ms": int(_stats.median(durations)) if durations else 0,
                    "latency_p95_ms": int(_percentile(durations, 95)) if durations else 0,
                    "latency_p99_ms": int(_percentile(durations, 99)) if durations else 0,
                    "ttft_p95_ms": int(_percentile(ttfts, 95)) if ttfts else 0,
                    "duration_avg_ms": int(sum(durations) / len(durations)) if durations else 0,
                },
            )


async def _run_cost_backfill() -> None:
    """按 model_price_versions 校准最近 24h LLM/Embedding span 成本（OBSERVABILITY_SPEC §6.4）。

    graph_runtime 关闭 span 时用内置默认价目表即时计算；本任务用带生效时间的价格版本
    校准，保证“不把供应商当前价格回填到历史调用”。
    """
    from sqlalchemy import select

    from ..storage import repositories as repo
    from ..storage.db import AsyncSessionLocal
    from ..storage.models import AgentSpan

    async with AsyncSessionLocal() as db:
        start = datetime.now(UTC) - timedelta(hours=24)
        spans = list(
            (
                await db.execute(
                    select(AgentSpan).where(
                        AgentSpan.span_type.in_(["llm", "embedding"]),
                        AgentSpan.token_usage.isnot(None),
                        AgentSpan.started_at >= start,
                    )
                )
            ).scalars().all()
        )
        corrected = 0
        for span in spans:
            price = await repo.get_price_at(db, span.model or "", at=span.started_at)
            if price is None:
                continue
            from ..observability.costing import compute_cost

            cost = compute_cost(
                span.model or "",
                span.token_usage,
                input_price_per_million=float(price.input_price_per_million or 0),
                cached_input_price_per_million=float(price.cached_input_price_per_million or 0),
                output_price_per_million=float(price.output_price_per_million or 0),
                currency=price.currency or "USD",
            )
            span.cost_input = cost["cost_input"]
            span.cost_output = cost["cost_output"]
            span.cost_total = cost["cost_total"]
            span.currency = cost["currency"]
            span.model_provider = price.model_provider
            span.model_name_resolved = price.model_name
            corrected += 1
        if corrected:
            await db.commit()
            logger.info("成本校准：按价格版本更新 %d 条 LLM/Embedding span", corrected)


async def _worker() -> None:
    """后台循环：每次启动先兜底聚合一次，之后定期轮询。"""
    global _last_agg_day
    # 启动即兜底聚合一次（保证重启不丢当日数据）
    try:
        await _run_aggregation()
        await _run_trace_summary()
        await _run_hourly_aggregation()
        await _run_cost_backfill()
        _last_agg_day = _today()
    except Exception as exc:  # noqa: BLE001
        logger.warning("启动指标聚合失败: %s", exc)

    while True:
        await asyncio.sleep(_POLL_SECONDS)
        try:
            # 每日聚合：跨天时执行
            if _last_agg_day != _today():
                await _run_aggregation()
                _last_agg_day = _today()
            # 高频任务：Trace 汇总 + 小时聚合 + 成本校准（每 5 分钟）
            await _run_trace_summary()
            await _run_hourly_aggregation()
            await _run_cost_backfill()
            # 数据保留清理：每天一次
            await _run_cleanup()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("指标聚合/清理任务异常: %s", exc)


_task: asyncio.Task | None = None


def start_metric_worker() -> None:
    """启动指标聚合与清理后台任务（幂等）。"""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_worker())


async def stop_metric_worker() -> None:
    """停止后台任务。"""
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None


def _percentile(values: list[int], p: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)
