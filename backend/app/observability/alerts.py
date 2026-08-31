"""SLO 与告警评估（OBSERVABILITY_SPEC §10.2 / §12.3）。

- 读取启用的 alert_rules（含内置默认 SLO 规则）；
- 按窗口计算指标当前值（错误率 / P95 延迟 / TTFT / 成本 / 工具错误率 / 队列使用率）；
- 触发条件满足 → 创建/保持 firing incident；恢复 → 标记 resolved；
- 状态机：pending → firing → acknowledged → resolved。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from ..config import settings

logger = logging.getLogger(__name__)

_POLL_SECONDS = 60  # 告警评估轮询间隔（秒）

# 内置默认 SLO 规则（仅当 alert_rules 表为空时注入，§10.2）
_DEFAULT_RULES = [
    {
        "name": "error-rate-budget",
        "metric": "error_rate",
        "threshold": settings.SLO_ERROR_RATE_BUDGET,
        "comparison": ">",
        "window_minutes": 15,
        "min_samples": 10,
    },
    {
        "name": "p95-latency",
        "metric": "p95_latency_ms",
        "threshold": settings.SLO_P95_LATENCY_MS,
        "comparison": ">",
        "window_minutes": 15,
        "min_samples": 10,
    },
    {
        "name": "p95-ttft",
        "metric": "p95_ttft_ms",
        "threshold": settings.SLO_P95_TTFT_MS,
        "comparison": ">",
        "window_minutes": 15,
        "min_samples": 10,
    },
    {
        "name": "tool-error-rate",
        "metric": "tool_error_rate",
        "threshold": settings.SLO_TOOL_ERROR_RATE,
        "comparison": ">",
        "window_minutes": 15,
        "min_samples": settings.SLO_TOOL_MIN_CALLS,
    },
    {
        "name": "queue-usage",
        "metric": "queue_usage",
        "threshold": settings.SLO_QUEUE_USAGE,
        "comparison": ">",
        "window_minutes": 15,
        "min_samples": 1,
    },
    {
        "name": "mq-deadletter",
        "metric": "mq_deadletter",
        "threshold": 0,
        "comparison": ">",
        "window_minutes": 5,
        "min_samples": 1,
    },
    {
        "name": "mq-session-queue-depth",
        "metric": "mq_session_queue_depth",
        "threshold": 1000,
        "comparison": ">",
        "window_minutes": 10,
        "min_samples": 1,
    },
    {
        "name": "mq-publish-fallback",
        "metric": "mq_publish_fallback_ratio",
        "threshold": 0.05,
        "comparison": ">",
        "window_minutes": 5,
        "min_samples": 1,
    },
    {
        "name": "guard-queue-wait-p95",
        "metric": "guard_queue_wait_p95_ms",
        "threshold": 30_000,
        "comparison": ">",
        "window_minutes": 15,
        "min_samples": 1,
        "severity": "warning",
    },
    {
        "name": "guard-queue-full-rate",
        "metric": "guard_queue_full_rate",
        "threshold": 10,
        "comparison": ">",
        "window_minutes": 5,
        "min_samples": 1,
        "severity": "critical",
    },
    {
        "name": "guard-circuit-open",
        "metric": "guard_cb_open_seconds",
        "threshold": 300,
        "comparison": ">",
        "window_minutes": 5,
        "min_samples": 1,
        "severity": "critical",
    },
    {
        "name": "guard-degrade-sustained",
        "metric": "guard_degrade_seconds",
        "threshold": 1800,
        "comparison": ">",
        "window_minutes": 30,
        "min_samples": 1,
        "severity": "warning",
    },
    {
        "name": "guard-upstream-429-rate",
        "metric": "guard_upstream_429_rate",
        "threshold": 10,
        "comparison": ">",
        "window_minutes": 5,
        "min_samples": 1,
        "severity": "warning",
    },
]


async def _evaluate_rule(db, rule, tenant_id: str) -> float | None:
    """计算规则指标当前值；无足够样本返回 None。"""
    window_start = datetime.now(UTC) - timedelta(minutes=rule.window_minutes)
    from sqlalchemy import func, select

    from ..storage.models import AgentSpan

    metric = rule.metric

    if metric == "error_rate":
        total = (
            await db.execute(
                select(func.count(AgentSpan.id)).where(
                    AgentSpan.tenant_id == tenant_id,
                    AgentSpan.span_type == "root",
                    AgentSpan.started_at >= window_start,
                )
            )
        ).scalar_one()
        if total < rule.min_samples:
            return None
        errors = (
            await db.execute(
                select(func.count(AgentSpan.id)).where(
                    AgentSpan.tenant_id == tenant_id,
                    AgentSpan.span_type == "root",
                    AgentSpan.status.in_(["error", "timeout", "canceled"]),
                    AgentSpan.started_at >= window_start,
                )
            )
        ).scalar_one()
        return errors / total

    if metric in ("p95_latency_ms", "p95_ttft_ms"):
        col = AgentSpan.duration_ms if metric == "p95_latency_ms" else AgentSpan.ttft_ms
        span_type = "root" if metric == "p95_latency_ms" else "llm"
        rows = (
            (
                await db.execute(
                    select(col).where(
                        AgentSpan.tenant_id == tenant_id,
                        AgentSpan.span_type == span_type,
                        col.isnot(None),
                        AgentSpan.started_at >= window_start,
                    )
                )
            )
            .scalars()
            .all()
        )
        if len(rows) < rule.min_samples:
            return None
        return float(_percentile(list(rows), 95))

    if metric == "tool_error_rate":
        tools = (
            await db.execute(
                select(func.count(AgentSpan.id)).where(
                    AgentSpan.tenant_id == tenant_id,
                    AgentSpan.span_type == "tool",
                    AgentSpan.started_at >= window_start,
                )
            )
        ).scalar_one()
        if tools < rule.min_samples:
            return None
        errors = (
            await db.execute(
                select(func.count(AgentSpan.id)).where(
                    AgentSpan.tenant_id == tenant_id,
                    AgentSpan.span_type == "tool",
                    AgentSpan.status == "error",
                    AgentSpan.started_at >= window_start,
                )
            )
        ).scalar_one()
        return errors / tools

    if metric == "queue_usage":
        from ..observability.metrics import metrics

        return metrics.queue_usage_ratio()

    if metric == "mq_deadletter":
        if not settings.MQ_DEADLETTER_ALERT_ENABLED:
            return None
        from ..observability.metrics import metrics

        return float(metrics.mq_deadletter_recent(rule.window_minutes))

    if metric == "mq_session_queue_depth":
        from ..mq.connection import SESSION_QUEUE
        from ..observability.metrics import metrics

        return float(metrics.mq_queue_depth_value(SESSION_QUEUE))

    if metric == "mq_publish_fallback_ratio":
        from ..observability.metrics import metrics

        return metrics.mq_publish_fallback_ratio_recent(rule.window_minutes)

    if metric == "guard_queue_wait_p95_ms":
        from ..observability.metrics import metrics

        return metrics.guard_queue_wait_p95_recent(rule.window_minutes)

    if metric == "guard_queue_full_rate":
        from ..observability.metrics import metrics

        return metrics.guard_rejected_rate_recent("queue_full", rule.window_minutes)

    if metric == "guard_cb_open_seconds":
        from ..observability.metrics import metrics

        return metrics.guard_cb_open_seconds()

    if metric == "guard_degrade_seconds":
        from ..observability.metrics import metrics

        return metrics.guard_degrade_seconds()

    if metric == "guard_upstream_429_rate":
        from ..observability.metrics import metrics

        return metrics.guard_upstream_429_rate_recent(rule.window_minutes)

    if metric == "cost_total":
        from ..storage.models import AgentSpan as SpanModel

        cost = (
            await db.execute(
                select(func.coalesce(func.sum(SpanModel.cost_total), 0.0)).where(
                    SpanModel.tenant_id == tenant_id,
                    SpanModel.span_type.in_(["llm", "embedding"]),
                    SpanModel.started_at >= window_start,
                )
            )
        ).scalar_one()
        return float(cost)

    return None


def _compare(value: float, threshold: float, comparison: str) -> bool:
    if comparison == ">":
        return value > threshold
    if comparison == ">=":
        return value >= threshold
    if comparison == "<":
        return value < threshold
    if comparison == "<=":
        return value <= threshold
    return value == threshold


async def _evaluate_all() -> None:
    from ..storage import repositories as repo
    from ..storage.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        # 取出所有租户（有 root span 的租户）
        from sqlalchemy import select

        from ..storage.models import AgentSpan

        tenant_ids = set((await db.execute(select(AgentSpan.tenant_id).distinct())).scalars().all())
        # 无租户可评估
        rules = await repo.list_alert_rules(db, tenant_id="__all__")
        existing_names = {rule.name for rule in rules}
        missing_defaults = [r for r in _DEFAULT_RULES if r["name"] not in existing_names]
        if missing_defaults:
            # 幂等补齐新增内置规则，避免已有环境永远缺少后续版本规则。
            for r in missing_defaults:
                await repo.create_alert_rule(db, values={**r, "tenant_id": "__all__"})
            rules = await repo.list_alert_rules(db, tenant_id="__all__")
        default_severities = {
            rule["name"]: rule.get("severity", "warning") for rule in _DEFAULT_RULES
        }
        for rule in rules:
            expected_severity = default_severities.get(rule.name)
            if expected_severity and rule.severity != expected_severity:
                await repo.update_alert_rule(
                    db,
                    rule.id,
                    values={"severity": expected_severity},
                )
                rule.severity = expected_severity

        guard_rules = [rule for rule in rules if rule.metric.startswith("guard_")]
        tenant_rules = [rule for rule in rules if not rule.metric.startswith("guard_")]
        for tenant_id in tenant_ids or {settings.DEFAULT_TENANT}:
            await _evaluate_for_tenant(db, tenant_id, tenant_rules)
        # Guard metrics are process/system-level rather than tenant-labelled. Evaluate
        # them once to avoid duplicating the same incident into every active tenant.
        if guard_rules:
            await _evaluate_for_tenant(db, settings.DEFAULT_TENANT, guard_rules)


async def _evaluate_for_tenant(db, tenant_id: str, rules) -> None:
    from ..storage import repositories as repo

    for rule in rules:
        if not rule.enabled:
            continue
        # 租户级覆盖：先查租户自己的规则；没有则用全局规则
        try:
            tenant_rule = await repo.get_alert_rule_by_name(db, tenant_id, rule.name)
        except AttributeError:
            tenant_rule = None
        target = tenant_rule or rule
        try:
            value = await _evaluate_rule(db, target, tenant_id)
        except Exception:  # noqa: BLE001
            logger.warning(
                "告警指标评估失败 rule=%s tenant=%s", target.name, tenant_id, exc_info=True
            )
            continue
        if value is None:
            continue

        # 当前是否有未关闭的 incident
        open_incidents = [
            i
            for i in await repo.list_alert_incidents(
                db, tenant_id=tenant_id, status=None, limit=100
            )
            if i.rule_id == target.id and i.status in ("pending", "firing", "acknowledged")
        ]
        is_breaching = _compare(value, target.threshold, target.comparison)

        if is_breaching:
            if not open_incidents:
                # 创建 firing incident（§10.2：附相关 Trace）
                related = await _related_trace_ids(db, tenant_id, target.metric)
                await repo.create_alert_incident(
                    db,
                    values={
                        "tenant_id": tenant_id,
                        "rule_id": target.id,
                        "status": "firing",
                        "severity": target.severity,
                        "metric": target.metric,
                        "threshold": target.threshold,
                        "current_value": round(value, 6),
                        "window_minutes": target.window_minutes,
                        "message": (
                            f"{target.name}: {target.metric}={value:.4f} (阈值 {target.threshold})"
                        ),
                        "related_trace_ids": related,
                    },
                )
                log = logger.critical if target.severity == "critical" else logger.warning
                log("告警触发 tenant=%s rule=%s value=%.4f", tenant_id, target.name, value)
            else:
                inc = open_incidents[0]
                await repo.update_alert_incident(
                    db,
                    inc.id,
                    values={"status": "firing", "current_value": round(value, 6)},
                )
        else:
            for inc in open_incidents:
                if inc.status != "resolved":
                    await repo.update_alert_incident(
                        db, inc.id, values={"status": "resolved", "resolved_at": datetime.now(UTC)}
                    )


async def _related_trace_ids(db, tenant_id: str, metric: str) -> list[str]:
    from sqlalchemy import select

    from ..storage.models import AgentSpan

    if metric == "tool_error_rate":
        stmt = (
            select(AgentSpan.trace_id)
            .where(
                AgentSpan.tenant_id == tenant_id,
                AgentSpan.span_type == "tool",
                AgentSpan.status == "error",
            )
            .order_by(AgentSpan.started_at.desc())
            .limit(5)
        )
    else:
        stmt = (
            select(AgentSpan.trace_id)
            .where(
                AgentSpan.tenant_id == tenant_id,
                AgentSpan.span_type == "root",
                AgentSpan.status.in_(["error", "timeout", "canceled"]),
            )
            .order_by(AgentSpan.started_at.desc())
            .limit(5)
        )
    return list((await db.execute(stmt)).scalars().all())


def _percentile(values: list[int], p: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


async def _worker() -> None:
    while True:
        await asyncio.sleep(_POLL_SECONDS)
        try:
            await _evaluate_all()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("告警评估任务异常: %s", exc)


_task: asyncio.Task | None = None


def start_alert_worker() -> None:
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_worker())


async def stop_alert_worker() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
