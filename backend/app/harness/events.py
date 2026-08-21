"""SSE 事件构造与序列化。

统一事件封装：每行一个 data: JSON，事件类型由 event: 标识。
"""

from __future__ import annotations

import json
import uuid
from typing import Any


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def sse_frame(event: str, data: dict[str, Any]) -> str:
    """构造一个 SSE 帧。"""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def session_start(session_id: str, thread_id: str) -> str:
    return sse_frame("session_start", {"session_id": session_id, "thread_id": thread_id})


def trace_start(*, trace_id: str, request_id: str | None, session_id: str | None) -> str:
    """Trace 创建事件（OBSERVABILITY_SPEC §12.4）：SSE 必须发送。"""
    return sse_frame(
        "trace_start",
        {"trace_id": trace_id, "request_id": request_id, "session_id": session_id},
    )


def message_start() -> str:
    return sse_frame("message_start", {"id": _new_id("msg"), "role": "assistant"})


def message_delta(msg_id: str, content: str) -> str:
    return sse_frame("message_delta", {"id": msg_id, "content": content})


def tool_start(tool_id: str, name: str, arguments: dict[str, Any]) -> str:
    return sse_frame(
        "tool_start", {"id": tool_id, "name": name, "arguments": arguments}
    )


def tool_end(tool_id: str, name: str, result: str) -> str:
    return sse_frame(
        "tool_end", {"id": tool_id, "name": name, "result": result}
    )


def run_step_start(
    *,
    step_id: str,
    kind: str,
    name: str,
    parent_id: str | None,
    summary: str | None = None,
    started_at: str | None = None,
) -> str:
    """发送面向聊天 UI 的可审计运行步骤，不暴露模型私有推理。"""
    return sse_frame(
        "run_step_start",
        {
            "id": step_id,
            "kind": kind,
            "name": name,
            "parent_id": parent_id,
            "status": "running",
            "summary": summary,
            "started_at": started_at,
        },
    )


def run_step_update(
    *, step_id: str, phase: str, detail: dict[str, Any] | None = None
) -> str:
    return sse_frame(
        "run_step_update",
        {"id": step_id, "phase": phase, "detail": detail or {}},
    )


def run_step_end(
    *,
    step_id: str,
    status: str,
    duration_ms: int,
    summary: str | None = None,
    error: str | None = None,
    token_usage: dict[str, int] | None = None,
    references: list[dict[str, Any]] | None = None,
) -> str:
    return sse_frame(
        "run_step_end",
        {
            "id": step_id,
            "status": status,
            "duration_ms": duration_ms,
            "summary": summary,
            "error": error,
            "token_usage": token_usage,
            "references": references or [],
        },
    )


def interrupt_requested(reason: str, state: dict[str, Any]) -> str:
    return sse_frame(
        "interrupt_requested",
        {"id": _new_id("it"), "reason": reason, "state": state},
    )


def message_end(token_usage: dict[str, int] | None = None) -> str:
    return sse_frame("message_end", {"id": _new_id("msg"), "token_usage": token_usage})


def run_end(*, status: str, duration_ms: int, tool_calls: int) -> str:
    return sse_frame(
        "run_end",
        {"status": status, "duration_ms": duration_ms, "tool_calls": tool_calls},
    )


def error(code: int, message: str) -> str:
    return sse_frame("error", {"code": code, "message": message})
