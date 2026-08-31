"""主 Agent 图构建：ReAct 循环 + HITL。

结构：__start__ → agent_node ──条件──→ tools_node ──→ agent_node（循环）
             └──(无 tool_call)──→ END
"""

from __future__ import annotations

import json
import logging

from langchain_core.messages import ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..agents.base_agent import agent_node
from ..config import settings
from ..tools.registry import ToolRegistry
from .context import get_tool_context
from .llm_guard import LLMGuardError
from .state import HarnessState

logger = logging.getLogger(__name__)


def resolve_request_tools(tools: list) -> list:
    """按 AgentHub 请求上下文收窄静态工具集。"""
    ctx = get_tool_context()
    skill_names = set(ctx.allowed_skill_names or [])
    result = []
    for tool in tools:
        name = tool.name
        if name.startswith("skill_"):
            if ctx.allowed_skill_names is None:
                result.append(tool)
                continue
            if any(name.startswith(f"skill_{skill_name}__") for skill_name in skill_names):
                result.append(tool)
            continue
        if name == "memory_search" and not ctx.memory_enabled:
            continue
        if ctx.allowed_tool_names is not None and name not in ctx.allowed_tool_names:
            continue
        result.append(tool)
    return result


def should_continue(state: dict) -> str:
    """条件边：最后一条消息含 tool_calls → 进入 tools，否则结束。"""
    messages = state.get("messages", [])
    if not messages:
        return "end"
    last = messages[-1]
    if hasattr(last, "tool_calls") and last.tool_calls:
        return "tools"
    return "end"


def request_tool_approval(state: dict) -> dict:
    """在工具产生副作用前建立可恢复的 HITL 中断。"""
    messages = state.get("messages", [])
    calls = getattr(messages[-1], "tool_calls", []) if messages else []
    if not calls:
        return {"tool_approval_decision": "approve"}

    mode = state.get("approval_mode") or (
        "manual" if settings.GRAPH_INTERRUPT_TOOLS else "auto"
    )
    if mode == "auto":
        return {"tool_approval_decision": "approve"}

    available = {
        tool.name: tool
        for tool in [
            *(state.get("_tools") or []),
            *(get_tool_context().dynamic_tools or []),
        ]
    }
    public_calls = []
    for call in calls:
        name = str(call.get("name") or "tool")
        tool = available.get(name)
        arguments = call.get("args") if isinstance(call.get("args"), dict) else {}
        public_calls.append(
            {
                "id": str(call.get("id") or "unknown"),
                "name": name,
                "description": str(getattr(tool, "description", "") or "")[:240],
                "arguments": _redact_approval_arguments(arguments),
            }
        )

    response = interrupt(
        {
            "type": "tool_approval",
            "reason": "Agent 请求在继续回答前调用工具",
            "tool_calls": public_calls,
        }
    )
    decision = (
        str(response.get("decision") or "reject")
        if isinstance(response, dict)
        else str(response or "reject")
    )
    if decision == "approve":
        return {"tool_approval_decision": "approve"}

    reason = (
        str(response.get("input") or "用户未批准本次工具调用")[:500]
        if isinstance(response, dict)
        else "用户未批准本次工具调用"
    )
    return {
        "tool_approval_decision": "reject",
        "messages": [
            ToolMessage(
                content=f"用户拒绝执行工具：{reason}",
                tool_call_id=str(call.get("id") or "unknown"),
                name=str(call.get("name") or "tool"),
                status="error",
            )
            for call in calls
        ],
    }


def route_after_approval(state: dict) -> str:
    return "tools" if state.get("tool_approval_decision") == "approve" else "agent"


_APPROVAL_SECRET_KEYS = ("api_key", "apikey", "authorization", "password", "secret", "token")


def _redact_approval_arguments(value):
    if isinstance(value, dict):
        return {
            str(key): (
                "***"
                if any(part in str(key).casefold() for part in _APPROVAL_SECRET_KEYS)
                else _redact_approval_arguments(item)
            )
            for key, item in list(value.items())[:30]
        }
    if isinstance(value, list):
        return [_redact_approval_arguments(item) for item in value[:20]]
    if isinstance(value, str):
        return value[:500]
    return value


def build_main_graph(tool_reg: ToolRegistry, *, checkpointer=None):
    """构建主 Agent 图并编译。

    MUST：图构建函数接收 tool_registry 与 checkpointer，保证可测试性与解耦。
    """
    tools = tool_reg.all()

    def _agent(state: dict) -> dict:
        # 将工具注入 state，供 agent_node 读取
        dynamic_tools = get_tool_context().dynamic_tools or []
        state["_tools"] = [*resolve_request_tools(tools), *dynamic_tools]
        try:
            return agent_node(state)
        except LLMGuardError:
            # 容量/熔断错误必须由 GraphRuntime 映射为稳定 SSE 错误码。
            raise
        except Exception:  # noqa: BLE001
            logger.exception("agent_node 执行失败")
            # 交给 GraphRuntime 记录 error span 并发送显式 error 事件；把异常
            # 包装成 AIMessage 会被误判为成功回答，甚至进入 24h 语义缓存。
            raise

    g = StateGraph(HarnessState)
    g.add_node("agent", _agent)

    _tools = make_tools_node(tools)

    # 动态节点在运行时读取请求态 MCP tools；即使启动时没有静态工具也可工作。
    g.add_node("tools", _tools)
    g.add_node("approval", request_tool_approval)

    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", should_continue, {"tools": "approval", "end": END})
    g.add_conditional_edges(
        "approval",
        route_after_approval,
        {"tools": "tools", "agent": "agent"},
    )
    g.add_edge("tools", "agent")

    return g.compile(
        checkpointer=checkpointer,
    )


def make_tools_node(tools: list):
    """构造运行期按 ToolContext 收窄能力的工具节点。"""

    async def _tools(state: dict) -> dict:
        state_tools = state.get("_tools") or []
        available = {
            tool.name: tool
            for tool in [
                *(state_tools or resolve_request_tools(tools)),
                *(get_tool_context().dynamic_tools or []),
            ]
        }
        messages = state.get("messages", [])
        calls = getattr(messages[-1], "tool_calls", []) if messages else []
        results: list[ToolMessage] = []
        for call in calls:
            name = call.get("name", "")
            tool = available.get(name)
            if tool is None:
                results.append(
                    ToolMessage(
                        content=f"工具不可用或未获授权: {name}",
                        tool_call_id=call.get("id", "unknown"),
                        name=name,
                        status="error",
                    )
                )
                continue
            try:
                output = await tool.ainvoke(call.get("args") or {})
                if isinstance(output, (str, list)):
                    content = output
                else:
                    content = json.dumps(output, ensure_ascii=False, default=str)
                if isinstance(content, str):
                    content = content[: settings.MCP_MAX_OUTPUT_BYTES]
                results.append(
                    ToolMessage(
                        content=content,
                        tool_call_id=call.get("id", "unknown"),
                        name=name,
                    )
                )
            except LLMGuardError:
                # 工具内部的受保护 LLM（例如 query expansion）也必须保留
                # 50300/50400 语义，不能降成普通 ToolMessage。
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("工具 %s 执行失败: %s", name, exc)
                results.append(
                    ToolMessage(
                        content=f"工具执行失败: {str(exc)[:500]}",
                        tool_call_id=call.get("id", "unknown"),
                        name=name,
                        status="error",
                    )
                )
        return {"messages": results}

    return _tools
