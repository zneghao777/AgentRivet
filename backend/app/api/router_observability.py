"""可观测性路由（OBSERVABILITY_SPEC v2 §12）：总览、Trace、调用、日志、成本、质量、告警。

安全：所有接口均 require_auth，并按当前 AuthContext.tenant_id 过滤（防跨租户读取）。
兼容：/tool-logs 保留并重定向到 /calls?type=tool（§3）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..observability.metrics import metrics
from ..storage import repositories as repo
from ..storage.db import get_db
from ..storage.models import AgentSpan, Message
from .deps import AuthContext, require_auth
from .schemas import (
    AlertIncidentUpdateRequest,
    AlertRuleCreateRequest,
    ApiResponse,
    EvaluationRunCreateRequest,
    ObservabilityOverview,
    PriceCreateRequest,
    ScoreCreateRequest,
    SpanNode,
    ToolLogItem,
    TraceDetail,
    TraceExportRequest,
    TraceListItem,
)

router = APIRouter(prefix="/observability", tags=["observability"])


# ---------- 辅助 ----------


def _dt(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts else None


def _parse_dt(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def _span_to_trace_item(span: AgentSpan, span_count: int = 0, tool_count: int = 0) -> TraceListItem:
    return TraceListItem(
        trace_id=span.trace_id,
        span_id=span.span_id,
        session_id=str(span.session_id) if span.session_id else None,
        span_name=span.span_name,
        status=span.status,
        started_at=_dt(span.started_at),
        ended_at=_dt(span.ended_at),
        duration_ms=span.duration_ms,
        model=span.model,
        token_usage=span.token_usage,
        input_summary=span.input_summary,
        output_summary=span.output_summary,
        error=span.error,
        span_count=span_count,
        tool_count=tool_count,
    )


def _build_tree(spans: list[AgentSpan]) -> SpanNode | None:
    """按 parent_span_id 组装 span 树（根为 parent_span_id=None）。"""
    if not spans:
        return None
    by_id = {
        s.span_id: SpanNode(
            span_id=s.span_id,
            parent_span_id=s.parent_span_id,
            span_name=s.span_name,
            span_type=s.span_type,
            status=s.status,
            model=s.model,
            token_usage=s.token_usage,
            input_summary=s.input_summary,
            output_summary=s.output_summary,
            duration_ms=s.duration_ms,
            started_at=_dt(s.started_at),
            ended_at=_dt(s.ended_at),
            error=s.error,
            children=[],
        )
        for s in spans
    }
    root = None
    for s in spans:
        node = by_id[s.span_id]
        if s.parent_span_id and s.parent_span_id in by_id:
            by_id[s.parent_span_id].children.append(node)
        elif root is None:
            root = node
    return root


async def _count_children(db: AsyncSession, trace_id: str) -> tuple[int, int]:
    """统计 trace 的 span 总数与工具数（不含根）。"""
    from sqlalchemy import func, select

    total = (
        await db.execute(
            select(func.count(AgentSpan.id)).where(AgentSpan.trace_id == trace_id)
        )
    ).scalar_one()
    tools = (
        await db.execute(
            select(func.count(AgentSpan.id)).where(
                AgentSpan.trace_id == trace_id, AgentSpan.span_type == "tool"
            )
        )
    ).scalar_one()
    return total, tools


def _token_value(usage: dict | None, canonical: str, legacy: str) -> int:
    data = usage or {}
    return int(data.get(canonical, data.get(legacy, 0)) or 0)


def _span_event_kind(span: AgentSpan) -> str:
    if span.span_type == "llm":
        return "assistant"
    if span.span_type == "tool":
        return "tool"
    if span.span_type in {"retriever", "memory"}:
        return "context"
    return "node"


def _span_lane(span: AgentSpan) -> str | None:
    if span.span_type == "llm":
        return "model"
    if span.span_type == "tool":
        return "tools"
    return None


def _safe_span_raw(span: AgentSpan, capture_mode: str) -> dict:
    """按采集策略返回检视原始数据，metadata_only 不泄露内容字段。"""
    attributes = span.attributes or {}
    base = {
        "span_id": span.span_id,
        "parent_span_id": span.parent_span_id,
        "run_id": span.run_id,
        "parent_run_id": span.parent_run_id,
        "span_name": span.span_name,
        "span_type": span.span_type,
        "status": span.status,
        "model": span.model,
        "model_provider": span.model_provider,
        "model_name_requested": span.model_name_requested,
        "model_name_resolved": span.model_name_resolved,
        "finish_reason": span.finish_reason,
        "token_usage": span.token_usage,
        "ttft_ms": span.ttft_ms,
        "retry_count": span.retry_count,
        "duration_ms": span.duration_ms,
        "started_at": _dt(span.started_at),
        "ended_at": _dt(span.ended_at),
        "error": span.error,
        "attribute_keys": sorted((span.attributes or {}).keys()),
        "events": span.events or [],
        "task_id": attributes.get("task_id"),
        "agent_name": attributes.get("agent_name"),
    }
    if capture_mode != "metadata_only":
        base.update(
            {
                "input_summary": span.input_summary,
                "output_summary": span.output_summary,
                "attributes": span.attributes,
                "model_parameters": span.model_parameters,
            }
        )
    return base


def _hourly_buckets(start: datetime, end: datetime) -> list[dict]:
    """生成近 N 小时的空桶序列（整点对齐、UTC 归一，与 span 桶 key 一致）。"""
    buckets: list[dict] = []
    cur = _floor_hour(start)
    while cur < end:
        buckets.append(
            {
                "ts": cur.isoformat(),
                "requests": 0,
                "errors": 0,
                "tokens_prompt": 0,
                "tokens_completion": 0,
                "latency_p50_ms": 0,
                "latency_p95_ms": 0,
            }
        )
        cur += timedelta(hours=1)
    return buckets


# ---------- 总览 ----------


@router.get("/overview", response_model=ApiResponse)
async def overview(
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """Dashboard 总览：今日卡片 + 近 24h 折线 + 工具 Top + 最近异常 + 采集健康。"""
    tenant_id = auth.tenant.tenant_id
    now = datetime.now(UTC)
    # "今日"按服务器本地时区取零点（UTC 零点会把本地 00:00~08:00 的请求算进昨天）
    local = now.astimezone()
    today_start = datetime.combine(local.date(), datetime.min.time(), tzinfo=local.tzinfo)

    from sqlalchemy import select

    today_roots = list(
        (
            await db.execute(
                select(AgentSpan)
                .where(
                    AgentSpan.tenant_id == tenant_id,
                    AgentSpan.span_type == "root",
                    AgentSpan.started_at >= today_start,
                )
                .order_by(AgentSpan.started_at.asc())
            )
        ).scalars().all()
    )

    today_requests = len(today_roots)
    today_errors = sum(1 for s in today_roots if s.status in ("error", "timeout", "canceled"))
    today_durations = [s.duration_ms for s in today_roots if s.duration_ms > 0]
    today_tokens = {"prompt": 0, "completion": 0, "total": 0}
    for s in today_roots:
        tu = s.token_usage or {}
        today_tokens["prompt"] += tu.get("prompt", tu.get("input", 0))
        today_tokens["completion"] += tu.get("completion", tu.get("output", 0))
        today_tokens["total"] += tu.get("total", 0)
    today_latency_p50 = _percentile(today_durations, 50) if today_durations else 0
    today_latency_p95 = _percentile(today_durations, 95) if today_durations else 0

    # 近 24h 折线（窗口按整点对齐：24 个整点桶，末桶为当前小时）
    trend_start = _floor_hour(now) - timedelta(hours=23)
    buckets = _hourly_buckets(trend_start, now)
    bucket_index = {b["ts"]: b for b in buckets}
    recent_spans = list(
        (
            await db.execute(
                select(AgentSpan)
                .where(
                    AgentSpan.tenant_id == tenant_id,
                    AgentSpan.span_type == "root",
                    AgentSpan.started_at >= trend_start,
                )
            )
        ).scalars().all()
    )
    hour_durations: dict[str, list[int]] = {}
    for s in recent_spans:
        key = _floor_hour(s.started_at).isoformat()
        bucket = bucket_index.get(key)
        if bucket is None:
            continue
        bucket["requests"] += 1
        if s.status in ("error", "timeout", "canceled"):
            bucket["errors"] += 1
        tu = s.token_usage or {}
        bucket["tokens_prompt"] += tu.get("prompt", tu.get("input", 0))
        bucket["tokens_completion"] += tu.get("completion", tu.get("output", 0))
        hour_durations.setdefault(key, []).append(s.duration_ms)
    for key, vals in hour_durations.items():
        bucket = bucket_index.get(key)
        if bucket:
            bucket["latency_p50_ms"] = _percentile(vals, 50)
            bucket["latency_p95_ms"] = _percentile(vals, 95)

    tool_stats = await repo.aggregate_tool_stats(db, tenant_id=tenant_id)
    recent_errors = await repo.recent_error_traces(db, tenant_id=tenant_id, limit=5)
    recent_error_items = []
    for s in recent_errors:
        count, tools = await _count_children(db, s.trace_id)
        recent_error_items.append(_span_to_trace_item(s, count, tools).model_dump())

    data = ObservabilityOverview(
        today_requests=today_requests,
        today_success_rate=round((today_requests - today_errors) / today_requests, 4)
        if today_requests
        else 1.0,
        today_errors=today_errors,
        today_tokens=today_tokens,
        today_latency_p50_ms=today_latency_p50,
        today_latency_p95_ms=today_latency_p95,
        active_sessions=metrics.active_session_count(),
        trend=buckets,
        tool_stats=tool_stats,
        recent_errors=recent_error_items,
    )
    return ApiResponse(data=data.model_dump())


@router.get("/health", response_model=ApiResponse)
async def collector_health(
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """采集器健康（§12.1 / §15.8）：队列、写入器、丢弃、最近聚合。"""
    from sqlalchemy import func, select

    from ..storage.models import AgentMetricsHourly

    latest_hour = (
        await db.execute(
            select(func.max(AgentMetricsHourly.bucket)).where(
                AgentMetricsHourly.tenant_id == auth.tenant.tenant_id
            )
        )
    ).scalar_one_or_none()
    return ApiResponse(
        data={
            **metrics.collector_health(),
            "latest_aggregation": _dt(latest_hour),
            "data_lag_seconds": int((datetime.now(UTC) - latest_hour).total_seconds())
            if latest_hour
            else None,
        }
    )


@router.get("/router-stats", response_model=ApiResponse)
async def router_stats(auth: AuthContext = Depends(require_auth)):
    """返回当前进程内的 Router 命中分布与平均置信度。"""
    return ApiResponse(data=metrics.router_stats(auth.tenant.tenant_id))


@router.get("/metrics", response_model=ApiResponse)
async def query_metrics(
    metric: list[str] = Query(default=["requests"]),
    start_from: str | None = Query(None, alias="from"),
    start_to: str | None = Query(None, alias="to"),
    granularity: str = Query("hour", pattern="^(hour|day)$"),
    group_by: str | None = Query(None),
    status: str | None = Query(None),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """多指标时间序列（§12.1）。"""
    from sqlalchemy import select

    tenant_id = auth.tenant.tenant_id
    now = datetime.now(UTC)
    start = _parse_dt(start_from) or (now - timedelta(hours=24))
    end = _parse_dt(start_to) or now

    spans = list(
        (
            await db.execute(
                select(AgentSpan)
                .where(
                    AgentSpan.tenant_id == tenant_id,
                    AgentSpan.span_type == "root",
                    AgentSpan.started_at >= start,
                    AgentSpan.started_at <= end,
                )
                .order_by(AgentSpan.started_at.asc())
            )
        ).scalars().all()
    )
    series: dict[str, dict] = {}
    buckets = _hourly_buckets(start, end) if granularity == "hour" else []
    for b in buckets:
        series.setdefault(b["ts"], {"ts": b["ts"]})
    for s in spans:
        key = _floor_hour(s.started_at).isoformat() if granularity == "hour" else s.started_at.date().isoformat()
        pt = series.setdefault(key, {"ts": key})
        pt["requests"] = pt.get("requests", 0) + 1
        if s.status in ("error", "timeout", "canceled"):
            pt["errors"] = pt.get("errors", 0) + 1
        tu = s.token_usage or {}
        pt["tokens_total"] = pt.get("tokens_total", 0) + tu.get("total", 0)
        pt["cost"] = pt.get("cost", 0) + float(s.cost_total or 0)
        durs = series.setdefault(f"{key}#durs", {"durs": []})
        durs["durs"] = durs.get("durs", []) + [s.duration_ms]
    for k, v in list(series.items()):
        if "#durs" in k:
            series.pop(k, None)
        elif "durs" in v:
            durs = v.pop("durs")
            v["latency_p50_ms"] = _percentile(durs, 50)
            v["latency_p95_ms"] = _percentile(durs, 95)
    return ApiResponse(
        data={
            "series": sorted(series.values(), key=lambda x: x["ts"]),
            "sampled": True if metrics.spans_dropped_total else False,
            "estimated": False,
            "granularity": granularity,
        }
    )


@router.get("/breakdowns", response_model=ApiResponse)
async def breakdowns(
    group_by: str = Query("model", pattern="^(model|tool|user|prompt_version|status)$"),
    start_from: str | None = Query(None, alias="from"),
    start_to: str | None = Query(None, alias="to"),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """按模型、工具、用户、版本聚合（§12.1）。"""
    from sqlalchemy import case, func, select

    tenant_id = auth.tenant.tenant_id
    now = datetime.now(UTC)
    start = _parse_dt(start_from) or (now - timedelta(hours=24))
    end = _parse_dt(start_to) or now

    base = (AgentSpan.tenant_id == tenant_id, AgentSpan.started_at >= start, AgentSpan.started_at <= end)
    if group_by == "tool":
        col = AgentSpan.span_name
        base = base + (AgentSpan.span_type == "tool",)
    elif group_by == "model":
        col = AgentSpan.model
        base = base + (AgentSpan.span_type.in_(["llm", "embedding"]),)
    elif group_by == "status":
        col = AgentSpan.status
        base = base + (AgentSpan.span_type == "root",)
    else:
        col = AgentSpan.span_type
        base = base + (AgentSpan.span_type == "root",)

    result = await db.execute(
        select(col, func.count(AgentSpan.id)).where(*base).group_by(col)
    )
    items = [{"key": str(k), "count": c} for k, c in result.all()]
    return ApiResponse(data={"items": items, "group_by": group_by})


# ---------- Trace 列表 / 详情 ----------


@router.get("/traces", response_model=ApiResponse)
async def list_traces(
    status: str | None = Query(None),
    span_type: str | None = Query(None),
    start_from: str | None = Query(None),
    start_to: str | None = Query(None),
    keyword: str | None = Query(None),
    model: str | None = Query(None),
    has_error: bool | None = Query(None),
    has_feedback: bool | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    sort: str | None = Query(None),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """Trace 列表：分页 + 复杂筛选（兼容旧分页 + 新增 cursor 与筛选）。"""
    tenant_id = auth.tenant.tenant_id

    # 优先使用 agent_traces 汇总表（§9.1）；无数据时回退 agent_spans 根 span
    filters = {
        "status": status,
        "start_from": _parse_dt(start_from),
        "start_to": _parse_dt(start_to),
        "keyword": keyword,
        "model": model,
        "has_error": has_error,
        "has_feedback": has_feedback,
    }
    # 以 agent_spans 根 span 为准（保证实时可见），并用 agent_traces 汇总表富化字段
    rows = await repo.query_root_spans(
        db,
        tenant_id=tenant_id,
        status=status,
        start_from=_parse_dt(start_from),
        start_to=_parse_dt(start_to),
        keyword=keyword,
        page=page,
        page_size=page_size,
        sort=sort,
        sort_order=sort_order,
    )
    trace_ids = [s.trace_id for s in rows]
    summary_by_id = {}
    if trace_ids:
        summary_rows = list(
            (
                await db.execute(
                    select(repo.AgentTrace).where(repo.AgentTrace.trace_id.in_(trace_ids))
                )
            ).scalars().all()
        )
        summary_by_id = {r.trace_id: r for r in summary_rows}
    items = []
    for s in rows:
        count, tools = await _count_children(db, s.trace_id)
        summ = summary_by_id.get(s.trace_id)
        items.append(
            {
                **_span_to_trace_item(s, count, tools).model_dump(),
                "ttft_ms": getattr(s, "ttft_ms", None) or (summ.ttft_ms if summ else None),
                "cost": float(s.cost_total)
                if s.cost_total is not None
                else (float(summ.cost) if summ and summ.cost is not None else None),
                "llm_calls": len(await repo.get_trace_llm_spans(db, s.trace_id)),
                "has_feedback": summ.has_feedback if summ else False,
                "model": (summ.model_names or [None])[0] if summ else s.model,
                "input_summary": s.input_summary or (summ.input_preview if summ else None),
                "output_summary": s.output_summary or (summ.output_preview if summ else None),
                "error": s.error or (summ.error_message if summ else None),
            }
        )
    return ApiResponse(
        data={
            "items": items,
            "total": len(items),
            "page": page,
            "page_size": page_size,
            "next_cursor": None,
        }
    )


@router.get("/traces/{trace_id}", response_model=ApiResponse)
async def get_trace_detail(
    trace_id: str,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """Trace 详情：span 树 + 每 span 摘要。"""
    spans = await repo.query_trace_spans(db, trace_id)
    if not spans:
        return ApiResponse(code=404, message="trace not found", data=None)
    if spans[0].tenant_id != auth.tenant.tenant_id:
        return ApiResponse(code=404, message="trace not found", data=None)

    root_span = next((s for s in spans if s.span_type == "root"), spans[0])
    # 汇总只统计 llm/embedding 子 span（C-04），根 span 只展示自身
    total_tokens = {"prompt": 0, "completion": 0, "total": 0}
    for s in spans:
        if s.span_type in ("llm", "embedding"):
            tu = s.token_usage or {}
            total_tokens["prompt"] += tu.get("prompt", tu.get("input", 0))
            total_tokens["completion"] += tu.get("completion", tu.get("output", 0))
            total_tokens["total"] += tu.get("total", 0)

    tree = _build_tree(spans)
    detail = TraceDetail(
        trace_id=trace_id,
        root=tree,
        token_usage=total_tokens if any(total_tokens.values()) else (root_span.token_usage or None),
        duration_ms=root_span.duration_ms,
        status=root_span.status,
        started_at=_dt(root_span.started_at),
        ended_at=_dt(root_span.ended_at),
        error=root_span.error,
    )
    return ApiResponse(data=detail.model_dump())


@router.get("/traces/{trace_id}/timeline", response_model=ApiResponse)
async def get_trace_timeline(
    trace_id: str,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """Trace 时间线（前端 Gantt/火焰图数据）。"""
    spans = await repo.query_trace_spans(db, trace_id)
    if not spans or spans[0].tenant_id != auth.tenant.tenant_id:
        return ApiResponse(code=404, message="trace not found", data=None)
    start = min(s.started_at for s in spans)
    end = max(s.ended_at for s in spans)
    timeline = [
        {
            "span_id": s.span_id,
            "parent_span_id": s.parent_span_id,
            "span_name": s.span_name,
            "span_type": s.span_type,
            "status": s.status,
            "duration_ms": s.duration_ms,
            "start_ms": int((s.started_at - start).total_seconds() * 1000),
            "total_ms": int((end - start).total_seconds() * 1000) or 1,
            "model": s.model,
            "error": s.error,
        }
        for s in spans
    ]
    return ApiResponse(data={"items": timeline, "start": _dt(start), "end": _dt(end)})


@router.get("/traces/{trace_id}/events", response_model=ApiResponse)
async def get_trace_events(
    trace_id: str,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """Trace Explorer 数据：消息、Span 事件、泳道和右侧检视所需元数据。"""
    spans = await repo.query_trace_spans(db, trace_id)
    if not spans or spans[0].tenant_id != auth.tenant.tenant_id:
        return ApiResponse(code=404, message="trace not found", data=None)

    spans = sorted(spans, key=lambda item: (item.started_at, item.span_id))
    root = next((span for span in spans if span.span_type == "root"), spans[0])
    trace_summary = await repo.get_trace_summary(db, trace_id)
    capture_mode = (
        getattr(trace_summary, "content_capture_mode", None)
        or root.content_capture_mode
        or "metadata_only"
    )
    trace_start = root.started_at
    trace_end = root.ended_at or max(span.ended_at for span in spans)
    total_ms = max(
        1,
        int((trace_end - trace_start).total_seconds() * 1000),
        root.duration_ms,
    )

    # 新数据优先按 trace_id 精确关联；历史数据退回 session + 紧邻时间窗。
    message_stmt = select(Message).where(Message.trace_id == trace_id)
    messages = list((await db.execute(message_stmt.order_by(Message.created_at))).scalars())
    if not messages and root.session_id:
        margin = timedelta(minutes=2)
        messages = list(
            (
                await db.execute(
                    select(Message)
                    .where(
                        Message.session_id == root.session_id,
                        Message.created_at >= trace_start - margin,
                        Message.created_at <= trace_end + margin,
                    )
                    .order_by(Message.created_at)
                )
            ).scalars()
        )
        # 单次 Trace 只对应一轮对话，避免把同一会话相邻历史轮次一起带入。
        first_user = next((i for i, msg in enumerate(messages) if msg.role == "user"), None)
        if first_user is not None:
            messages = messages[first_user:]
            next_user = next(
                (i for i, msg in enumerate(messages[1:], start=1) if msg.role == "user"),
                None,
            )
            if next_user is not None:
                messages = messages[:next_user]

    sortable_events: list[tuple[datetime, int, dict]] = []
    lane_blocks: list[dict] = []

    root_attrs = root.attributes or {}
    context_bits = []
    if root_attrs.get("skill_loaded"):
        context_bits.append(f"技能 {len(root_attrs.get('skill_loaded') or [])} 项")
    if root_attrs.get("memory_injected"):
        context_bits.append("已注入长期记忆")
    if context_bits:
        sortable_events.append(
            (
                trace_start,
                0,
                {
                    "event_id": f"context:{root.span_id}",
                    "kind": "context",
                    "role": "context",
                    "name": "运行上下文",
                    "summary": " · ".join(context_bits),
                    "status": root.status,
                    "span_id": root.span_id,
                    "parent_span_id": None,
                    "started_at": _dt(trace_start),
                    "ended_at": _dt(trace_start),
                    "start_ms": 0,
                    "duration_ms": 0,
                    "token_usage": None,
                    "ttft_ms": None,
                    "generation_ms": None,
                    "throughput": None,
                    "model": None,
                    "model_provider": None,
                    "finish_reason": None,
                    "input_summary": None,
                    "output_summary": None,
                    "error": None,
                    "capture_mode": capture_mode,
                    "raw": {"context": context_bits},
                },
            )
        )

    for msg in messages:
        kind = msg.role if msg.role in {"user", "assistant", "tool", "system"} else "context"
        start_ms = max(0, int((msg.created_at - trace_start).total_seconds() * 1000))
        event = {
            "event_id": f"message:{msg.id}",
            "kind": kind,
            "role": msg.role,
            "name": "用户输入" if msg.role == "user" else ("最终回复" if msg.role == "assistant" else (msg.tool_name or msg.role)),
            "summary": msg.content,
            "status": "ok",
            "span_id": msg.span_id,
            "parent_span_id": None,
            "started_at": _dt(msg.created_at),
            "ended_at": _dt(msg.created_at),
            "start_ms": min(start_ms, total_ms),
            "duration_ms": 0,
            "token_usage": msg.token_usage,
            "ttft_ms": None,
            "generation_ms": None,
            "throughput": None,
            "model": None,
            "model_provider": None,
            "finish_reason": None,
            "input_summary": msg.content if msg.role == "user" else None,
            "output_summary": msg.content if msg.role == "assistant" else None,
            "error": None,
            "capture_mode": "message_history",
            "raw": {
                "message_id": msg.id,
                "role": msg.role,
                "tool_name": msg.tool_name,
                "token_usage": msg.token_usage,
                "request_id": msg.request_id,
                "trace_id": msg.trace_id,
                "created_at": _dt(msg.created_at),
            },
        }
        sortable_events.append((msg.created_at, 1, event))
        if msg.role == "user":
            lane_blocks.append(
                {
                    "block_id": f"input:{msg.id}",
                    "lane": "input",
                    "event_id": event["event_id"],
                    "span_id": None,
                    "status": "ok",
                    "start_ms": min(start_ms, total_ms),
                    "duration_ms": max(1, min(12, total_ms)),
                }
            )

    for span in spans:
        if span.span_type == "root":
            continue
        start_ms = max(0, int((span.started_at - trace_start).total_seconds() * 1000))
        output_tokens = _token_value(span.token_usage, "output", "completion")
        generation_ms = (
            max(0, span.duration_ms - span.ttft_ms)
            if span.ttft_ms is not None
            else None
        )
        throughput = (
            round(output_tokens / (generation_ms / 1000), 1)
            if output_tokens and generation_ms and generation_ms > 0
            else None
        )
        kind = _span_event_kind(span)
        visible_input = span.input_summary if capture_mode != "metadata_only" else None
        visible_output = span.output_summary if capture_mode != "metadata_only" else None
        event = {
            "event_id": f"span:{span.span_id}",
            "kind": kind,
            "role": "assistant" if span.span_type == "llm" else span.span_type,
            "name": span.span_name,
            "summary": visible_output or visible_input or (
                "调用完成" if span.status == "ok" else (span.error or span.status)
            ),
            "status": span.status,
            "span_id": span.span_id,
            "parent_span_id": span.parent_span_id,
            "started_at": _dt(span.started_at),
            "ended_at": _dt(span.ended_at),
            "start_ms": start_ms,
            "duration_ms": span.duration_ms,
            "token_usage": span.token_usage,
            "ttft_ms": span.ttft_ms,
            "generation_ms": generation_ms,
            "throughput": throughput,
            "model": span.model,
            "model_provider": span.model_provider,
            "finish_reason": span.finish_reason,
            "input_summary": visible_input,
            "output_summary": visible_output,
            "error": span.error,
            "capture_mode": capture_mode,
            "task_id": (span.attributes or {}).get("task_id"),
            "references": (
                (span.attributes or {}).get("references", [])
                if capture_mode != "metadata_only"
                else []
            ),
            "raw": _safe_span_raw(span, capture_mode),
        }
        sortable_events.append((span.started_at, 2, event))
        lane = _span_lane(span)
        if lane:
            lane_blocks.append(
                {
                    "block_id": f"span:{span.span_id}",
                    "lane": lane,
                    "event_id": event["event_id"],
                    "span_id": span.span_id,
                    "status": span.status,
                    "start_ms": start_ms,
                    "duration_ms": max(1, span.duration_ms),
                }
            )

    sortable_events.sort(key=lambda item: (item[0], item[1], item[2]["event_id"]))
    events: list[dict] = []
    turn_index = 0
    for step_index, (_, _, event) in enumerate(sortable_events, start=1):
        if event["kind"] == "user":
            turn_index += 1
        event["turn_index"] = max(1, turn_index)
        event["step_index"] = step_index
        events.append(event)

    llm_spans = [span for span in spans if span.span_type == "llm"]
    ttft_values = [span.ttft_ms for span in llm_spans if span.ttft_ms is not None]
    trace_ttft = min(ttft_values) if ttft_values else None
    trace_output_tokens = sum(
        _token_value(span.token_usage, "output", "completion") for span in llm_spans
    )
    trace_generation_ms = max(0, total_ms - trace_ttft) if trace_ttft is not None else None
    trace_throughput = (
        round(trace_output_tokens / (trace_generation_ms / 1000), 1)
        if trace_output_tokens and trace_generation_ms and trace_generation_ms > 0
        else None
    )
    token_usage = (
        getattr(trace_summary, "token_usage", None)
        or root.token_usage
        or None
    )
    return ApiResponse(
        data={
            "trace": {
                "trace_id": trace_id,
                "request_id": getattr(trace_summary, "request_id", None) or root.request_id,
                "session_id": str(root.session_id) if root.session_id else None,
                "thread_id": getattr(trace_summary, "thread_id", None) or root.thread_id,
                "status": root.status,
                "started_at": _dt(trace_start),
                "ended_at": _dt(trace_end),
                "duration_ms": total_ms,
                "span_count": len(spans),
                "tool_calls": len([span for span in spans if span.span_type == "tool"]),
                "token_usage": token_usage,
                "ttft_ms": trace_ttft,
                "generation_ms": trace_generation_ms,
                "throughput": trace_throughput,
                "content_capture_mode": capture_mode,
            },
            "events": events,
            "lanes": lane_blocks,
        }
    )


@router.get("/traces/{trace_id}/logs", response_model=ApiResponse)
async def get_trace_logs(
    trace_id: str,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """Trace 关联日志（§12.2）。"""
    logs, _ = await repo.query_observability_logs(
        db, tenant_id=auth.tenant.tenant_id, filters={"trace_id": trace_id}, limit=200
    )
    return ApiResponse(
        data={
            "items": [
                {
                    "id": l.id,
                    "timestamp": _dt(l.timestamp),
                    "severity_text": l.severity_text,
                    "body": l.body,
                    "logger_name": l.logger_name,
                    "module": l.module,
                    "span_id": l.span_id,
                    "trace_id": l.trace_id,
                    "instance_id": l.instance_id,
                    "exception_type": l.exception_type,
                    "exception_message": l.exception_message,
                }
                for l in logs
            ]
        }
    )


@router.get("/traces/{trace_id}/scores", response_model=ApiResponse)
async def get_trace_scores(
    trace_id: str,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """Trace 关联评分（§12.2）。"""
    scores = await repo.list_trace_scores(db, trace_id=trace_id)
    return ApiResponse(
        data={
            "items": [
                {
                    "id": s.id,
                    "score_name": s.score_name,
                    "value": s.value,
                    "source": s.source,
                    "evaluator_version": s.evaluator_version,
                    "reason": s.reason,
                    "creator_id": s.creator_id,
                    "created_at": _dt(s.created_at),
                }
                for s in scores
            ]
        }
    )


@router.post("/traces/{trace_id}/scores", response_model=ApiResponse)
async def add_trace_score(
    trace_id: str,
    body: ScoreCreateRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """新增人工评分（§12.2）。"""
    from ..observability import audit

    score = await repo.add_trace_score(
        db,
        trace_id=trace_id,
        tenant_id=auth.tenant.tenant_id,
        score_name=body.score_name,
        value=body.value,
        source=body.source,
        reason=body.reason,
        creator_id=auth.user_id,
    )
    await audit.audit(
        auth.tenant.tenant_id,
        "observability.score.add",
        user_id=auth.user_id,
        detail={"trace_id": trace_id, "score_name": body.score_name},
    )
    # 更新 Trace 汇总 has_feedback
    await db.execute(
        update(repo.AgentTrace)
        .where(repo.AgentTrace.trace_id == trace_id)
        .values(has_feedback=True)
    )
    await db.commit()
    return ApiResponse(data={"score_id": score.id})


@router.post("/traces/export", response_model=ApiResponse)
async def export_traces(
    body: TraceExportRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """导出 Trace（§12.2）。"""
    from ..observability import audit

    payload = []
    for trace_id in body.trace_ids:
        spans = await repo.query_trace_spans(db, trace_id)
        if spans and spans[0].tenant_id != auth.tenant.tenant_id:
            continue
        trace_payload = {
            "trace_id": trace_id,
            "spans": [
                {
                    "span_id": s.span_id,
                    "parent_span_id": s.parent_span_id,
                    "span_name": s.span_name,
                    "span_type": s.span_type,
                    "status": s.status,
                    "model": s.model,
                    "token_usage": s.token_usage,
                    "input_summary": s.input_summary,
                    "output_summary": s.output_summary,
                    "duration_ms": s.duration_ms,
                    "started_at": _dt(s.started_at),
                    "error": s.error,
                }
                for s in spans
            ],
        }
        if body.include_scores:
            trace_payload["scores"] = [
                {
                    "score_name": s.score_name,
                    "value": s.value,
                    "source": s.source,
                    "reason": s.reason,
                }
                for s in await repo.list_trace_scores(db, trace_id=trace_id)
            ]
        payload.append(trace_payload)
    await audit.audit(
        auth.tenant.tenant_id,
        "observability.trace.export",
        user_id=auth.user_id,
        detail={"trace_count": len(payload)},
    )
    return ApiResponse(data={"count": len(payload), "traces": payload})


# ---------- 调用分析（§12.3 / §15.4）----------


@router.get("/calls", response_model=ApiResponse)
async def list_calls(
    span_type: str | None = Query(None, pattern="^(llm|tool|retriever|embedding|memory|node)$"),
    model: str | None = Query(None),
    tool_name: str | None = Query(None),
    status: str | None = Query(None),
    start_from: str | None = Query(None),
    start_to: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """LLM、工具、检索、Embedding、Memory 调用列表（§12.3）。"""
    from sqlalchemy import func, select

    tenant_id = auth.tenant.tenant_id
    types = {"llm", "tool", "retriever", "embedding", "memory", "node"}
    stmt = select(AgentSpan).where(AgentSpan.tenant_id == tenant_id)
    count_stmt = select(func.count(AgentSpan.id)).where(AgentSpan.tenant_id == tenant_id)
    if span_type:
        stmt = stmt.where(AgentSpan.span_type == span_type)
        count_stmt = count_stmt.where(AgentSpan.span_type == span_type)
    else:
        stmt = stmt.where(AgentSpan.span_type.in_(types))
        count_stmt = count_stmt.where(AgentSpan.span_type.in_(types))
    if model:
        stmt = stmt.where(AgentSpan.model == model)
        count_stmt = count_stmt.where(AgentSpan.model == model)
    if tool_name:
        stmt = stmt.where(AgentSpan.span_name == f"tool.{tool_name}")
        count_stmt = count_stmt.where(AgentSpan.span_name == f"tool.{tool_name}")
    if status:
        stmt = stmt.where(AgentSpan.status == status)
        count_stmt = count_stmt.where(AgentSpan.status == status)
    if _parse_dt(start_from):
        stmt = stmt.where(AgentSpan.started_at >= _parse_dt(start_from))
        count_stmt = count_stmt.where(AgentSpan.started_at >= _parse_dt(start_from))
    if _parse_dt(start_to):
        stmt = stmt.where(AgentSpan.started_at <= _parse_dt(start_to))
        count_stmt = count_stmt.where(AgentSpan.started_at <= _parse_dt(start_to))
    total = (await db.execute(count_stmt)).scalar_one()
    rows = list(
        (
            await db.execute(
                stmt.order_by(AgentSpan.started_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()
    )
    return ApiResponse(
        data={
            "items": [
                {
                    "span_id": s.span_id,
                    "trace_id": s.trace_id,
                    "session_id": str(s.session_id) if s.session_id else None,
                    "span_name": s.span_name,
                    "span_type": s.span_type,
                    "status": s.status,
                    "model": s.model,
                    "model_provider": s.model_provider,
                    "model_name_resolved": s.model_name_resolved,
                    "token_usage": s.token_usage,
                    "ttft_ms": s.ttft_ms,
                    "input_summary": s.input_summary,
                    "output_summary": s.output_summary,
                    "duration_ms": s.duration_ms,
                    "started_at": _dt(s.started_at),
                    "error": s.error,
                    "finish_reason": s.finish_reason,
                    "retry_count": s.retry_count,
                    "cost_total": float(s.cost_total) if s.cost_total is not None else None,
                    "attributes": s.attributes,
                }
                for s in rows
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    )


@router.get("/calls/{span_id}", response_model=ApiResponse)
async def get_call_detail(
    span_id: str,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """单次调用详情（§12.3）。"""
    span = (
        await db.execute(
            select(AgentSpan).where(AgentSpan.span_id == span_id)
        )
    ).scalar_one_or_none()
    if span is None or span.tenant_id != auth.tenant.tenant_id:
        return ApiResponse(code=404, message="call not found", data=None)
    return ApiResponse(
        data={
            "span_id": span.span_id,
            "trace_id": span.trace_id,
            "parent_span_id": span.parent_span_id,
            "span_name": span.span_name,
            "span_type": span.span_type,
            "status": span.status,
            "model": span.model,
            "model_provider": span.model_provider,
            "model_name_requested": span.model_name_requested,
            "model_name_resolved": span.model_name_resolved,
            "model_parameters": span.model_parameters,
            "token_usage": span.token_usage,
            "ttft_ms": span.ttft_ms,
            "finish_reason": span.finish_reason,
            "retry_count": span.retry_count,
            "input_summary": span.input_summary,
            "output_summary": span.output_summary,
            "duration_ms": span.duration_ms,
            "started_at": _dt(span.started_at),
            "ended_at": _dt(span.ended_at),
            "attributes": span.attributes,
            "events": span.events,
            "error": span.error,
            "cost_input": float(span.cost_input) if span.cost_input is not None else None,
            "cost_output": float(span.cost_output) if span.cost_output is not None else None,
            "cost_total": float(span.cost_total) if span.cost_total is not None else None,
            "currency": span.currency,
        }
    )


# ---------- 日志（§12.3 / §15.5）----------


@router.get("/logs", response_model=ApiResponse)
async def list_logs(
    severity: str | None = Query(None, pattern="^(DEBUG|INFO|WARN|ERROR|FATAL)$"),
    trace_id: str | None = Query(None),
    span_id: str | None = Query(None),
    service: str | None = Query(None),
    module: str | None = Query(None),
    instance: str | None = Query(None),
    start_from: str | None = Query(None),
    start_to: str | None = Query(None),
    keyword: str | None = Query(None),
    cursor: int | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """结构化日志列表（cursor 分页）。"""
    filters = {
        "severity": severity,
        "trace_id": trace_id,
        "span_id": span_id,
        "service": service,
        "module": module,
        "instance": instance,
        "start_from": _parse_dt(start_from),
        "start_to": _parse_dt(start_to),
        "keyword": keyword,
    }
    rows, next_cursor = await repo.query_observability_logs(
        db,
        tenant_id=auth.tenant.tenant_id,
        filters={k: v for k, v in filters.items() if v is not None},
        cursor=cursor,
        limit=limit,
    )
    return ApiResponse(
        data={
            "items": [
                {
                    "id": l.id,
                    "timestamp": _dt(l.timestamp),
                    "severity_text": l.severity_text,
                    "body": l.body,
                    "trace_id": l.trace_id,
                    "span_id": l.span_id,
                    "service_name": l.service_name,
                    "module": l.module,
                    "instance_id": l.instance_id,
                    "logger_name": l.logger_name,
                    "exception_type": l.exception_type,
                    "exception_message": l.exception_message,
                    "stacktrace": l.stacktrace,
                }
                for l in rows
            ],
            "next_cursor": next_cursor,
        }
    )


# ---------- 成本（§12.3 / §15.6）----------


@router.get("/costs", response_model=ApiResponse)
async def get_costs(
    group_by: str | None = Query(None, pattern="^(model|user|prompt_version|environment|none)$"),
    start_from: str | None = Query(None, alias="from"),
    start_to: str | None = Query(None, alias="to"),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """成本趋势与拆分（§12.3）。"""
    from sqlalchemy import func, select

    tenant_id = auth.tenant.tenant_id
    now = datetime.now(UTC)
    start = _parse_dt(start_from) or (now - timedelta(days=7))
    end = _parse_dt(start_to) or now
    stmt = select(AgentSpan).where(
        AgentSpan.tenant_id == tenant_id,
        AgentSpan.span_type.in_(["llm", "embedding"]),
        AgentSpan.started_at >= start,
        AgentSpan.started_at <= end,
    )
    rows = list((await db.execute(stmt)).scalars().all())
    by_group: dict[str, dict] = {}
    total_cost = 0.0
    total_tokens = 0
    for s in rows:
        key = "all"
        if group_by == "model":
            key = s.model_name_resolved or s.model or "unknown"
        elif group_by == "user":
            key = s.user_id or "unknown"
        elif group_by == "environment":
            key = "unknown"
        g = by_group.setdefault(key, {"cost": 0.0, "tokens": 0, "input_tokens": 0, "output_tokens": 0})
        c = float(s.cost_total or 0)
        g["cost"] += c
        total_cost += c
        tu = s.token_usage or {}
        tok = tu.get("total", 0)
        g["tokens"] += tok
        total_tokens += tok
        g["input_tokens"] += tu.get("input", 0)
        g["output_tokens"] += tu.get("output", 0)
    # 未配置价格的调用
    unconfigured = sum(1 for s in rows if s.cost_total is None)
    return ApiResponse(
        data={
            "total_cost": round(total_cost, 6),
            "total_tokens": total_tokens,
            "unconfigured_price_calls": unconfigured,
            "groups": by_group,
            "start": _dt(start),
            "end": _dt(end),
        }
    )


@router.get("/model-prices", response_model=ApiResponse)
async def list_model_prices(
    model_name: str | None = Query(None),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """模型价格版本（§12.3）。"""
    rows = await repo.list_price_versions(db, model_name=model_name)
    return ApiResponse(
        data={
            "items": [
                {
                    "id": r.id,
                    "model_provider": r.model_provider,
                    "model_name": r.model_name,
                    "input_price_per_million": float(r.input_price_per_million or 0),
                    "cached_input_price_per_million": float(r.cached_input_price_per_million or 0),
                    "output_price_per_million": float(r.output_price_per_million or 0),
                    "currency": r.currency,
                    "effective_from": _dt(r.effective_from),
                    "version": r.version,
                }
                for r in rows
            ]
        }
    )


@router.post("/model-prices", response_model=ApiResponse)
async def create_model_price(
    body: PriceCreateRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """新增模型价格版本（§12.3）。"""
    from ..observability import audit

    row = await repo.create_price_version(
        db,
        values={
            "model_provider": body.model_provider,
            "model_name": body.model_name,
            "input_price_per_million": body.input_price_per_million,
            "cached_input_price_per_million": body.cached_input_price_per_million,
            "output_price_per_million": body.output_price_per_million,
            "currency": body.currency,
            "version": body.version,
        },
    )
    await audit.audit(
        auth.tenant.tenant_id,
        "observability.price.add",
        user_id=auth.user_id,
        detail={"model_name": body.model_name},
    )
    return ApiResponse(data={"price_version_id": row.id})


# ---------- 质量（§12.3 / §15.7）----------


@router.get("/quality", response_model=ApiResponse)
async def get_quality(
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """质量概览：评分趋势、按模型/版本分组、低分队列。"""
    from sqlalchemy import func, select

    from ..storage.models import TraceScore

    tenant_id = auth.tenant.tenant_id
    scores = list(
        (
            await db.execute(
                select(TraceScore)
                .where(TraceScore.tenant_id == tenant_id)
                .order_by(TraceScore.created_at.desc())
                .limit(500)
            )
        ).scalars().all()
    )
    by_source: dict[str, list] = {}
    for s in scores:
        by_source.setdefault(s.source, []).append(s.value)
    avg_by_source = {k: round(sum(v) / len(v), 3) for k, v in by_source.items() if v}
    return ApiResponse(
        data={
            "score_count": len(scores),
            "avg_by_source": avg_by_source,
            "recent_scores": [
                {
                    "id": s.id,
                    "trace_id": s.trace_id,
                    "score_name": s.score_name,
                    "value": s.value,
                    "source": s.source,
                    "reason": s.reason,
                    "created_at": _dt(s.created_at),
                }
                for s in scores[:50]
            ],
        }
    )


@router.get("/evaluations", response_model=ApiResponse)
async def list_evaluations(
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """评测任务列表（§12.3）。"""
    rows = await repo.list_evaluation_runs(db, tenant_id=auth.tenant.tenant_id)
    return ApiResponse(
        data={
            "items": [
                {
                    "id": r.id,
                    "name": r.name,
                    "status": r.status,
                    "dataset": r.dataset,
                    "variants": r.variants,
                    "results": r.results,
                    "created_at": _dt(r.created_at),
                }
                for r in rows
            ]
        }
    )


@router.post("/evaluations", response_model=ApiResponse)
async def create_evaluation(
    body: EvaluationRunCreateRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """创建评测任务（§12.3）。"""
    row = await repo.create_evaluation_run(
        db,
        values={
            "tenant_id": auth.tenant.tenant_id,
            "name": body.name,
            "dataset": body.dataset,
            "variants": body.variants,
            "created_by": auth.user_id,
        },
    )
    return ApiResponse(data={"evaluation_id": row.id})


# ---------- 告警与规则（§12.3 / §15.8）----------


@router.get("/alerts", response_model=ApiResponse)
async def list_alerts(
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """告警规则列表。"""
    rows = await repo.list_alert_rules(db, tenant_id=auth.tenant.tenant_id)
    return ApiResponse(
        data={
            "items": [
                {
                    "id": r.id,
                    "name": r.name,
                    "metric": r.metric,
                    "threshold": r.threshold,
                    "comparison": r.comparison,
                    "window_minutes": r.window_minutes,
                    "min_samples": r.min_samples,
                    "enabled": r.enabled,
                    "created_at": _dt(r.created_at),
                }
                for r in rows
            ]
        }
    )


@router.post("/alerts", response_model=ApiResponse)
async def create_alert(
    body: AlertRuleCreateRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """创建告警规则。"""
    from ..observability import audit

    row = await repo.create_alert_rule(
        db,
        values={
            "tenant_id": auth.tenant.tenant_id,
            "name": body.name,
            "metric": body.metric,
            "threshold": body.threshold,
            "comparison": body.comparison,
            "window_minutes": body.window_minutes,
            "min_samples": body.min_samples,
            "enabled": body.enabled,
        },
    )
    await audit.audit(
        auth.tenant.tenant_id,
        "observability.alert.create",
        user_id=auth.user_id,
        detail={"rule_name": body.name},
    )
    return ApiResponse(data={"rule_id": row.id})


@router.get("/incidents", response_model=ApiResponse)
async def list_incidents(
    status: str | None = Query(None, pattern="^(pending|firing|acknowledged|resolved)$"),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """告警事件列表。"""
    rows = await repo.list_alert_incidents(db, tenant_id=auth.tenant.tenant_id, status=status)
    return ApiResponse(
        data={
            "items": [
                {
                    "id": r.id,
                    "rule_id": r.rule_id,
                    "status": r.status,
                    "metric": r.metric,
                    "threshold": r.threshold,
                    "current_value": r.current_value,
                    "window_minutes": r.window_minutes,
                    "message": r.message,
                    "related_trace_ids": r.related_trace_ids,
                    "acknowledged_by": r.acknowledged_by,
                    "created_at": _dt(r.created_at),
                    "resolved_at": _dt(r.resolved_at),
                }
                for r in rows
            ]
        }
    )


@router.post("/incidents/{incident_id}", response_model=ApiResponse)
async def update_incident(
    incident_id: int,
    body: AlertIncidentUpdateRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """确认/恢复告警事件。"""
    from ..observability import audit

    values: dict = {}
    if body.status:
        values["status"] = body.status
    if body.acknowledged_by:
        values["acknowledged_by"] = body.acknowledged_by
    if body.status == "acknowledged":
        values["acknowledged_at"] = datetime.now(UTC)
    elif body.status == "resolved":
        values["resolved_at"] = datetime.now(UTC)
    row = await repo.update_alert_incident(db, incident_id, values=values)
    if row is None:
        return ApiResponse(code=404, message="incident not found", data=None)
    await audit.audit(
        auth.tenant.tenant_id,
        "observability.incident.update",
        user_id=auth.user_id,
        detail={"incident_id": incident_id, "status": body.status},
    )
    return ApiResponse(data={"status": row.status})


# ---------- 工具调用（兼容） ----------


@router.get("/tool-logs", response_model=ApiResponse)
async def list_tool_logs(
    tool_name: str | None = Query(None),
    status: str | None = Query(None),
    start_from: str | None = Query(None),
    start_to: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """工具调用日志（兼容 /tool-logs，前端可重定向到 /calls?type=tool）。"""
    _tool_name = f"tool.{tool_name}" if tool_name and not tool_name.startswith("tool.") else tool_name
    spans, total = await repo.query_tool_logs(
        db,
        tenant_id=auth.tenant.tenant_id,
        tool_name=_tool_name,
        status=status,
        start_from=_parse_dt(start_from),
        start_to=_parse_dt(start_to),
        page=page,
        page_size=page_size,
    )
    items = [
        ToolLogItem(
            span_id=s.span_id,
            trace_id=s.trace_id,
            session_id=str(s.session_id) if s.session_id else None,
            tool_name=s.span_name.removeprefix("tool."),
            status=s.status,
            input_summary=s.input_summary,
            output_summary=s.output_summary,
            duration_ms=s.duration_ms,
            started_at=_dt(s.started_at),
            error=s.error,
        )
        for s in spans
    ]
    return ApiResponse(
        data={"items": [i.model_dump() for i in items], "total": total, "page": page, "page_size": page_size}
    )


@router.get("/tools/stats", response_model=ApiResponse)
async def tool_stats(
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """工具调用统计：各工具次数 / 成功率 / 平均耗时。"""
    stats = await repo.aggregate_tool_stats(db, tenant_id=auth.tenant.tenant_id)
    return ApiResponse(data={"items": stats})


@router.get("/metrics/trend", response_model=ApiResponse)
async def metric_trend(
    granularity: str = Query("hour", pattern="^(hour|day)$"),
    days: int = Query(24, ge=1, le=90),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """指标趋势：granularity=hour|day，返回折线序列。"""
    from sqlalchemy import select

    tenant_id = auth.tenant.tenant_id
    now = datetime.now(UTC)

    if granularity == "day":
        start_date = now.date() - timedelta(days=days - 1)
        daily = await repo.list_metric_days(db, tenant_id=tenant_id, start=start_date, end=now.date())
        by_day = {d.day.date(): d for d in daily}
        points: list[dict] = []
        for offset in range(days):
            day = start_date + timedelta(days=offset)
            d = by_day.get(day)
            points.append(
                {
                    "ts": day.isoformat(),
                    "requests": d.requests if d else 0,
                    "errors": d.errors if d else 0,
                    "tokens_prompt": d.tokens_prompt if d else 0,
                    "tokens_completion": d.tokens_completion if d else 0,
                    "latency_p50_ms": d.latency_p50_ms if d else 0,
                    "latency_p95_ms": d.latency_p95_ms if d else 0,
                }
            )
        return ApiResponse(data={"items": points})

    hours = days
    # 窗口按整点对齐（hours 个整点桶，末桶为当前小时）
    trend_start = _floor_hour(now) - timedelta(hours=hours - 1)
    buckets = _hourly_buckets(trend_start, now)
    bucket_index = {b["ts"]: b for b in buckets}
    spans = list(
        (
            await db.execute(
                select(AgentSpan)
                .where(
                    AgentSpan.tenant_id == tenant_id,
                    AgentSpan.span_type == "root",
                    AgentSpan.started_at >= trend_start,
                )
            )
        ).scalars().all()
    )
    hour_durations: dict[str, list[int]] = {}
    for s in spans:
        key = _floor_hour(s.started_at).isoformat()
        bucket = bucket_index.get(key)
        if bucket is None:
            continue
        bucket["requests"] += 1
        if s.status in ("error", "timeout", "canceled"):
            bucket["errors"] += 1
        tu = s.token_usage or {}
        bucket["tokens_prompt"] += tu.get("prompt", tu.get("input", 0))
        bucket["tokens_completion"] += tu.get("completion", tu.get("output", 0))
        hour_durations.setdefault(key, []).append(s.duration_ms)
    for key, vals in hour_durations.items():
        bucket = bucket_index.get(key)
        if bucket:
            bucket["latency_p50_ms"] = _percentile(vals, 50)
            bucket["latency_p95_ms"] = _percentile(vals, 95)
    return ApiResponse(data={"items": buckets})


# ---------- 工具函数 ----------


def _floor_hour(ts: datetime) -> datetime:
    """归一到 UTC 并取整点：DB 会话时区（如 +08）与 UTC 生成的桶 key 才能对齐。"""
    return ts.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _percentile(values: list[int], p: float) -> int:
    if not values:
        return 0
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return int(sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f))
