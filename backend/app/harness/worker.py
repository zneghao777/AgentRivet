"""AgentHub Worker 的真实执行、重试、持久化与委派工具。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import replace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool

from ..agents.base_agent import build_system_prompt, create_llm
from ..agents.models import OrchestrationAgent
from ..config import settings
from ..observability import audit
from ..observability.metrics import metrics
from ..storage import repositories as repo
from ..storage.db import AsyncSessionLocal
from .agent_circuit_breaker import agent_circuit_breaker
from .context import get_tool_context, pop_tool_context, push_tool_context
from .graph_builder import resolve_request_tools
from .models import TaskCard
from .orchestration_events import emit

logger = logging.getLogger(__name__)

RESPONSIBILITY_TEMPLATE = """# 职责边界
你只负责：{description}。超出此范围时，输出「CANNOT_HANDLE: 原因」，不要尝试硬做。

# 当前 TaskCard
目标：{goal}
必要上下文：{context_brief}
约束：{constraints}
期望输出：{expected_output}
"""


async def execute_task(
    state: dict,
    task_data: dict,
    base_tools: list,
    *,
    verification_reason: str | None = None,
) -> dict:
    """执行单张 TaskCard，并返回可由 reducer 合并的结果。"""
    started = time.monotonic()
    try:
        task = TaskCard.model_validate(task_data)
    except Exception as exc:  # noqa: BLE001
        return _failed_output(task_data, "invalid_task_card", str(exc), started)
    if not task.goal.strip():
        return _failed_output(task.model_dump(), "invalid_task_card", "goal 不能为空", started)

    parent_context = get_tool_context()
    agent = (parent_context.orchestration_agents or {}).get(task.target_agent)
    if agent is None:
        return _failed_output(
            task.model_dump(), "agent_not_available", "目标 Agent 不可用", started
        )
    tenant_id = state.get("tenant_id") or parent_context.tenant_id or agent.tenant_id
    if not agent_circuit_breaker.available(tenant_id, agent.name):
        return _failed_output(task.model_dump(), "agent_circuit_open", "目标 Agent 已熔断", started)

    await _persist_start(state, task, agent)
    await emit(
        "orchestration.worker_started",
        {"task_id": task.task_id, "agent_name": agent.name},
    )

    result: str | None = None
    error: str | None = None
    status = "failed"
    for attempt in range(settings.WORKER_RETRY + 1):
        try:
            result = await asyncio.wait_for(
                _run_agent(
                    agent,
                    task,
                    base_tools,
                    verification_reason=verification_reason,
                    previous_error=error,
                ),
                timeout=settings.WORKER_TIMEOUT_SECONDS,
            )
            if result.lstrip().startswith("CANNOT_HANDLE:"):
                error = "agent_declined"
                break
            status = "done"
            error = None
            break
        except TimeoutError:
            error = "worker_timeout"
        except Exception as exc:  # noqa: BLE001
            error = f"worker_error: {str(exc)[:500]}"
            logger.warning(
                "Worker 执行失败 agent=%s task=%s attempt=%d: %s",
                agent.name,
                task.task_id,
                attempt + 1,
                exc,
            )

    duration_ms = int((time.monotonic() - started) * 1000)
    if status == "done":
        agent_circuit_breaker.record_success(tenant_id, agent.name)
    else:
        agent_circuit_breaker.record_failure(tenant_id, agent.name)
    output = {
        "task_id": task.task_id,
        "target_agent": agent.name,
        "target_agent_id": str(agent.agent_id),
        "goal": task.goal,
        "status": status,
        "result": result if status == "done" else None,
        "error": error,
        "duration_ms": duration_ms,
        "result_preview": (result or "")[:120],
    }
    await _persist_finish(state, output)
    metrics.record_orch_worker(
        tenant_id,
        agent_name=agent.name,
        status=status,
        duration_ms=duration_ms,
    )
    await emit(
        "orchestration.worker_finished",
        {
            "task_id": task.task_id,
            "agent_name": agent.name,
            "status": status,
            "duration_ms": duration_ms,
            "result_preview": output["result_preview"],
            "error": error,
        },
    )
    return output


async def _run_agent(
    agent: OrchestrationAgent,
    task: TaskCard,
    base_tools: list,
    *,
    verification_reason: str | None,
    previous_error: str | None,
) -> str:
    parent = get_tool_context()
    worker_context = replace(
        parent,
        allowed_kb_ids=agent.allowed_kb_ids,
        dynamic_tools=agent.dynamic_tools,
        model_id=agent.model or parent.model_id,
        model_base_url=agent.model_base_url or parent.model_base_url,
        attachments=parent.attachments,
        agent_id=agent.agent_id,
        agent_name=agent.name,
        agent_system_prompt=agent.system_prompt,
        allowed_skill_names=agent.allowed_skill_names,
        allowed_tool_names=agent.allowed_tool_names,
        allowed_capability_ids=agent.allowed_capability_ids,
        memory_enabled=agent.memory_enabled,
    )
    token = push_tool_context(worker_context)
    try:
        tools = [*resolve_request_tools(base_tools), *agent.dynamic_tools]
        unique_tools = {tool.name: tool for tool in tools}
        tools = list(unique_tools.values())[: settings.CAPABILITY_MAX_TOOLS_PER_RUN]
        llm = create_llm(agent.model, agent.model_base_url, parent.model_api_key)
        llm_with_tools = llm.bind_tools(tools) if tools else llm
        boundary = RESPONSIBILITY_TEMPLATE.format(
            description=agent.description,
            goal=task.goal,
            context_brief=task.context_brief or "（无）",
            constraints="；".join(task.constraints) or "（无）",
            expected_output=task.expected_output or "（未指定）",
        )
        if verification_reason:
            boundary += (
                "\n# Verifier 反馈\n"
                f"上次结果未通过校验：{verification_reason}\n请修正后重新回答。"
            )
        if previous_error:
            boundary += f"\n# 上次执行错误\n{previous_error}\n请避开该错误后重试。"
        prompt = [
            SystemMessage(
                content=build_system_prompt(
                    agent.allowed_skill_names,
                    None,
                    agent.system_prompt,
                )
                + "\n\n"
                + boundary
            ),
            HumanMessage(content=_task_user_message(task)),
        ]
        tool_map = {tool.name: tool for tool in tools}
        for _ in range(settings.CAPABILITY_MAX_TOOLS_PER_RUN + 1):
            response: AIMessage = await llm_with_tools.ainvoke(prompt)
            prompt.append(response)
            calls = getattr(response, "tool_calls", []) or []
            if not calls:
                return _message_text(response)
            for call in calls:
                name = call.get("name", "")
                tool = tool_map.get(name)
                if tool is None:
                    content = f"工具不可用或未获授权: {name}"
                    status = "error"
                else:
                    try:
                        value = await tool.ainvoke(call.get("args") or {})
                        content = _tool_text(value)
                        status = "success"
                    except Exception as exc:  # noqa: BLE001
                        content = f"工具执行失败: {str(exc)[:500]}"
                        status = "error"
                prompt.append(
                    ToolMessage(
                        content=content[: settings.MCP_MAX_OUTPUT_BYTES],
                        tool_call_id=call.get("id", "unknown"),
                        name=name,
                        status=status,
                    )
                )
        raise RuntimeError("Worker 工具调用次数超过限制")
    finally:
        pop_tool_context(token)


def build_delegate_tools(base_tools: list) -> list[StructuredTool]:
    """把 internal Agent 暴露为主 Agent 可调用的委派工具。"""
    parent = get_tool_context()
    delegates: list[StructuredTool] = []
    for agent in (parent.orchestration_agents or {}).values():
        if agent.scope != "internal":
            continue

        _delegate = _make_delegate(agent.name, parent, base_tools)

        delegates.append(
            StructuredTool.from_function(
                coroutine=_delegate,
                name=f"delegate_to_{_slug(agent.name)}",
                description=f"委派给 {agent.name}。职责：{agent.description}",
            )
        )
    return delegates


def _make_delegate(agent_name: str, parent, base_tools: list):
    async def delegate(goal: str, context_brief: str = "") -> str:
        task = TaskCard(
            target_agent=agent_name,
            goal=goal,
            context_brief=context_brief,
        )
        output = await execute_task(
            {
                "tenant_id": parent.tenant_id,
                "session_id": str(parent.session_id) if parent.session_id else "",
                "message_id": parent.message_id,
            },
            task.model_dump(),
            base_tools,
        )
        return output.get("result") or (
            f"委派失败：{output.get('error', 'unknown')}"
        )

    return delegate


async def _persist_start(state: dict, task: TaskCard, agent: OrchestrationAgent) -> None:
    session_id = _uuid(state.get("session_id")) or get_tool_context().session_id
    if session_id is None:
        return
    trace_id = None
    try:
        from ..observability.tracing import get_trace_context

        ctx = get_trace_context()
        trace_id = ctx.trace_id if ctx else None
    except Exception:  # noqa: BLE001
        pass
    async with AsyncSessionLocal() as db:
        await repo.start_agent_task(
            db,
            task=task.model_dump(),
            session_id=session_id,
            message_id=state.get("message_id") or get_tool_context().message_id,
            target_agent_id=agent.agent_id,
            trace_id=trace_id,
        )


async def _persist_finish(state: dict, output: dict) -> None:
    session_id = _uuid(state.get("session_id")) or get_tool_context().session_id
    if session_id is None:
        return
    async with AsyncSessionLocal() as db:
        await repo.finish_agent_task(
            db,
            session_id=session_id,
            task_id=output["task_id"],
            status=output["status"],
            result=output.get("result"),
            error=output.get("error"),
            duration_ms=output["duration_ms"],
        )
    await audit.audit(
        get_tool_context().tenant_id or state.get("tenant_id", "default"),
        "orchestration.task.finished",
        session_id=session_id,
        resource_id=output["task_id"],
        detail={
            "task_id": output["task_id"],
            "target_agent": output["target_agent"],
            "status": output["status"],
            "duration_ms": output["duration_ms"],
        },
    )


def _task_user_message(task: TaskCard) -> str:
    artifacts = json.dumps(task.artifacts, ensure_ascii=False) if task.artifacts else "无"
    return (
        f"请完成子任务：{task.goal}\n"
        f"上下文：{task.context_brief or '无'}\n"
        f"约束：{'；'.join(task.constraints) or '无'}\n"
        f"产物引用：{artifacts}"
    )


def _message_text(message: AIMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in content
        ).strip()
    return str(content).strip()


def _tool_text(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _failed_output(task: dict, code: str, detail: str, started: float) -> dict:
    return {
        "task_id": task.get("task_id", uuid.uuid4().hex),
        "target_agent": task.get("target_agent", "unknown"),
        "target_agent_id": None,
        "goal": task.get("goal", ""),
        "status": "failed",
        "result": None,
        "error": code,
        "error_detail": detail[:500],
        "duration_ms": int((time.monotonic() - started) * 1000),
        "result_preview": "",
    }


def _uuid(value) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


def _slug(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_").lower()
    return (normalized or uuid.uuid5(uuid.NAMESPACE_DNS, value).hex[:12])[:40]
