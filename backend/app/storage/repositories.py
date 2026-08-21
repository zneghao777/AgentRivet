"""仓储层：会话、消息、Skill、审计、Agent span / 指标 的数据库访问。"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    AgentMetricsDaily,
    AgentMetricsHourly,
    AgentSpan,
    AgentTrace,
    AlertIncident,
    AlertRule,
    AuditLog,
    EvaluationRun,
    Message,
    ModelPriceVersion,
    ObservabilityLog,
    ObservabilitySavedView,
    Session,
    Skill,
    TraceScore,
)

# ---------- Session ----------


async def create_session(
    db: AsyncSession,
    *,
    thread_id: str,
    tenant_id: str,
    user_id: str,
    title: str | None = None,
) -> Session:
    session = Session(
        thread_id=thread_id,
        tenant_id=tenant_id,
        user_id=user_id,
        title=title,
        status="active",
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def get_session(db: AsyncSession, session_id: uuid.UUID) -> Session | None:
    return await db.get(Session, session_id)


async def get_session_by_thread(
    db: AsyncSession, thread_id: str
) -> Session | None:
    result = await db.execute(select(Session).where(Session.thread_id == thread_id))
    return result.scalar_one_or_none()


async def list_sessions(
    db: AsyncSession, *, tenant_id: str, page: int, page_size: int
) -> tuple[list[Session], int]:
    total = (
        await db.execute(
            select(func.count(Session.id)).where(
                Session.tenant_id == tenant_id,
                Session.status != "deleted",
            )
        )
    ).scalar_one()
    result = await db.execute(
        select(Session)
        .where(Session.tenant_id == tenant_id, Session.status != "deleted")
        .order_by(Session.updated_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all()), total


async def soft_delete_session(db: AsyncSession, session_id: uuid.UUID) -> bool:
    result = await db.execute(
        update(Session)
        .where(Session.id == session_id)
        .values(status="deleted", updated_at=datetime.now())
    )
    await db.commit()
    return result.rowcount > 0


async def set_session_status(
    db: AsyncSession, session_id: uuid.UUID, status: str
) -> None:
    await db.execute(
        update(Session)
        .where(Session.id == session_id)
        .values(status=status, updated_at=datetime.now())
    )
    await db.commit()


async def set_session_title(
    db: AsyncSession, session_id: uuid.UUID, title: str
) -> None:
    await db.execute(
        update(Session)
        .where(Session.id == session_id)
        .values(title=title, updated_at=datetime.now())
    )
    await db.commit()


# ---------- Message ----------


async def add_message(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    role: str,
    content: str,
    tool_name: str | None = None,
    token_usage: dict | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
    request_id: str | None = None,
    turn_index: int | None = None,
    step_index: int | None = None,
) -> Message:
    msg = Message(
        session_id=session_id,
        role=role,
        content=content,
        tool_name=tool_name,
        token_usage=token_usage,
        trace_id=trace_id,
        span_id=span_id,
        request_id=request_id,
        turn_index=turn_index,
        step_index=step_index,
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    return msg


async def link_messages_to_trace(
    db: AsyncSession,
    *,
    request_id: str,
    trace_id: str,
) -> None:
    """把运行开始前写入的消息补链到最终 Trace。"""
    await db.execute(
        update(Message)
        .where(Message.request_id == request_id, Message.trace_id.is_(None))
        .values(trace_id=trace_id)
    )
    await db.commit()


async def list_messages(
    db: AsyncSession, session_id: uuid.UUID, limit: int = 100
) -> list[Message]:
    result = await db.execute(
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.created_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


# ---------- Skill ----------


async def upsert_skill(db: AsyncSession, skill: Skill) -> Skill:
    """按 name 做 upsert（存在则更新，不存在则插入）。"""
    existing = await get_skill(db, skill.name)
    if existing is not None:
        existing.path = skill.path
        existing.description = skill.description
        existing.version = skill.version
        existing.author = skill.author
        existing.tags = skill.tags
        existing.updated_at = datetime.now()
        await db.commit()
        await db.refresh(existing)
        return existing
    db.add(skill)
    await db.commit()
    await db.refresh(skill)
    return skill


async def get_skill(db: AsyncSession, name: str) -> Skill | None:
    result = await db.execute(select(Skill).where(Skill.name == name))
    return result.scalar_one_or_none()


async def list_skills(db: AsyncSession) -> list[Skill]:
    result = await db.execute(select(Skill).order_by(Skill.name.asc()))
    return list(result.scalars().all())


async def set_skill_enabled(
    db: AsyncSession, name: str, enabled: bool
) -> Skill | None:
    skill = await get_skill(db, name)
    if skill:
        skill.enabled = enabled
        skill.updated_at = datetime.now()
        await db.commit()
        await db.refresh(skill)
    return skill


async def remove_skill(db: AsyncSession, name: str) -> None:
    await db.execute(delete(Skill).where(Skill.name == name))
    await db.commit()


# ---------- Audit ----------


async def add_audit(
    db: AsyncSession,
    *,
    tenant_id: str,
    action: str,
    user_id: str | None = None,
    session_id: uuid.UUID | None = None,
    detail: dict | None = None,
    ip: str | None = None,
    trace_id: str | None = None,
    request_id: str | None = None,
    resource_id: str | None = None,
) -> AuditLog:
    log = AuditLog(
        tenant_id=tenant_id,
        action=action,
        user_id=user_id,
        session_id=session_id,
        detail=detail,
        ip=ip,
        trace_id=trace_id,
        request_id=request_id,
        resource_id=resource_id,
    )
    db.add(log)
    await db.commit()
    return log


async def query_audit(
    db: AsyncSession,
    *,
    tenant_id: str | None = None,
    action: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[AuditLog], int]:
    stmt = select(AuditLog)
    count_stmt = select(func.count(AuditLog.id))
    if tenant_id:
        stmt = stmt.where(AuditLog.tenant_id == tenant_id)
        count_stmt = count_stmt.where(AuditLog.tenant_id == tenant_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
        count_stmt = count_stmt.where(AuditLog.action == action)
    total = (await db.execute(count_stmt)).scalar_one()
    result = await db.execute(
        stmt.order_by(AuditLog.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all()), total


# ---------- Agent Spans（OBSERVABILITY_SPEC §2.1） ----------


async def add_span(
    db: AsyncSession,
    *,
    trace_id: str,
    span_id: str,
    parent_span_id: str | None,
    tenant_id: str,
    session_id: uuid.UUID | None,
    user_id: str | None,
    span_name: str,
    span_type: str,
    status: str,
    model: str | None,
    token_usage: dict | None,
    input_summary: str | None,
    output_summary: str | None,
    duration_ms: int,
    started_at: datetime,
    ended_at: datetime,
    attributes: dict | None,
    error: str | None,
) -> AgentSpan:
    span = AgentSpan(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        session_id=session_id,
        tenant_id=tenant_id,
        user_id=user_id,
        span_name=span_name,
        span_type=span_type,
        status=status,
        model=model,
        token_usage=token_usage,
        input_summary=input_summary,
        output_summary=output_summary,
        duration_ms=duration_ms,
        started_at=started_at,
        ended_at=ended_at,
        attributes=attributes,
        error=error,
    )
    db.add(span)
    await db.commit()
    return span


async def bulk_upsert_spans(db: AsyncSession, items: list[dict]) -> int:
    """批量写入 span（§5.5）：同一批次一个事务，唯一键 (trace_id, span_id)。

    已存在的 span（同一 trace+span）做幂等覆盖，避免重复结束事件产生重复行。
    返回写入行数。
    """
    if not items:
        return 0
    written = 0
    for item in items:
        if item.get("record_type") == "span":
            existing = (
                await db.execute(
                    select(AgentSpan.id).where(
                        AgentSpan.trace_id == item["trace_id"],
                        AgentSpan.span_id == item["span_id"],
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                # 幂等：已存在则跳过（首个终态已落库）
                continue
            span = AgentSpan(
                trace_id=item["trace_id"],
                span_id=item["span_id"],
                parent_span_id=item.get("parent_span_id"),
                session_id=item.get("session_id"),
                tenant_id=item.get("tenant_id", "default"),
                user_id=item.get("user_id"),
                span_name=item.get("span_name", ""),
                span_type=item.get("span_type", "internal"),
                status=item.get("status", "ok"),
                model=item.get("model"),
                token_usage=item.get("token_usage"),
                input_summary=item.get("input_summary"),
                output_summary=item.get("output_summary"),
                duration_ms=item.get("duration_ms", 0),
                started_at=item.get("started_at") or datetime.now(UTC),
                ended_at=item.get("ended_at") or datetime.now(UTC),
                attributes=item.get("attributes"),
                error=item.get("error"),
                run_id=item.get("run_id"),
                parent_run_id=item.get("parent_run_id"),
                links=item.get("links"),
                status_code=item.get("status_code"),
                status_message=item.get("status_message"),
                model_provider=item.get("model_provider"),
                model_name_requested=item.get("model_name_requested"),
                model_name_resolved=item.get("model_name_resolved"),
                model_parameters=item.get("model_parameters"),
                finish_reason=item.get("finish_reason"),
                provider_request_id=item.get("provider_request_id"),
                prompt_name=item.get("prompt_name"),
                prompt_version=item.get("prompt_version"),
                prompt_hash=item.get("prompt_hash"),
                ttft_ms=item.get("ttft_ms"),
                retry_count=item.get("retry_count", 0),
                cost_input=item.get("cost_input"),
                cost_output=item.get("cost_output"),
                cost_total=item.get("cost_total"),
                currency=item.get("currency"),
                events=item.get("events"),
                content_capture_mode=item.get("content_capture_mode"),
                redaction_version=item.get("redaction_version"),
                request_id=item.get("request_id"),
                thread_id=item.get("thread_id"),
            )
            db.add(span)
            written += 1
    await db.commit()
    return written


async def query_traces(
    db: AsyncSession,
    *,
    tenant_id: str,
    status: str | None = None,
    span_type: str | None = None,
    start_from: datetime | None = None,
    start_to: datetime | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[AgentSpan], int]:
    """Trace 列表：按根 span（span_type=root）分页 + 筛选。"""
    stmt = select(AgentSpan).where(
        AgentSpan.tenant_id == tenant_id,
        AgentSpan.span_type == "root",
    )
    count_stmt = select(func.count(AgentSpan.id)).where(
        AgentSpan.tenant_id == tenant_id,
        AgentSpan.span_type == "root",
    )
    if status:
        stmt = stmt.where(AgentSpan.status == status)
        count_stmt = count_stmt.where(AgentSpan.status == status)
    if start_from:
        stmt = stmt.where(AgentSpan.started_at >= start_from)
        count_stmt = count_stmt.where(AgentSpan.started_at >= start_from)
    if start_to:
        stmt = stmt.where(AgentSpan.started_at <= start_to)
        count_stmt = count_stmt.where(AgentSpan.started_at <= start_to)
    if keyword:
        stmt = stmt.where(
            AgentSpan.span_name.ilike(f"%{keyword}%")
            | AgentSpan.input_summary.ilike(f"%{keyword}%")
            | AgentSpan.output_summary.ilike(f"%{keyword}%")
        )
        count_stmt = count_stmt.where(
            AgentSpan.span_name.ilike(f"%{keyword}%")
            | AgentSpan.input_summary.ilike(f"%{keyword}%")
            | AgentSpan.output_summary.ilike(f"%{keyword}%")
        )
    total = (await db.execute(count_stmt)).scalar_one()
    result = await db.execute(
        stmt.order_by(AgentSpan.started_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all()), total


async def query_trace_spans(db: AsyncSession, trace_id: str) -> list[AgentSpan]:
    """Trace 详情：返回该 trace 的全部 span（按时间排序）。"""
    result = await db.execute(
        select(AgentSpan)
        .where(AgentSpan.trace_id == trace_id)
        .order_by(AgentSpan.started_at.asc())
    )
    return list(result.scalars().all())


async def query_tool_logs(
    db: AsyncSession,
    *,
    tenant_id: str,
    tool_name: str | None = None,
    status: str | None = None,
    start_from: datetime | None = None,
    start_to: datetime | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[AgentSpan], int]:
    """工具调用日志：复用 tool span。"""
    stmt = select(AgentSpan).where(
        AgentSpan.tenant_id == tenant_id,
        AgentSpan.span_type == "tool",
    )
    count_stmt = select(func.count(AgentSpan.id)).where(
        AgentSpan.tenant_id == tenant_id,
        AgentSpan.span_type == "tool",
    )
    if tool_name:
        stmt = stmt.where(AgentSpan.span_name == tool_name)
        count_stmt = count_stmt.where(AgentSpan.span_name == tool_name)
    if status:
        stmt = stmt.where(AgentSpan.status == status)
        count_stmt = count_stmt.where(AgentSpan.status == status)
    if start_from:
        stmt = stmt.where(AgentSpan.started_at >= start_from)
        count_stmt = count_stmt.where(AgentSpan.started_at >= start_from)
    if start_to:
        stmt = stmt.where(AgentSpan.started_at <= start_to)
        count_stmt = count_stmt.where(AgentSpan.started_at <= start_to)
    total = (await db.execute(count_stmt)).scalar_one()
    result = await db.execute(
        stmt.order_by(AgentSpan.started_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all()), total


async def aggregate_tool_stats(
    db: AsyncSession, *, tenant_id: str
) -> list[dict]:
    """工具调用统计：各工具次数 / 成功数 / 平均耗时。"""
    result = await db.execute(
        select(
            AgentSpan.span_name,
            func.count(AgentSpan.id).label("calls"),
            func.sum(case((AgentSpan.status == "ok", 1), else_=0)).label(
                "success"
            ),
            func.avg(AgentSpan.duration_ms).label("avg_duration"),
        )
        .where(AgentSpan.tenant_id == tenant_id, AgentSpan.span_type == "tool")
        .group_by(AgentSpan.span_name)
        .order_by(func.count(AgentSpan.id).desc())
    )
    return [
        {
            "tool_name": name,
            "calls": calls,
            "success": success or 0,
            "avg_duration_ms": round(float(avg_duration or 0), 2),
        }
        for name, calls, success, avg_duration in result.all()
    ]


async def recent_error_traces(
    db: AsyncSession, *, tenant_id: str, limit: int = 5
) -> list[AgentSpan]:
    """最近异常 Trace（按根 span 过滤，status=error）。"""
    result = await db.execute(
        select(AgentSpan)
        .where(
            AgentSpan.tenant_id == tenant_id,
            AgentSpan.span_type == "root",
            AgentSpan.status == "error",
        )
        .order_by(AgentSpan.started_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


# ---------- Agent 指标聚合（OBSERVABILITY_SPEC §2.4） ----------


async def upsert_metric_day(
    db: AsyncSession, *, tenant_id: str, day: datetime, values: dict
) -> None:
    """按 (tenant_id, day) upsert 一条每日聚合记录。"""
    existing = await db.execute(
        select(AgentMetricsDaily).where(
            AgentMetricsDaily.tenant_id == tenant_id,
            AgentMetricsDaily.day == day,
        )
    )
    row = existing.scalar_one_or_none()
    if row is None:
        row = AgentMetricsDaily(tenant_id=tenant_id, day=day)
        db.add(row)
    for key, value in values.items():
        setattr(row, key, value)
    await db.commit()


async def list_metric_days(
    db: AsyncSession, *, tenant_id: str, start: date, end: date
) -> list[AgentMetricsDaily]:
    result = await db.execute(
        select(AgentMetricsDaily)
        .where(
            AgentMetricsDaily.tenant_id == tenant_id,
            AgentMetricsDaily.day >= datetime.combine(start, time.min, tzinfo=UTC),
            AgentMetricsDaily.day < datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC),
        )
        .order_by(AgentMetricsDaily.day.asc())
    )
    return list(result.scalars().all())


async def delete_expired_spans(db: AsyncSession, *, before: datetime) -> int:
    """清理过期 span（TRACE_RETENTION_DAYS）。"""
    result = await db.execute(
        delete(AgentSpan).where(AgentSpan.started_at < before)
    )
    await db.commit()
    return result.rowcount or 0


# ---------- Trace 汇总（OBSERVABILITY_SPEC §9.1）----------


async def upsert_trace_summary(db: AsyncSession, *, trace_id: str, values: dict) -> None:
    """按 trace_id upsert Trace 汇总行。"""
    existing = (
        await db.execute(select(AgentTrace).where(AgentTrace.trace_id == trace_id))
    ).scalar_one_or_none()
    if existing is None:
        row = AgentTrace(trace_id=trace_id)
        db.add(row)
    else:
        row = existing
    for key, value in values.items():
        setattr(row, key, value)
    await db.commit()


async def query_traces_v2(
    db: AsyncSession,
    *,
    tenant_id: str,
    filters: dict | None = None,
    cursor: str | None = None,
    limit: int = 50,
    sort: str | None = None,
    sort_order: str = "desc",
) -> tuple[list[AgentTrace], str | None]:
    """Trace 列表（agent_traces 表）cursor 分页 + 复杂筛选。"""
    f = filters or {}
    stmt = select(AgentTrace).where(AgentTrace.tenant_id == tenant_id)
    if f.get("status"):
        stmt = stmt.where(AgentTrace.status == f["status"])
    if f.get("session_id"):
        stmt = stmt.where(AgentTrace.session_id == f["session_id"])
    if f.get("user_id"):
        stmt = stmt.where(AgentTrace.user_id == f["user_id"])
    if f.get("agent_name"):
        stmt = stmt.where(AgentTrace.name == f["agent_name"])
    if f.get("model"):
        stmt = stmt.where(AgentTrace.model_names.contains([f["model"]]))
    if f.get("trace_id"):
        stmt = stmt.where(AgentTrace.trace_id == f["trace_id"])
    if f.get("request_id"):
        stmt = stmt.where(AgentTrace.request_id == f["request_id"])
    if f.get("start_from"):
        stmt = stmt.where(AgentTrace.started_at >= f["start_from"])
    if f.get("start_to"):
        stmt = stmt.where(AgentTrace.started_at <= f["start_to"])
    if f.get("has_error") is True:
        stmt = stmt.where(AgentTrace.status.in_(["error", "timeout", "canceled"]))
    if f.get("has_feedback") is True:
        stmt = stmt.where(AgentTrace.has_feedback.is_(True))
    if f.get("keyword"):
        kw = f"%{f['keyword']}%"
        stmt = stmt.where(
            AgentTrace.input_preview.ilike(kw)
            | AgentTrace.output_preview.ilike(kw)
            | AgentTrace.trace_id.ilike(kw)
            | AgentTrace.error_message.ilike(kw)
        )
    # 排序
    sort_col = {
        "started_at": AgentTrace.started_at,
        "duration_ms": AgentTrace.duration_ms,
        "cost": AgentTrace.cost,
        "token_total": AgentTrace.token_usage,
        "status": AgentTrace.status,
    }.get(sort, AgentTrace.started_at)
    order = sort_col.desc() if sort_order == "desc" else sort_col.asc()
    stmt = stmt.order_by(order, AgentTrace.trace_id.desc()).limit(limit + 1)
    rows = list((await db.execute(stmt)).scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = rows[-1].trace_id if has_more and rows else None
    return rows, next_cursor


async def get_trace_summary(db: AsyncSession, trace_id: str) -> AgentTrace | None:
    return await db.get(AgentTrace, trace_id)


async def get_trace_llm_spans(db: AsyncSession, trace_id: str) -> list[AgentSpan]:
    result = await db.execute(
        select(AgentSpan).where(
            AgentSpan.trace_id == trace_id, AgentSpan.span_type.in_(["llm", "embedding"])
        ).order_by(AgentSpan.started_at.asc())
    )
    return list(result.scalars().all())


async def query_root_spans(
    db: AsyncSession,
    *,
    tenant_id: str,
    status: str | None = None,
    start_from: datetime | None = None,
    start_to: datetime | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 50,
    sort: str | None = None,
    sort_order: str = "desc",
) -> list[AgentSpan]:
    """回退查询：从 agent_spans 中查根 span 列表（汇总表为空时）。"""
    stmt = select(AgentSpan).where(
        AgentSpan.tenant_id == tenant_id, AgentSpan.span_type == "root"
    )
    if status:
        stmt = stmt.where(AgentSpan.status == status)
    if start_from:
        stmt = stmt.where(AgentSpan.started_at >= start_from)
    if start_to:
        stmt = stmt.where(AgentSpan.started_at <= start_to)
    if keyword:
        kw = f"%{keyword}%"
        stmt = stmt.where(
            AgentSpan.input_summary.ilike(kw)
            | AgentSpan.output_summary.ilike(kw)
            | AgentSpan.trace_id.ilike(kw)
            | AgentSpan.error.ilike(kw)
        )
    order = {
        "started_at": AgentSpan.started_at,
        "duration_ms": AgentSpan.duration_ms,
        "cost": AgentSpan.cost_total,
        "status": AgentSpan.status,
    }.get(sort, AgentSpan.started_at)
    stmt = stmt.order_by(order.desc() if sort_order == "desc" else order.asc())
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    return list((await db.execute(stmt)).scalars().all())


# ---------- 结构化日志（OBSERVABILITY_SPEC §8.1）----------


async def add_observability_log(
    db: AsyncSession, *, timestamp: datetime | None = None, **kwargs
) -> ObservabilityLog:
    log = ObservabilityLog(timestamp=timestamp or datetime.now(UTC), **kwargs)
    db.add(log)
    await db.commit()
    return log


async def bulk_add_observability_logs(db: AsyncSession, items: list[dict]) -> int:
    """批量写 observability_logs（§8.1，供采集队列调用）。"""
    written = 0
    for item in items:
        db.add(
            ObservabilityLog(
                timestamp=item.get("timestamp") or datetime.now(UTC),
                observed_at=item.get("observed_at") or datetime.now(UTC),
                severity_text=item.get("severity_text", "INFO"),
                severity_number=item.get("severity_number", 9),
                body=item.get("body"),
                trace_id=item.get("trace_id"),
                span_id=item.get("span_id"),
                request_id=item.get("request_id"),
                session_id=item.get("session_id"),
                tenant_id=item.get("tenant_id") or "default",
                user_id=item.get("user_id"),
                logger_name=item.get("logger_name"),
                module=item.get("module"),
                function=item.get("function"),
                service_name=item.get("service_name"),
                instance_id=item.get("instance_id"),
                environment=item.get("environment"),
                exception_type=item.get("exception_type"),
                exception_message=item.get("exception_message"),
                stacktrace=item.get("stacktrace"),
                attributes=item.get("attributes"),
            )
        )
        written += 1
    await db.commit()
    return written


async def query_observability_logs(
    db: AsyncSession,
    *,
    tenant_id: str,
    filters: dict | None = None,
    cursor: int | None = None,
    limit: int = 50,
) -> tuple[list[ObservabilityLog], int | None]:
    f = filters or {}
    stmt = select(ObservabilityLog).where(ObservabilityLog.tenant_id == tenant_id)
    if f.get("severity"):
        stmt = stmt.where(ObservabilityLog.severity_text == f["severity"])
    if f.get("severity_min"):
        stmt = stmt.where(ObservabilityLog.severity_number >= f["severity_min"])
    if f.get("trace_id"):
        stmt = stmt.where(ObservabilityLog.trace_id == f["trace_id"])
    if f.get("span_id"):
        stmt = stmt.where(ObservabilityLog.span_id == f["span_id"])
    if f.get("service"):
        stmt = stmt.where(ObservabilityLog.service_name == f["service"])
    if f.get("module"):
        stmt = stmt.where(ObservabilityLog.module.ilike(f"%{f['module']}%"))
    if f.get("instance"):
        stmt = stmt.where(ObservabilityLog.instance_id == f["instance"])
    if f.get("start_from"):
        stmt = stmt.where(ObservabilityLog.timestamp >= f["start_from"])
    if f.get("start_to"):
        stmt = stmt.where(ObservabilityLog.timestamp <= f["start_to"])
    if f.get("keyword"):
        kw = f"%{f['keyword']}%"
        stmt = stmt.where(
            ObservabilityLog.body.ilike(kw)
            | ObservabilityLog.exception_message.ilike(kw)
            | ObservabilityLog.logger_name.ilike(kw)
        )
    if cursor:
        stmt = stmt.where(ObservabilityLog.id < cursor)
    rows = list(
        (await db.execute(stmt.order_by(ObservabilityLog.id.desc()).limit(limit + 1))).scalars().all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = rows[-1].id if has_more and rows else None
    return rows, next_cursor


# ---------- 模型价格（OBSERVABILITY_SPEC §6.4 / §12.3）----------


async def list_price_versions(
    db: AsyncSession, *, model_name: str | None = None
) -> list[ModelPriceVersion]:
    stmt = select(ModelPriceVersion).order_by(ModelPriceVersion.effective_from.desc())
    if model_name:
        stmt = stmt.where(ModelPriceVersion.model_name == model_name)
    return list((await db.execute(stmt)).scalars().all())


async def get_price_at(
    db: AsyncSession, model_name: str, *, at: datetime | None = None
) -> ModelPriceVersion | None:
    """获取某模型在指定时间生效的价格版本。"""
    at = at or datetime.now(UTC)
    result = await db.execute(
        select(ModelPriceVersion)
        .where(ModelPriceVersion.model_name == model_name, ModelPriceVersion.effective_from <= at)
        .order_by(ModelPriceVersion.effective_from.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def create_price_version(db: AsyncSession, *, values: dict) -> ModelPriceVersion:
    row = ModelPriceVersion(**values)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


# ---------- 评分（OBSERVABILITY_SPEC §11.1）----------


async def add_trace_score(
    db: AsyncSession,
    *,
    trace_id: str,
    tenant_id: str,
    score_name: str,
    value: float,
    source: str,
    evaluator_version: str | None = None,
    reason: str | None = None,
    creator_id: str | None = None,
) -> TraceScore:
    score = TraceScore(
        trace_id=trace_id,
        tenant_id=tenant_id,
        score_name=score_name,
        value=value,
        source=source,
        evaluator_version=evaluator_version,
        reason=reason,
        creator_id=creator_id,
    )
    db.add(score)
    await db.commit()
    await db.refresh(score)
    return score


async def list_trace_scores(db: AsyncSession, *, trace_id: str) -> list[TraceScore]:
    result = await db.execute(
        select(TraceScore).where(TraceScore.trace_id == trace_id).order_by(TraceScore.created_at.desc())
    )
    return list(result.scalars().all())


async def delete_trace_score(db: AsyncSession, score_id: int) -> bool:
    result = await db.execute(delete(TraceScore).where(TraceScore.id == score_id))
    await db.commit()
    return result.rowcount > 0


# ---------- 评测（OBSERVABILITY_SPEC §11.2）----------


async def create_evaluation_run(db: AsyncSession, *, values: dict) -> EvaluationRun:
    row = EvaluationRun(**values)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def list_evaluation_runs(
    db: AsyncSession, *, tenant_id: str, limit: int = 50
) -> list[EvaluationRun]:
    result = await db.execute(
        select(EvaluationRun)
        .where(EvaluationRun.tenant_id == tenant_id)
        .order_by(EvaluationRun.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


# ---------- 告警（OBSERVABILITY_SPEC §10.2 / §12.3）----------


async def list_alert_rules(db: AsyncSession, *, tenant_id: str) -> list[AlertRule]:
    result = await db.execute(
        select(AlertRule).where(AlertRule.tenant_id == tenant_id).order_by(AlertRule.id.asc())
    )
    return list(result.scalars().all())


async def get_alert_rule_by_name(
    db: AsyncSession, tenant_id: str, name: str
) -> AlertRule | None:
    result = await db.execute(
        select(AlertRule).where(
            AlertRule.tenant_id == tenant_id, AlertRule.name == name
        )
    )
    return result.scalar_one_or_none()


async def create_alert_rule(db: AsyncSession, *, values: dict) -> AlertRule:
    row = AlertRule(**values)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def update_alert_rule(db: AsyncSession, rule_id: int, *, values: dict) -> AlertRule | None:
    rule = await db.get(AlertRule, rule_id)
    if rule is None:
        return None
    for k, v in values.items():
        setattr(rule, k, v)
    await db.commit()
    await db.refresh(rule)
    return rule


async def delete_alert_rule(db: AsyncSession, rule_id: int) -> bool:
    result = await db.execute(delete(AlertRule).where(AlertRule.id == rule_id))
    await db.commit()
    return result.rowcount > 0


async def list_alert_incidents(
    db: AsyncSession, *, tenant_id: str, status: str | None = None, limit: int = 100
) -> list[AlertIncident]:
    stmt = select(AlertIncident).where(AlertIncident.tenant_id == tenant_id)
    if status:
        stmt = stmt.where(AlertIncident.status == status)
    stmt = stmt.order_by(AlertIncident.created_at.desc()).limit(limit)
    return list((await db.execute(stmt)).scalars().all())


async def create_alert_incident(db: AsyncSession, *, values: dict) -> AlertIncident:
    row = AlertIncident(**values)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def update_alert_incident(db: AsyncSession, incident_id: int, *, values: dict) -> AlertIncident | None:
    inc = await db.get(AlertIncident, incident_id)
    if inc is None:
        return None
    for k, v in values.items():
        setattr(inc, k, v)
    await db.commit()
    await db.refresh(inc)
    return inc


# ---------- 小时/日聚合 ----------


async def upsert_metric_hourly(
    db: AsyncSession, *, tenant_id: str, bucket: datetime, values: dict
) -> None:
    existing = (
        await db.execute(
            select(AgentMetricsHourly).where(
                AgentMetricsHourly.tenant_id == tenant_id,
                AgentMetricsHourly.bucket == bucket,
            )
        )
    ).scalar_one_or_none()
    row = existing or AgentMetricsHourly(tenant_id=tenant_id, bucket=bucket)
    if existing is None:
        db.add(row)
    for key, value in values.items():
        setattr(row, key, value)
    await db.commit()


async def list_metric_hourly(
    db: AsyncSession, *, tenant_id: str, start: datetime, end: datetime
) -> list[AgentMetricsHourly]:
    result = await db.execute(
        select(AgentMetricsHourly)
        .where(
            AgentMetricsHourly.tenant_id == tenant_id,
            AgentMetricsHourly.bucket >= start,
            AgentMetricsHourly.bucket < end,
        )
        .order_by(AgentMetricsHourly.bucket.asc())
    )
    return list(result.scalars().all())


# ---------- 保留策略清理（OBSERVABILITY_SPEC §9.4）----------


async def delete_expired_logs(db: AsyncSession, *, before: datetime) -> int:
    result = await db.execute(delete(ObservabilityLog).where(ObservabilityLog.timestamp < before))
    await db.commit()
    return result.rowcount or 0


async def delete_expired_metrics(db: AsyncSession, *, before: datetime) -> int:
    """清理过期小时聚合。"""
    result = await db.execute(delete(AgentMetricsHourly).where(AgentMetricsHourly.bucket < before))
    await db.commit()
    return result.rowcount or 0


async def delete_expired_traces(db: AsyncSession, *, before: datetime) -> int:
    """清理过期 Trace，同时处理关联 Span/Log/Score，避免孤儿数据（§9.4）。

    聚合表保留匿名统计，不删除。
    """
    # 找出过期 Trace 的 trace_id（分批处理）
    result = await db.execute(
        select(AgentTrace.trace_id).where(AgentTrace.started_at < before).limit(500)
    )
    expired_ids = list(result.scalars().all())
    removed = 0
    for trace_id in expired_ids:
        await db.execute(delete(ObservabilityLog).where(ObservabilityLog.trace_id == trace_id))
        await db.execute(delete(TraceScore).where(TraceScore.trace_id == trace_id))
        await db.execute(delete(AgentSpan).where(AgentSpan.trace_id == trace_id))
        await db.execute(delete(AgentTrace).where(AgentTrace.trace_id == trace_id))
        removed += 1
    await db.commit()
    return removed
