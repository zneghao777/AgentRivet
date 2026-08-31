"""Bounded conversation projection for multi-Agent routing and execution."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

ORCHESTRATION_CONTEXT_MAX_MESSAGES = 12
ORCHESTRATION_CONTEXT_MAX_CHARS = 6000
ORCHESTRATION_CONTEXT_MESSAGE_MAX_CHARS = 1200


def project_conversation_context(
    messages: list[Any],
    *,
    exclude_latest_user: bool = True,
    max_messages: int = ORCHESTRATION_CONTEXT_MAX_MESSAGES,
    max_chars: int = ORCHESTRATION_CONTEXT_MAX_CHARS,
) -> str:
    """Render recent history without copying an unbounded checkpoint into every Agent."""

    if max_messages <= 0 or max_chars <= 0:
        return ""
    latest_user_index = _latest_user_index(messages) if exclude_latest_user else None
    entries: list[str] = []
    for index, message in enumerate(messages):
        if index == latest_user_index or isinstance(message, SystemMessage):
            continue
        rendered = _render_message(message)
        if rendered:
            entries.append(rendered)
    entries = entries[-max_messages:]

    selected: list[str] = []
    remaining = max_chars
    for entry in reversed(entries):
        if remaining <= 0:
            break
        clipped = entry[:remaining].rstrip()
        if clipped:
            selected.append(clipped)
            remaining -= len(clipped) + 1
    return "\n".join(reversed(selected))


def format_contextual_request(message: str, conversation_context: str) -> str:
    """Keep the current request explicit while providing bounded prior context."""

    context = conversation_context.strip() or "（无）"
    return (
        "# 最近会话上下文（从旧到新）\n"
        f"{context}\n\n"
        "# 当前用户请求\n"
        f"{message.strip()}"
    )


def _latest_user_index(messages: list[Any]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, HumanMessage):
            return index
        if isinstance(message, tuple) and message and message[0] in {"user", "human"}:
            return index
    return None


def _render_message(message: Any) -> str:
    if isinstance(message, HumanMessage):
        role = "用户"
    elif isinstance(message, AIMessage):
        role = "助手"
    elif isinstance(message, ToolMessage):
        role = f"工具 {message.name or 'unknown'}"
    elif isinstance(message, tuple) and len(message) >= 2:
        role = {"user": "用户", "human": "用户", "assistant": "助手", "ai": "助手"}.get(
            str(message[0]), str(message[0])
        )
        return _line(role, message[1])
    else:
        return ""

    content = _content_text(getattr(message, "content", ""))
    if isinstance(message, AIMessage) and message.tool_calls:
        calls = ", ".join(
            f"{call.get('name', 'unknown')}("
            f"{json.dumps(call.get('args') or {}, ensure_ascii=False, default=str)})"
            for call in message.tool_calls
        )
        content = "；".join(part for part in (content, f"请求调用工具：{calls}") if part)
    return _line(role, content)


def _line(role: str, content: Any) -> str:
    text = " ".join(_content_text(content).split())
    if not text:
        return ""
    return f"- {role}：{text[:ORCHESTRATION_CONTEXT_MESSAGE_MAX_CHARS]}"


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content) if content is not None else ""
