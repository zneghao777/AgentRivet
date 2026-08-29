"""LangGraph 多 Agent Supervisor：路由、规划、执行、校验与综合。"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from ..agents.base_agent import agent_node
from ..config import settings
from ..observability.metrics import metrics
from ..tools.registry import ToolRegistry
from .context import get_tool_context
from .graph_builder import make_tools_node, resolve_request_tools, should_continue
from .models import TaskCard
from .orchestration_events import emit
from .planner import create_plan
from .router import available_agents, decide_route
from .state import SupervisorState
from .synthesizer import aggregate_outputs, synthesize
from .verifier import verify_outputs
from .worker import build_delegate_tools, execute_task

logger = logging.getLogger(__name__)


def build_supervisor_graph(tool_reg: ToolRegistry, *, checkpointer=None):
    """构建编排图；关闭开关时逐字复用既有主 Agent 图。"""
    from .graph_builder import build_main_graph

    if not settings.MULTI_AGENT_ENABLED:
        logger.info("多 Agent 未开启，回退主 Agent 图")
        return build_main_graph(tool_reg, checkpointer=checkpointer)

    tools = tool_reg.all()
    graph = StateGraph(SupervisorState)

    async def router_node(state: dict) -> dict:
        message = _last_user_text(state)
        context = get_tool_context()
        candidates = available_agents(
            state.get("tenant_id") or context.tenant_id,
            context.orchestration_agents or {},
        )
        decision = await decide_route(
            message,
            tenant_id=state.get("tenant_id") or context.tenant_id,
            agents=candidates,
        )
        payload = decision.model_dump()
        await emit("orchestration.router_decision", payload)
        update = {"router_decision": payload, "route": decision.mode}
        if decision.needs_clarification:
            clarification = {
                "question": decision.clarification_question,
                "options": decision.clarification_options,
            }
            await emit("orchestration.clarification", clarification)
            update.update(route="clarification", clarification=clarification)
        elif decision.mode == "direct" and decision.target_agent == "main":
            update["route"] = "main"
        elif decision.mode == "direct":
            task = TaskCard(
                target_agent=decision.target_agent,
                goal=message,
                context_brief=message[:2000],
                expected_output="直接回答用户请求",
            )
            update.update(
                route="direct_worker",
                task=task.model_dump(),
                plan=[task.model_dump()],
            )
        return update

    async def planner_node(state: dict) -> dict:
        context = get_tool_context()
        candidates = available_agents(
            state.get("tenant_id") or context.tenant_id,
            context.orchestration_agents or {},
        )
        plan = await create_plan(_last_user_text(state), agents=candidates)
        if plan is None:
            logger.warning("Planner 连续失败，降级到 main")
            return {"route": "main", "plan": []}
        payload = {
            "summary": plan.summary,
            "execution": plan.execution,
            "tasks": [
                {
                    "task_id": task.task_id,
                    "target_agent": task.target_agent,
                    "goal": task.goal,
                    "constraints": task.constraints,
                }
                for task in plan.tasks
            ],
        }
        metrics.record_orch_plan(state.get("tenant_id", "default"), len(plan.tasks))
        await emit("orchestration.plan", payload)
        return {
            "route": "orchestrate",
            "plan": [task.model_dump() for task in plan.tasks],
            "plan_summary": plan.summary,
            "execution": plan.execution,
        }

    async def worker_node(state: dict) -> dict:
        output = await execute_task(state, state.get("task") or {}, tools)
        return {"agent_outputs": [output]}

    async def sequential_worker_node(state: dict) -> dict:
        outputs = []
        for task in state.get("plan", []):
            outputs.append(await execute_task(state, task, tools))
        return {"agent_outputs": outputs}

    async def direct_worker_node(state: dict) -> dict:
        output = await execute_task(state, state.get("task") or {}, tools)
        verified_state = {**state, "agent_outputs": [output]}
        verified = await verify_outputs(verified_state, tools)
        final = verified[0] if verified else output
        if final.get("status") == "done":
            answer = str(final.get("result") or "")
            error = ""
        else:
            answer = (
                f"目标 Agent 未能完成任务：{final.get('error', 'unknown')}。"
                "请调整请求后重试。"
            )
            error = str(final.get("error") or "worker_failed")
            await emit(
                "orchestration.error",
                {"code": "direct_worker_failed", "message": answer},
            )
        return {
            "verified_outputs": verified,
            "final_answer": answer,
            "orchestration_error": error,
            "messages": [AIMessage(content=answer)],
        }

    def aggregate_node(state: dict) -> dict:
        return {"aggregated": aggregate_outputs(state.get("agent_outputs", []))}

    async def verifier_node(state: dict) -> dict:
        return {"verified_outputs": await verify_outputs(state, tools)}

    async def synthesizer_node(state: dict) -> dict:
        await emit("orchestration.synthesis", {"phase": "started"})
        aggregated = aggregate_outputs(
            state.get("verified_outputs") or state.get("agent_outputs", [])
        )
        answer, degraded = await synthesize(aggregated)
        if degraded == "all_workers_failed":
            await emit(
                "orchestration.error",
                {"code": degraded, "message": answer},
            )
        return {
            "aggregated": aggregated,
            "final_answer": answer,
            "orchestration_error": degraded or "",
            "messages": [AIMessage(content=answer)],
        }

    def main_agent_node(state: dict) -> dict:
        dynamic = get_tool_context().dynamic_tools or []
        delegates = build_delegate_tools(tools)
        state["_tools"] = [*resolve_request_tools(tools), *dynamic, *delegates]
        try:
            return agent_node(state)
        except Exception:  # noqa: BLE001
            logger.exception("main Agent 执行失败")
            answer = "模型服务暂时不可用，请检查模型配置或稍后重试。"
            return {
                "messages": [AIMessage(content=answer)],
                "final_answer": answer,
                "orchestration_error": "main_agent_failed",
            }

    graph.add_node("router", router_node)
    graph.add_node("planner", planner_node)
    graph.add_node("worker", worker_node)
    graph.add_node("sequential_worker", sequential_worker_node)
    graph.add_node("direct_worker", direct_worker_node)
    graph.add_node("aggregate", aggregate_node)
    graph.add_node("verifier", verifier_node)
    graph.add_node("synthesizer", synthesizer_node)
    graph.add_node("main", main_agent_node)
    graph.add_node("main_tools", make_tools_node(tools))

    graph.add_edge(START, "router")
    graph.add_conditional_edges(
        "router",
        lambda state: state.get("route", "main"),
        {
            "clarification": END,
            "main": "main",
            "direct_worker": "direct_worker",
            "orchestrate": "planner",
        },
    )
    graph.add_conditional_edges(
        "main",
        should_continue,
        {"tools": "main_tools", "end": END},
    )
    graph.add_edge("main_tools", "main")
    graph.add_edge("direct_worker", END)
    graph.add_conditional_edges("planner", _dispatch_plan)
    graph.add_edge("worker", "aggregate")
    graph.add_edge("sequential_worker", "aggregate")
    graph.add_edge("aggregate", "verifier")
    graph.add_edge("verifier", "synthesizer")
    graph.add_edge("synthesizer", END)

    return graph.compile(
        checkpointer=checkpointer,
        interrupt_before=["main_tools"] if settings.GRAPH_INTERRUPT_TOOLS else None,
    )


def _dispatch_plan(state: dict):
    if state.get("route") == "main" or not state.get("plan"):
        return "main"
    if state.get("execution") == "sequential":
        return "sequential_worker"
    base = {
        "tenant_id": state.get("tenant_id", "default"),
        "user_id": state.get("user_id", "unknown"),
        "session_id": state.get("session_id", ""),
        "message_id": state.get("message_id"),
        "request_id": state.get("request_id", ""),
    }
    return [Send("worker", {**base, "task": task}) for task in state.get("plan", [])]


def _last_user_text(state: dict) -> str:
    for message in reversed(state.get("messages", [])):
        if isinstance(message, HumanMessage):
            return str(message.content)
        if isinstance(message, tuple) and message and message[0] in {"user", "human"}:
            return str(message[1])
    return ""
