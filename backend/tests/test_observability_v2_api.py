"""可观测性 v2 API 测试（OBSERVABILITY_SPEC §12）。

覆盖：/health /metrics /breakdowns /calls /logs /costs /model-prices
/quality /alerts /incidents /traces/:id/scores /traces/export /traces/:id/logs
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.storage import repositories as repo
from app.storage.db import AsyncSessionLocal
from app.storage.models import AgentSpan, ObservabilityLog, TraceScore


async def _write_span(**kwargs):
    async with AsyncSessionLocal() as db:
        await repo.add_span(
            db,
            trace_id=kwargs.get("trace_id", f"trace-{uuid.uuid4().hex[:6]}"),
            span_id=kwargs.get("span_id", uuid.uuid4().hex[:16]),
            parent_span_id=kwargs.get("parent_span_id"),
            session_id=kwargs.get("session_id"),
            tenant_id=kwargs.get("tenant_id", "tenant-a"),
            user_id=kwargs.get("user_id", "user-1"),
            span_name=kwargs.get("span_name", "agent.run"),
            span_type=kwargs.get("span_type", "root"),
            status=kwargs.get("status", "ok"),
            model=kwargs.get("model"),
            token_usage=kwargs.get("token_usage"),
            input_summary=kwargs.get("input_summary"),
            output_summary=kwargs.get("output_summary"),
            attributes=kwargs.get("attributes"),
            error=kwargs.get("error"),
            duration_ms=kwargs.get("duration_ms", 10),
            started_at=kwargs.get("started_at", datetime.now(UTC)),
            ended_at=kwargs.get("ended_at", datetime.now(UTC)),
        )


async def _write_log(**kwargs):
    async with AsyncSessionLocal() as db:
        await repo.add_observability_log(
            db,
            timestamp=kwargs.get("timestamp", datetime.now(UTC)),
            severity_text=kwargs.get("severity_text", "ERROR"),
            severity_number=kwargs.get("severity_number", 17),
            body=kwargs.get("body", "boom"),
            trace_id=kwargs.get("trace_id"),
            span_id=kwargs.get("span_id"),
            tenant_id=kwargs.get("tenant_id", "tenant-a"),
            user_id=kwargs.get("user_id", "user-1"),
            logger_name=kwargs.get("logger_name", "test.logger"),
            module=kwargs.get("module", "test_module"),
            exception_type=kwargs.get("exception_type"),
            exception_message=kwargs.get("exception_message"),
        )


def _headers():
    return {"X-API-Key": "test-key-1", "X-Tenant-Id": "tenant-a"}


@pytest.mark.asyncio
async def test_trace_events_returns_messages_lanes_and_inspector_data(app_client):
    trace_id = f"trace-events-{uuid.uuid4().hex[:6]}"
    request_id = f"req-{uuid.uuid4().hex[:8]}"
    started = datetime.now(UTC)
    async with AsyncSessionLocal() as db:
        session = await repo.create_session(
            db,
            thread_id=f"thread-{uuid.uuid4().hex}",
            tenant_id="tenant-a",
            user_id="user-1",
            title="事件回放测试",
        )
        root = AgentSpan(
            trace_id=trace_id,
            span_id="event-root",
            parent_span_id=None,
            session_id=session.id,
            tenant_id="tenant-a",
            user_id="user-1",
            span_name="agent.run",
            span_type="root",
            status="ok",
            token_usage={"input": 10, "output": 5, "total": 15},
            duration_ms=1000,
            started_at=started,
            ended_at=started + timedelta(seconds=1),
            attributes={"skill_loaded": ["search"], "memory_injected": True},
            content_capture_mode="redacted",
            request_id=request_id,
        )
        llm = AgentSpan(
            trace_id=trace_id,
            span_id="event-llm",
            parent_span_id="event-root",
            session_id=session.id,
            tenant_id="tenant-a",
            user_id="user-1",
            span_name="llm.chat",
            span_type="llm",
            status="ok",
            model="test-model",
            token_usage={"input": 10, "output": 5, "total": 15},
            input_summary="测试输入",
            output_summary="测试输出",
            duration_ms=700,
            started_at=started + timedelta(milliseconds=100),
            ended_at=started + timedelta(milliseconds=800),
            ttft_ms=120,
            events=[{"name": "llm.chat", "phase": "start"}],
            content_capture_mode="redacted",
            request_id=request_id,
        )
        tool = AgentSpan(
            trace_id=trace_id,
            span_id="event-tool",
            parent_span_id="event-root",
            session_id=session.id,
            tenant_id="tenant-a",
            user_id="user-1",
            span_name="tool.search",
            span_type="tool",
            status="ok",
            input_summary="search(query)",
            output_summary="找到 2 条",
            duration_ms=200,
            started_at=started + timedelta(milliseconds=400),
            ended_at=started + timedelta(milliseconds=600),
            content_capture_mode="redacted",
            request_id=request_id,
        )
        db.add_all([root, llm, tool])
        await db.commit()
        await repo.add_message(
            db,
            session_id=session.id,
            role="user",
            content="请帮我搜索",
            trace_id=trace_id,
            request_id=request_id,
        )
        await repo.add_message(
            db,
            session_id=session.id,
            role="assistant",
            content="搜索完成",
            trace_id=trace_id,
            request_id=request_id,
        )

    resp = await app_client.get(
        f"/api/v1/observability/traces/{trace_id}/events", headers=_headers()
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["trace"]["ttft_ms"] == 120
    assert data["trace"]["tool_calls"] == 1
    assert {lane["lane"] for lane in data["lanes"]} == {"input", "model", "tools"}
    assert {event["kind"] for event in data["events"]} >= {
        "user",
        "assistant",
        "tool",
        "context",
    }
    llm_event = next(event for event in data["events"] if event["span_id"] == "event-llm")
    assert llm_event["generation_ms"] == 580
    assert llm_event["throughput"] == 8.6
    assert llm_event["raw"]["events"][0]["phase"] == "start"


@pytest.mark.asyncio
async def test_health_endpoint(app_client):
    resp = await app_client.get("/api/v1/observability/health", headers=_headers())
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert "spans_dropped_total" in body
    assert "queue_depth" in body
    assert "active_sessions" in body


@pytest.mark.asyncio
async def test_metrics_endpoint(app_client):
    await _write_span(status="ok", duration_ms=100)
    await _write_span(status="error", duration_ms=200)
    resp = await app_client.get(
        "/api/v1/observability/metrics",
        headers=_headers(),
        params={"metric": "requests", "granularity": "hour"},
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert "series" in body
    assert body["granularity"] == "hour"


@pytest.mark.asyncio
async def test_calls_endpoint_filters_by_type(app_client):
    await _write_span(span_name="llm.chat", span_type="llm", model="test-model", token_usage={"input": 10, "output": 5, "total": 15})
    await _write_span(span_name="tool.echo", span_type="tool")
    resp = await app_client.get("/api/v1/observability/calls", headers=_headers(), params={"span_type": "llm", "model": "test-model"})
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["total"] >= 1
    assert all(i["span_type"] == "llm" for i in body["items"])
    assert all(i["model"] == "test-model" for i in body["items"])


@pytest.mark.asyncio
async def test_logs_endpoint(app_client):
    await _write_log(trace_id="trace-log-1", severity_text="ERROR", body="boom detail", module="m1")
    await _write_log(severity_text="INFO", body="info log", module="m2")
    resp = await app_client.get("/api/v1/observability/logs", headers=_headers())
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert len(body["items"]) >= 1
    # 按 trace 过滤
    resp2 = await app_client.get("/api/v1/observability/logs", headers=_headers(), params={"trace_id": "trace-log-1"})
    body2 = resp2.json()["data"]
    assert all(i["trace_id"] == "trace-log-1" for i in body2["items"])
    assert any(i["body"] == "boom detail" for i in body2["items"])


@pytest.mark.asyncio
async def test_trace_logs_and_scores_endpoints(app_client):
    trace_id = f"trace-scores-{uuid.uuid4().hex[:6]}"
    await _write_span(trace_id=trace_id, span_id="sroot", span_type="root", span_name="agent.run")
    await _write_log(trace_id=trace_id, severity_text="WARN", body="关联日志")
    resp = await app_client.get(f"/api/v1/observability/traces/{trace_id}/logs", headers=_headers())
    assert resp.status_code == 200
    logs = resp.json()["data"]["items"]
    assert any(l["trace_id"] == trace_id for l in logs)

    # 评分
    resp = await app_client.post(
        f"/api/v1/observability/traces/{trace_id}/scores",
        headers=_headers(),
        json={"score_name": "helpfulness", "value": 0.9, "source": "user", "reason": "good"},
    )
    assert resp.status_code == 200
    resp = await app_client.get(f"/api/v1/observability/traces/{trace_id}/scores", headers=_headers())
    scores = resp.json()["data"]["items"]
    assert any(s["score_name"] == "helpfulness" and s["value"] == 0.9 for s in scores)


@pytest.mark.asyncio
async def test_costs_endpoint(app_client):
    await _write_span(span_name="llm.chat", span_type="llm", model="claude-sonnet-4", token_usage={"input": 1000, "output": 500, "total": 1500})
    resp = await app_client.get("/api/v1/observability/costs", headers=_headers(), params={"group_by": "model"})
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert "total_cost" in body
    assert "groups" in body


@pytest.mark.asyncio
async def test_model_prices_endpoint(app_client):
    resp = await app_client.get("/api/v1/observability/model-prices", headers=_headers())
    assert resp.status_code == 200
    assert "items" in resp.json()["data"]
    # 创建价格
    resp = await app_client.post(
        "/api/v1/observability/model-prices",
        headers=_headers(),
        json={"model_name": "test-model-v2", "input_price_per_million": 2.0, "output_price_per_million": 10.0},
    )
    assert resp.status_code == 200
    resp = await app_client.get("/api/v1/observability/model-prices", headers=_headers(), params={"model_name": "test-model-v2"})
    items = resp.json()["data"]["items"]
    assert any(i["model_name"] == "test-model-v2" and i["input_price_per_million"] == 2.0 for i in items)


@pytest.mark.asyncio
async def test_quality_endpoint(app_client):
    resp = await app_client.get("/api/v1/observability/quality", headers=_headers())
    assert resp.status_code == 200
    assert "score_count" in resp.json()["data"]


@pytest.mark.asyncio
async def test_alerts_and_incidents_endpoint(app_client):
    resp = await app_client.get("/api/v1/observability/alerts", headers=_headers())
    assert resp.status_code == 200
    assert "items" in resp.json()["data"]
    resp = await app_client.get("/api/v1/observability/incidents", headers=_headers())
    assert resp.status_code == 200
    assert "items" in resp.json()["data"]


@pytest.mark.asyncio
async def test_breakdowns_endpoint(app_client):
    resp = await app_client.get("/api/v1/observability/breakdowns", headers=_headers(), params={"group_by": "status"})
    assert resp.status_code == 200
    assert "items" in resp.json()["data"]


@pytest.mark.asyncio
async def test_trace_export_endpoint(app_client):
    trace_id = f"trace-export-{uuid.uuid4().hex[:6]}"
    await _write_span(trace_id=trace_id, span_id="eroot", span_type="root", span_name="agent.run")
    resp = await app_client.post(
        "/api/v1/observability/traces/export",
        headers=_headers(),
        json={"trace_ids": [trace_id], "include_logs": False, "include_scores": False},
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["count"] == 1
    assert body["traces"][0]["trace_id"] == trace_id
    assert any(s["span_id"] == "eroot" for s in body["traces"][0]["spans"])


@pytest.mark.asyncio
async def test_tenant_isolation_for_logs(app_client):
    # tenant-b 看不到 tenant-a 的日志
    await _write_log(tenant_id="tenant-a", trace_id="trace-iso-1")
    resp = await app_client.get(
        "/api/v1/observability/logs",
        headers={"X-API-Key": "test-key-1", "X-Tenant-Id": "tenant-b"},
    )
    body = resp.json()["data"]
    assert all(i["trace_id"] != "trace-iso-1" or i["tenant_id"] == "tenant-b" for i in body["items"])
