"""MULTI_AGENT_ORCH 的结构化协议、运行时、熔断与 API 回归。"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from app.agents.models import OrchestrationAgent
from app.config import settings
from app.harness import supervisor
from app.harness.agent_circuit_breaker import agent_circuit_breaker
from app.harness.context import ToolContext, reset_tool_context, set_tool_context
from app.harness.graph_runtime import GraphRuntime
from app.harness.models import Plan, RouterDecision, TaskCard
from app.harness.planner import create_plan
from app.harness.router import decide_route
from app.harness.synthesizer import synthesize
from app.harness.verifier import verify_outputs, verify_result
from app.harness.worker import execute_task
from app.storage import repositories as repo
from app.tools.registry import ToolRegistry

AUTH = {"X-API-Key": "test-key-1"}


class SequenceLlm:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls = 0

    async def ainvoke(self, _prompt):
        value = self.outputs[min(self.calls, len(self.outputs) - 1)]
        self.calls += 1
        return AIMessage(content=value)


def _agent(name: str, scope: str = "internal") -> OrchestrationAgent:
    return OrchestrationAgent(
        agent_id=uuid.uuid4(),
        tenant_id="tenant-a",
        name=name,
        description=f"负责{name}任务。能处理对应输入；不能处理其他领域。",
        scope=scope,
        system_prompt=None,
        model=None,
        model_base_url=None,
        memory_enabled=False,
        capability_summary=f"Skills: {name}",
    )


def test_task_models_forbid_extra_fields_and_bound_plan():
    with pytest.raises(ValidationError):
        TaskCard(target_agent="a", goal="x", unexpected=True)
    with pytest.raises(ValidationError):
        Plan(
            tasks=[TaskCard(target_agent="a", goal=str(index)) for index in range(6)],
            summary="too many",
        )


@pytest.mark.asyncio
async def test_router_retries_invalid_json_and_forces_low_confidence_clarification():
    llm = SequenceLlm(
        [
            "not-json",
            '{"mode":"direct","target_agent":"analyst","confidence":0.3,'
            '"reason":"analyst 负责分析","needs_clarification":false,'
            '"clarification_question":null,"clarification_options":[]}',
        ]
    )
    decision = await decide_route(
        "处理一下",
        tenant_id="tenant-a",
        agents={"analyst": _agent("analyst")},
        llm=llm,
    )
    assert llm.calls == 2
    assert decision.target_agent == "analyst"
    assert decision.needs_clarification is True
    assert decision.clarification_question


@pytest.mark.asyncio
async def test_planner_retries_unknown_agent_then_returns_valid_plan():
    invalid = (
        '{"tasks":[{"target_agent":"missing","goal":"分析数据"}],'
        '"execution":"parallel","summary":"分析"}'
    )
    valid = (
        '{"tasks":[{"target_agent":"analyst","goal":"分析数据",'
        '"context_brief":"销售数据","constraints":[]}],'
        '"execution":"parallel","summary":"并行分析"}'
    )
    llm = SequenceLlm([invalid, valid])
    plan = await create_plan(
        "分析销售数据",
        agents={"analyst": _agent("analyst")},
        llm=llm,
    )
    assert plan is not None
    assert llm.calls == 2
    assert plan.tasks[0].target_agent == "analyst"


def test_circuit_breaker_opens_and_recovers(monkeypatch):
    agent_circuit_breaker.reset()
    monkeypatch.setattr(settings, "AGENT_CIRCUIT_BREAKER_THRESHOLD", 2)
    monkeypatch.setattr(settings, "AGENT_CIRCUIT_BREAKER_COOLDOWN", 100)
    assert agent_circuit_breaker.record_failure("t", "a") is False
    assert agent_circuit_breaker.record_failure("t", "a") is True
    assert agent_circuit_breaker.available("t", "a") is False
    monkeypatch.setattr(settings, "AGENT_CIRCUIT_BREAKER_COOLDOWN", 0)
    assert agent_circuit_breaker.available("t", "a") is True


@pytest.mark.asyncio
async def test_verifier_reassigns_once(monkeypatch):
    task = TaskCard(target_agent="analyst", goal="生成完整分析")
    bad = {
        "task_id": task.task_id,
        "target_agent": "analyst",
        "goal": task.goal,
        "status": "done",
        "result": "太短",
    }
    calls = 0

    async def retry(_state, task_data, _tools, **_kwargs):
        nonlocal calls
        calls += 1
        return {
            "task_id": task_data["task_id"],
            "target_agent": "analyst",
            "goal": task_data["goal"],
            "status": "done",
            "result": "这是重新执行后生成的完整分析结果，长度足以通过代码级校验。",
        }

    monkeypatch.setattr("app.harness.verifier.execute_task", retry)
    outputs = await verify_outputs(
        {"plan": [task.model_dump()], "agent_outputs": [bad]},
        [],
    )
    assert calls == 1
    assert verify_result(outputs[0])[0] is True


@pytest.mark.asyncio
async def test_worker_decline_is_not_retried(monkeypatch):
    agent = _agent("analyst")
    task = TaskCard(target_agent="analyst", goal="处理超出职责的任务")
    calls = 0

    async def run_agent(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return "CANNOT_HANDLE: 超出数据分析职责"

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.harness.worker._run_agent", run_agent)
    monkeypatch.setattr("app.harness.worker._persist_start", noop)
    monkeypatch.setattr("app.harness.worker._persist_finish", noop)
    monkeypatch.setattr("app.harness.worker.emit", noop)
    monkeypatch.setattr(settings, "WORKER_RETRY", 3)
    set_tool_context(
        ToolContext(tenant_id="tenant-a", orchestration_agents={"analyst": agent})
    )
    try:
        output = await execute_task(
            {"tenant_id": "tenant-a"}, task.model_dump(), []
        )
    finally:
        reset_tool_context()
    assert calls == 1
    assert output["status"] == "failed"
    assert output["error"] == "agent_declined"


@pytest.mark.asyncio
async def test_all_worker_failures_return_explicit_retry_message():
    answer, error = await synthesize(
        {
            "results": [],
            "failures": [
                {"target_agent": "analyst", "error": "worker_timeout"},
                {"target_agent": "writer", "error": "agent_declined"},
            ],
        }
    )
    assert error == "all_workers_failed"
    assert "所有子任务均未成功" in answer
    assert "worker_timeout" in answer


@pytest.mark.asyncio
async def test_parallel_graph_fanout_and_synthesis(monkeypatch):
    monkeypatch.setattr(settings, "MULTI_AGENT_ENABLED", True)
    agents = {"analyst": _agent("analyst"), "writer": _agent("writer")}
    active = 0
    max_active = 0

    async def route(*_args, **_kwargs):
        return RouterDecision(
            mode="orchestrate",
            target_agent="main",
            confidence=.9,
            reason="analyst 与 writer 分别负责分析和写作",
        )

    async def plan(*_args, **_kwargs):
        return Plan(
            tasks=[
                TaskCard(target_agent="analyst", goal="分析数据"),
                TaskCard(target_agent="writer", goal="撰写报告"),
            ],
            execution="parallel",
            summary="并行执行两个子任务",
        )

    async def worker(_state, task, _tools, **_kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(.02)
        active -= 1
        return {
            "task_id": task["task_id"],
            "target_agent": task["target_agent"],
            "goal": task["goal"],
            "status": "done",
            "result": f"{task['target_agent']} 返回了足够完整且可验证的子任务结果。",
            "error": None,
            "duration_ms": 20,
            "result_preview": "完成",
        }

    async def verify(state, _tools):
        return state["agent_outputs"]

    async def synthesize(_aggregated):
        return "综合后的最终回答", None

    monkeypatch.setattr(supervisor, "decide_route", route)
    monkeypatch.setattr(supervisor, "create_plan", plan)
    monkeypatch.setattr(supervisor, "execute_task", worker)
    monkeypatch.setattr(supervisor, "verify_outputs", verify)
    monkeypatch.setattr(supervisor, "synthesize", synthesize)
    graph = supervisor.build_supervisor_graph(ToolRegistry())
    set_tool_context(ToolContext(tenant_id="tenant-a", orchestration_agents=agents))
    try:
        result = await graph.ainvoke(
            {"messages": [("user", "分析并写报告")], "tenant_id": "tenant-a"}
        )
    finally:
        reset_tool_context()
    assert result["final_answer"] == "综合后的最终回答"
    assert len(result["agent_outputs"]) == 2
    assert max_active == 2


@pytest.mark.asyncio
async def test_clarification_sse_has_no_dummy_reply(monkeypatch):
    monkeypatch.setattr(settings, "MULTI_AGENT_ENABLED", True)

    async def route(*_args, **_kwargs):
        return RouterDecision(
            mode="direct",
            target_agent="analyst",
            confidence=.2,
            reason="请求过于模糊",
            needs_clarification=True,
            clarification_question="你希望分析数据还是生成报告？",
            clarification_options=["分析数据", "生成报告"],
        )

    monkeypatch.setattr(supervisor, "decide_route", route)
    graph = supervisor.build_supervisor_graph(ToolRegistry())
    runtime = GraphRuntime(graph)
    set_tool_context(
        ToolContext(
            tenant_id="tenant-a",
            orchestration_agents={"analyst": _agent("analyst")},
        )
    )
    try:
        frames = [
            frame
            async for frame in runtime.astream(
                "thr-clarify", "处理一下", "tenant-a", "u1", []
            )
        ]
    finally:
        reset_tool_context()
    text = "".join(frames)
    assert "orchestration.router_decision" in text
    assert "orchestration.clarification" in text
    assert "message_delta" not in text
    assert "run_end" in text


@pytest.mark.asyncio
async def test_main_agent_failure_returns_safe_error_sse(monkeypatch):
    monkeypatch.setattr(settings, "MULTI_AGENT_ENABLED", True)

    def broken_agent(_state):
        raise RuntimeError("provider-secret-detail")

    monkeypatch.setattr(supervisor, "agent_node", broken_agent)
    graph = supervisor.build_supervisor_graph(ToolRegistry())
    runtime = GraphRuntime(graph)
    set_tool_context(ToolContext(tenant_id="tenant-a", orchestration_agents={}))
    try:
        frames = [
            frame
            async for frame in runtime.astream(
                "thr-main-failure", "你好", "tenant-a", "u1", []
            )
        ]
    finally:
        reset_tool_context()
    text = "".join(frames)
    assert "模型服务暂时不可用" in text
    assert '"status": "error"' in text
    assert "provider-secret-detail" not in text


@pytest.mark.asyncio
async def test_agent_tasks_api_replays_persisted_task(app_client, db_session):
    created = await app_client.post("/api/v1/sessions", headers=AUTH, json={})
    session_id = uuid.UUID(created.json()["data"]["session_id"])
    task = TaskCard(target_agent="analyst", goal="分析销售数据")
    await repo.start_agent_task(
        db_session,
        task=task.model_dump(),
        session_id=session_id,
        message_id=None,
        target_agent_id=None,
        trace_id="trace-task-test",
    )
    await repo.finish_agent_task(
        db_session,
        session_id=session_id,
        task_id=task.task_id,
        status="done",
        result="已完成销售数据分析并输出关键趋势。",
        error=None,
        duration_ms=12,
    )
    response = await app_client.get(
        f"/api/v1/sessions/{session_id}/tasks", headers=AUTH
    )
    assert response.status_code == 200
    item = response.json()["data"]["items"][0]
    assert item["task_id"] == task.task_id
    assert item["status"] == "done"


@pytest.mark.asyncio
async def test_route_dry_run_does_not_execute_worker(app_client, monkeypatch):
    created = await app_client.post(
        "/api/v1/agents",
        headers=AUTH,
        json={
            "name": f"route-agent-{uuid.uuid4().hex[:8]}",
            "description": "负责路由分析。能识别请求；不能执行写操作。",
            "scope": "internal",
        },
    )
    agent_id = created.json()["data"]["id"]
    worker_called = False

    async def route(*_args, **_kwargs):
        return RouterDecision(
            mode="direct",
            target_agent="main",
            confidence=.88,
            reason="通用助手可以直接完成",
        )

    async def forbidden_worker(*_args, **_kwargs):
        nonlocal worker_called
        worker_called = True

    monkeypatch.setattr("app.api.router_admin.decide_route", route)
    monkeypatch.setattr(supervisor, "execute_task", forbidden_worker)
    response = await app_client.post(
        f"/api/v1/admin/agents/{agent_id}/route-dry-run",
        headers=AUTH,
        json={"message": "测试路由"},
    )
    assert response.status_code == 200
    assert response.json()["data"]["confidence"] == .88
    assert worker_called is False
    await app_client.delete(
        f"/api/v1/agents/{agent_id}?hard=true",
        headers=AUTH,
    )
