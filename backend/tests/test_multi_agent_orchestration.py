"""MULTI_AGENT_ORCH 的结构化协议、运行时、熔断与 API 回归。"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import ValidationError

from app.agents.models import OrchestrationAgent
from app.config import settings
from app.harness import supervisor
from app.harness.agent_circuit_breaker import agent_circuit_breaker
from app.harness.context import ToolContext, reset_tool_context, set_tool_context
from app.harness.conversation_context import project_conversation_context
from app.harness.graph_runtime import GraphRuntime
from app.harness.models import Plan, RouterDecision, TaskCard
from app.harness.planner import create_plan
from app.harness.router import decide_route, render_agent_cards
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
        self.prompts = []

    async def ainvoke(self, prompt):
        self.prompts.append(prompt)
        value = self.outputs[min(self.calls, len(self.outputs) - 1)]
        self.calls += 1
        return AIMessage(content=value)

    def bind_tools(self, _tools):
        return self


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


def test_conversation_context_projection_is_recent_bounded_and_tool_aware():
    messages = [HumanMessage(content=f"旧消息-{index}") for index in range(12)]
    messages.extend(
        [
            AIMessage(
                content="正在识别快递公司",
                tool_calls=[
                    {
                        "name": "express_lookup",
                        "args": {"number": "73669911731687"},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(
                content="中通快递，需要手机号后四位",
                tool_call_id="call-1",
                name="express_lookup",
            ),
            AIMessage(content="请提供手机号后四位"),
            HumanMessage(content="2030"),
        ]
    )

    context = project_conversation_context(
        messages,
        exclude_latest_user=True,
        max_messages=5,
        max_chars=1000,
    )

    assert "旧消息-0" not in context
    assert "express_lookup" in context
    assert "73669911731687" in context
    assert "中通快递，需要手机号后四位" in context
    assert "请提供手机号后四位" in context
    assert "2030" not in context
    assert len(context) <= 1000


@pytest.mark.asyncio
async def test_router_receives_recent_conversation_context():
    llm = SequenceLlm(
        [
            '{"mode":"direct","target_agent":"main","confidence":0.95,'
            '"reason":"这是对现有快递任务的参数补充，应由通用助手继续",'
            '"needs_clarification":false,"clarification_question":null,'
            '"clarification_options":[]}'
        ]
    )

    decision = await decide_route(
        "2030",
        tenant_id="tenant-a",
        agents={"analyst": _agent("analyst")},
        conversation_context="- 助手：请提供手机号后四位",
        llm=llm,
    )

    prompt = llm.prompts[0][-1].content
    assert "请提供手机号后四位" in prompt
    assert "# 当前用户请求\n2030" in prompt
    assert decision.target_agent == "main"
    assert decision.needs_clarification is False


@pytest.mark.asyncio
async def test_planner_receives_prior_task_context():
    llm = SequenceLlm(
        [
            '{"tasks":[{"target_agent":"analyst","goal":"分析合同付款风险",'
            '"context_brief":"付款周期30天，违约金上限10%"}],'
            '"execution":"parallel","summary":"分析已确认合同约束"}'
        ]
    )

    plan = await create_plan(
        "现在用多 Agent 分析风险",
        agents={"analyst": _agent("analyst")},
        conversation_context="- 用户：付款周期30天，违约金上限10%",
        llm=llm,
    )

    assert plan is not None
    assert "付款周期30天，违约金上限10%" in llm.prompts[0][-1].content
    assert plan.tasks[0].context_brief == "付款周期30天，违约金上限10%"


@pytest.mark.asyncio
async def test_worker_receives_projected_conversation_context(monkeypatch):
    agent = _agent("analyst")
    llm = SequenceLlm(["已结合付款周期30天和违约金上限10%完成风险分析。"])

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.harness.worker.create_llm", lambda *_args, **_kwargs: llm)
    monkeypatch.setattr("app.harness.worker._persist_start", noop)
    monkeypatch.setattr("app.harness.worker._persist_finish", noop)
    monkeypatch.setattr("app.harness.worker.emit", noop)
    set_tool_context(
        ToolContext(tenant_id="tenant-a", orchestration_agents={"analyst": agent})
    )
    try:
        output = await execute_task(
            {
                "tenant_id": "tenant-a",
                "conversation_context": "- 用户：付款周期30天，违约金上限10%",
            },
            TaskCard(target_agent="analyst", goal="分析合同风险").model_dump(),
            [],
        )
    finally:
        reset_tool_context()

    assert output["status"] == "done"
    assert "付款周期30天，违约金上限10%" in llm.prompts[0][0].content


@pytest.mark.asyncio
async def test_synthesizer_receives_original_conversation_task(monkeypatch):
    llm = SequenceLlm(["综合结论"])
    monkeypatch.setattr(
        "app.harness.synthesizer.create_llm",
        lambda *_args, **_kwargs: llm,
    )

    answer, error = await synthesize(
        {
            "results": [
                {
                    "task_id": "a",
                    "target_agent": "analyst",
                    "goal": "分析付款风险",
                    "status": "done",
                    "result": "付款风险结果足够完整，可以参与最终综合。",
                },
                {
                    "task_id": "b",
                    "target_agent": "writer",
                    "goal": "形成建议",
                    "status": "done",
                    "result": "建议结果足够完整，可以参与最终综合。",
                },
            ],
            "failures": [],
        },
        request_context="付款周期30天，违约金上限10%",
    )

    assert answer == "综合结论"
    assert error is None
    assert "付款周期30天，违约金上限10%" in llm.prompts[0][-1].content


def test_task_models_forbid_extra_fields_and_bound_plan():
    with pytest.raises(ValidationError):
        TaskCard(target_agent="a", goal="x", unexpected=True)
    with pytest.raises(ValidationError):
        Plan(
            tasks=[TaskCard(target_agent="a", goal=str(index)) for index in range(6)],
            summary="too many",
        )


def test_agent_cards_separate_machine_name_from_scope():
    cards = render_agent_cards({"analyst": _agent("analyst")})

    assert '- machine_name: "analyst"' in cards
    assert 'scope: "internal"' in cards
    assert "analyst [internal]" not in cards


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
async def test_router_accepts_exact_legacy_agent_display_alias_without_retry():
    llm = SequenceLlm(
        [
            '{"mode":"direct","target_agent":"analyst [internal]",'
            '"confidence":0.9,"reason":"analyst 负责分析",'
            '"needs_clarification":false,"clarification_question":null,'
            '"clarification_options":[]}'
        ]
    )

    decision = await decide_route(
        "分析销售数据",
        tenant_id="tenant-a",
        agents={"analyst": _agent("analyst")},
        llm=llm,
    )

    assert llm.calls == 1
    assert decision.target_agent == "analyst"


@pytest.mark.asyncio
async def test_router_does_not_fuzzy_match_legacy_agent_display_alias():
    invalid = (
        '{"mode":"direct","target_agent":"analyst [internal] extra",'
        '"confidence":0.9,"reason":"analyst 负责分析",'
        '"needs_clarification":false,"clarification_question":null,'
        '"clarification_options":[]}'
    )
    valid = invalid.replace("analyst [internal] extra", "analyst")
    llm = SequenceLlm([invalid, valid])

    decision = await decide_route(
        "分析销售数据",
        tenant_id="tenant-a",
        agents={"analyst": _agent("analyst")},
        llm=llm,
    )

    assert llm.calls == 2
    assert decision.target_agent == "analyst"


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


@pytest.mark.asyncio
async def test_planner_accepts_exact_legacy_agent_display_alias_without_retry():
    llm = SequenceLlm(
        [
            '{"tasks":[{"target_agent":"analyst [internal]",'
            '"goal":"分析数据","context_brief":"销售数据",'
            '"constraints":[]}],"execution":"parallel","summary":"并行分析"}'
        ]
    )

    plan = await create_plan(
        "分析销售数据",
        agents={"analyst": _agent("analyst")},
        llm=llm,
    )

    assert plan is not None
    assert llm.calls == 1
    assert plan.tasks[0].target_agent == "analyst"


@pytest.mark.asyncio
async def test_planner_replaces_invalid_model_task_id_without_retry():
    model_task_id = "a" * 31
    llm = SequenceLlm(
        [
            '{"tasks":[{"task_id":"'
            + model_task_id
            + '","target_agent":"analyst","goal":"分析数据",'
            '"context_brief":"销售数据","constraints":[]}],'
            '"execution":"parallel","summary":"并行分析"}'
        ]
    )

    plan = await create_plan(
        "分析销售数据",
        agents={"analyst": _agent("analyst")},
        llm=llm,
    )

    assert plan is not None
    assert llm.calls == 1
    generated_id = plan.tasks[0].task_id
    assert generated_id != model_task_id
    assert uuid.UUID(generated_id).hex == generated_id


@pytest.mark.asyncio
async def test_planner_replaces_all_model_task_ids_with_unique_server_ids():
    model_task_ids = {"b" * 32, "c" * 31}
    llm = SequenceLlm(
        [
            json.dumps(
                {
                    "tasks": [
                        {
                            "task_id": "b" * 32,
                            "target_agent": "analyst",
                            "goal": "分析数据",
                        },
                        {
                            "task_id": "c" * 31,
                            "target_agent": "writer",
                            "goal": "撰写报告",
                        },
                    ],
                    "execution": "parallel",
                    "summary": "并行处理",
                },
                ensure_ascii=False,
            )
        ]
    )

    plan = await create_plan(
        "分析并撰写报告",
        agents={"analyst": _agent("analyst"), "writer": _agent("writer")},
        llm=llm,
    )

    assert plan is not None
    assert llm.calls == 1
    generated_ids = [task.task_id for task in plan.tasks]
    assert len(generated_ids) == len(set(generated_ids)) == 2
    assert model_task_ids.isdisjoint(generated_ids)
    assert all(uuid.UUID(value).hex == value for value in generated_ids)


@pytest.mark.asyncio
async def test_planner_still_retries_other_unknown_task_fields():
    invalid = (
        '{"tasks":[{"task_id":"not-trusted","target_agent":"analyst",'
        '"goal":"分析数据","unexpected":"must-fail"}],'
        '"execution":"parallel","summary":"分析"}'
    )
    valid = (
        '{"tasks":[{"task_id":"also-not-trusted","target_agent":"analyst",'
        '"goal":"分析数据"}],"execution":"parallel","summary":"分析"}'
    )
    llm = SequenceLlm([invalid, valid])

    plan = await create_plan(
        "分析销售数据",
        agents={"analyst": _agent("analyst")},
        llm=llm,
    )

    assert plan is not None
    assert llm.calls == 2
    assert plan.tasks[0].task_id not in {"not-trusted", "also-not-trusted"}


@pytest.mark.asyncio
async def test_planner_does_not_normalize_legacy_alias_with_wrong_scope():
    invalid = (
        '{"tasks":[{"target_agent":"analyst [user]","goal":"分析数据",'
        '"context_brief":"销售数据","constraints":[]}],'
        '"execution":"parallel","summary":"并行分析"}'
    )
    valid = invalid.replace("analyst [user]", "analyst")
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
    route_context = ""
    plan_context = ""
    worker_contexts: list[str] = []

    async def route(*_args, **kwargs):
        nonlocal route_context
        route_context = kwargs.get("conversation_context", "")
        return RouterDecision(
            mode="orchestrate",
            target_agent="main",
            confidence=.9,
            reason="analyst 与 writer 分别负责分析和写作",
        )

    async def plan(*_args, **kwargs):
        nonlocal plan_context
        plan_context = kwargs.get("conversation_context", "")
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
        worker_contexts.append(_state.get("conversation_context", ""))
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

    async def synthesize(_aggregated, **_kwargs):
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
            {
                "messages": [
                    ("user", "销售额是120万元，成本是80万元"),
                    ("assistant", "已记录销售与成本数据"),
                    ("user", "现在请用多 Agent 分析并写报告"),
                ],
                "tenant_id": "tenant-a",
            }
        )
    finally:
        reset_tool_context()
    assert result["final_answer"] == "综合后的最终回答"
    assert len(result["agent_outputs"]) == 2
    assert max_active == 2
    assert "销售额是120万元，成本是80万元" in route_context
    assert route_context == plan_context
    assert worker_contexts == [route_context, route_context]


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
