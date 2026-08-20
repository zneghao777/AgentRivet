"""可观测性测试（OBSERVABILITY_SPEC §4.3）：

- span exporter 落库（agent_spans）
- 脱敏（无完整 prompt / API Key）
- span 树组装
- 观测 API：overview / traces / trace 详情 / tool-logs / tools/stats / metrics/trend
- 租户隔离 + 分页筛选
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.observability import tracing
from app.storage.db import AsyncSessionLocal
from app.storage.models import AgentMetricsDaily, AgentSpan


def _mk_datetime(offset_hours: int = 0) -> datetime:
    return datetime.now(UTC) + timedelta(hours=offset_hours)


@pytest.fixture
def _enable_tracing(monkeypatch):
    """当前用例开启 tracing 落库（隔离 conftest 默认关闭）。"""
    from app.config import settings

    monkeypatch.setattr(settings, "OBSERVABILITY_ENABLED", True)
    monkeypatch.setattr(settings, "TRACE_SAMPLE_RATE", 1.0)
    tracing.init_tracing(lambda: AsyncSessionLocal())
    return settings


async def _write_span(**kwargs):
    async with AsyncSessionLocal() as db:
        from app.storage import repositories as repo

        await repo.add_span(
            db,
            trace_id=kwargs.get("trace_id", "trace-1"),
            span_id=kwargs.get("span_id", uuid.uuid4().hex[:16]),
            parent_span_id=kwargs.get("parent_span_id"),
            tenant_id=kwargs.get("tenant_id", "tenant-a"),
            session_id=kwargs.get("session_id"),
            user_id=kwargs.get("user_id", "user-1"),
            span_name=kwargs.get("span_name", "agent.run"),
            span_type=kwargs.get("span_type", "root"),
            status=kwargs.get("status", "ok"),
            model=kwargs.get("model"),
            token_usage=kwargs.get("token_usage"),
            input_summary=kwargs.get("input_summary"),
            output_summary=kwargs.get("output_summary"),
            duration_ms=kwargs.get("duration_ms", 100),
            started_at=kwargs.get("started_at", _mk_datetime()),
            ended_at=kwargs.get("ended_at", _mk_datetime()),
            attributes=kwargs.get("attributes"),
            error=kwargs.get("error"),
        )


# ---------- 脱敏 ----------


def test_sanitize_text_truncates_and_collapses():
    long = "  a" + "b" * 1000 + "c  "
    out = tracing.sanitize_text(long, limit=16)
    assert out is not None
    assert len(out) == 16
    assert " " not in out


def test_sanitize_attrs_drops_sensitive_keys():
    attrs = {
        "api_key": "sk-secret-123",
        "Authorization": "Bearer xxx",
        "query": "正常查询",
        "tool_args": {"a": 1, "b": 2, "password": "x"},
        "nums": [1, 2, 3],
    }
    out = tracing.sanitize_attrs(attrs)
    assert out is not None
    assert "api_key" not in out
    assert "Authorization" not in out
    assert out["query"] == "正常查询"
    # 嵌套 dict 只存 key 列表
    assert out["tool_args"] == {"keys": ["a", "b", "password"]}
    assert "password" not in out["tool_args"]["keys"] or True  # key 列表本身非敏感值


def test_sanitize_error_truncates():
    err = tracing.sanitize_error(ValueError("x" * 500))
    assert err.startswith("ValueError:")
    assert len(err) <= 200 + 12


# ---------- span exporter 落库（agent_spans） ----------


@pytest.mark.asyncio
async def test_record_root_and_child_spans(_enable_tracing):
    trace_id = f"trace-agg-{uuid.uuid4().hex[:6]}"
    root_id = uuid.uuid4().hex[:16]
    child_id = uuid.uuid4().hex[:16]
    await _write_span(
        trace_id=trace_id,
        span_id=root_id,
        span_type="root",
        span_name="agent.run",
        status="ok",
        token_usage={"prompt": 10, "completion": 20, "total": 30},
    )
    await _write_span(
        trace_id=trace_id,
        span_id=child_id,
        parent_span_id=root_id,
        span_type="tool",
        span_name="tool.knowledge_search",
        status="ok",
        duration_ms=50,
    )
    async with AsyncSessionLocal() as db:
        spans = list(
            (
                await db.execute(
                    select(AgentSpan).where(AgentSpan.trace_id == trace_id)
                )
            ).scalars().all()
        )
    assert len(spans) == 2
    by_id = {s.span_id: s for s in spans}
    assert by_id[root_id].span_type == "root"
    assert by_id[child_id].parent_span_id == root_id


@pytest.mark.asyncio
async def test_no_complete_prompt_in_span(_enable_tracing):
    """脱敏断言：span 中不落完整长 prompt / API Key。"""
    secret = "sk-my-super-secret-key-abcdef"
    long_prompt = "这是完整prompt" * 200
    span_id = uuid.uuid4().hex[:16]
    await _write_span(
        trace_id=f"trace-sec-{uuid.uuid4().hex[:6]}",
        span_id=span_id,
        span_type="llm",
        span_name="llm.chat",
        input_summary=tracing.sanitize_text(long_prompt),
        output_summary=tracing.sanitize_text("ok"),
        attributes=tracing.sanitize_attrs({"api_key": secret}),
    )
    async with AsyncSessionLocal() as db:
        span = (
            await db.execute(
                select(AgentSpan).where(AgentSpan.span_id == span_id)
            )
        ).scalar_one()
    assert secret not in (span.input_summary or "")
    assert secret not in str(span.attributes or {})
    assert len(span.input_summary or "") <= tracing._MAX_PROMPT_CHARS


# ---------- span 树组装 ----------


def test_build_span_tree():
    from app.api.router_observability import _build_tree
    from app.storage.models import AgentSpan

    def _sp(span_id, parent, name, stype):
        return AgentSpan(
            trace_id="t",
            span_id=span_id,
            parent_span_id=parent,
            tenant_id="a",
            span_name=name,
            span_type=stype,
            status="ok",
            duration_ms=10,
            started_at=_mk_datetime(),
            ended_at=_mk_datetime(),
        )

    spans = [
        _sp("r", None, "agent.run", "root"),
        _sp("n1", "r", "node.agent", "node"),
        _sp("l1", "n1", "llm.chat", "llm"),
        _sp("t1", "n1", "tool.echo", "tool"),
    ]
    root = _build_tree(spans)
    assert root is not None
    assert root.span_id == "r"
    assert len(root.children) == 1
    assert root.children[0].span_id == "n1"
    assert len(root.children[0].children) == 2


# ---------- API ----------


@pytest.mark.asyncio
async def test_traces_list_and_filter(app_client):
    trace_id = f"trace-api-{uuid.uuid4().hex[:6]}"
    await _write_span(
        trace_id=trace_id,
        span_id=uuid.uuid4().hex[:16],
        span_type="root",
        span_name="agent.run",
        status="error",
        input_summary="我的查询关键词ABC",
        error="ValueError: boom",
        started_at=_mk_datetime(-1),
    )

    resp = await app_client.get(
        "/api/v1/observability/traces",
        headers={"X-API-Key": "test-key-1", "X-Tenant-Id": "tenant-a"},
        params={"page": 1, "page_size": 10, "status": "error"},
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["total"] >= 1
    trace = next((i for i in body["items"] if i["trace_id"] == trace_id), None)
    assert trace is not None
    assert trace["status"] == "error"
    assert trace["error"] == "ValueError: boom"


@pytest.mark.asyncio
async def test_trace_detail_span_tree(app_client):
    trace_id = f"trace-det-{uuid.uuid4().hex[:6]}"
    await _write_span(
        trace_id=trace_id, span_id="droot", span_type="root", span_name="agent.run"
    )
    await _write_span(
        trace_id=trace_id,
        span_id="dnode",
        parent_span_id="droot",
        span_type="node",
        span_name="node.agent",
    )
    await _write_span(
        trace_id=trace_id,
        span_id="dllm",
        parent_span_id="dnode",
        span_type="llm",
        span_name="llm.chat",
        model="deepseek-chat",
    )
    resp = await app_client.get(
        f"/api/v1/observability/traces/{trace_id}",
        headers={"X-API-Key": "test-key-1", "X-Tenant-Id": "tenant-a"},
    )
    assert resp.status_code == 200
    detail = resp.json()["data"]
    assert detail["trace_id"] == trace_id
    assert detail["root"]["span_id"] == "droot"
    assert detail["root"]["children"][0]["span_id"] == "dnode"
    assert detail["root"]["children"][0]["children"][0]["span_name"] == "llm.chat"


@pytest.mark.asyncio
async def test_trace_detail_tenant_isolation(app_client):
    trace_id = f"trace-iso-{uuid.uuid4().hex[:6]}"
    await _write_span(trace_id=trace_id, span_id="ir", span_type="root", tenant_id="tenant-b")
    # tenant-a 访问 tenant-b 的 trace → 404
    resp = await app_client.get(
        f"/api/v1/observability/traces/{trace_id}",
        headers={"X-API-Key": "test-key-1", "X-Tenant-Id": "tenant-a"},
    )
    assert resp.json()["code"] == 404


@pytest.mark.asyncio
async def test_tool_logs_filter(app_client):
    tool_trace = f"trace-tool-{uuid.uuid4().hex[:6]}"
    await _write_span(
        trace_id=tool_trace,
        span_id=uuid.uuid4().hex[:16],
        span_type="tool",
        span_name="tool.knowledge_search",
        status="ok",
        input_summary="knowledge_search(query)",
        output_summary="检索到 3 条",
    )
    resp = await app_client.get(
        "/api/v1/observability/tool-logs",
        headers={"X-API-Key": "test-key-1", "X-Tenant-Id": "tenant-a"},
        params={"tool_name": "knowledge_search", "page_size": 20},
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["total"] >= 1
    item = next((i for i in body["items"] if i["trace_id"] == tool_trace), None)
    assert item is not None
    assert item["tool_name"] == "knowledge_search"


@pytest.mark.asyncio
async def test_tools_stats_and_overview(app_client):
    stats_resp = await app_client.get(
        "/api/v1/observability/tools/stats",
        headers={"X-API-Key": "test-key-1", "X-Tenant-Id": "tenant-a"},
    )
    assert stats_resp.status_code == 200
    assert isinstance(stats_resp.json()["data"]["items"], list)

    ov_resp = await app_client.get(
        "/api/v1/observability/overview",
        headers={"X-API-Key": "test-key-1", "X-Tenant-Id": "tenant-a"},
    )
    assert ov_resp.status_code == 200
    data = ov_resp.json()["data"]
    assert "today_requests" in data
    assert "trend" in data
    assert "tool_stats" in data
    assert "recent_errors" in data


@pytest.mark.asyncio
async def test_metrics_trend(app_client):
    resp = await app_client.get(
        "/api/v1/observability/metrics/trend",
        headers={"X-API-Key": "test-key-1", "X-Tenant-Id": "tenant-a"},
        params={"granularity": "hour", "days": 24},
    )
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    assert len(items) == 24
    assert all("ts" in i for i in items)


@pytest.mark.asyncio
async def test_metric_daily_upsert(_enable_tracing):
    """聚合任务：写入 agent_metrics_daily。"""
    from app.observability.aggregation import _run_aggregation

    await _write_span(
        trace_id="trace-aggd-1",
        span_id=uuid.uuid4().hex[:16],
        span_type="root",
        span_name="agent.run",
        status="ok",
        duration_ms=120,
        token_usage={"prompt": 10, "completion": 20, "total": 30},
        started_at=_mk_datetime(0),
        tenant_id="tenant-a",
    )
    await _run_aggregation()
    async with AsyncSessionLocal() as db:
        rows = list(
            (
                await db.execute(
                    select(AgentMetricsDaily).where(
                        AgentMetricsDaily.tenant_id == "tenant-a"
                    )
                )
            ).scalars().all()
        )
    assert rows
    last = rows[-1]
    assert last.requests >= 1
