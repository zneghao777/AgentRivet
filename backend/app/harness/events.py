"""SSE 事件构造与序列化。

统一事件封装：每行一个 data: JSON，事件类型由 event: 标识。
"""

from __future__ import annotations

import json
import time
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


def message_start() -> str:
    return sse_frame("message_start", {"id": _new_id("msg"), "role": "assistant"})


def message_delta(msg_id: str, content: str) -> str:
    return sse_frame("message_delta", {"id": msg_id, "content": content})


def tool_start(name: str, arguments: dict[str, Any]) -> str:
    return sse_frame(
        "tool_start", {"id": _new_id("tl"), "name": name, "arguments": arguments}
    )


def tool_end(name: str, result: str) -> str:
    return sse_frame("tool_end", {"id": _new_id("tl"), "name": name, "result": result})


def node_event(kind: str, name: str) -> str:
    return sse_frame(kind, {"name": name})


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
