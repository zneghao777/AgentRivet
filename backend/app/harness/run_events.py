"""ChatRun SSE 帧解析、持久化和订阅唤醒。"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from ..storage import chat_runs
from ..storage.db import AsyncSessionLocal

_SENSITIVE_KEY = re.compile(r"(?i)(api[_-]?key|authorization|password|secret|token)")
_CREDENTIAL_PATTERNS = (
    (re.compile(r"sk-[A-Za-z0-9_-]+"), "sk-***"),
    (re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._-]+"), r"\1***"),
    (re.compile(r"(?i)(api[_-]?key[=: ]+)[A-Za-z0-9._-]+"), r"\1***"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "1**********"),
)


def _redact_client_payload(value: Any, key: str = "") -> Any:
    if key and _SENSITIVE_KEY.search(key):
        return "***"
    if isinstance(value, dict):
        return {
            str(child_key): _redact_client_payload(child_value, str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_client_payload(item) for item in value]
    if isinstance(value, str):
        redacted = value
        for pattern, replacement in _CREDENTIAL_PATTERNS:
            redacted = pattern.sub(replacement, redacted)
        return redacted[:100_000]
    return value


@dataclass(frozen=True)
class ParsedSseFrame:
    event_type: str
    payload: dict[str, Any]


def parse_sse_frame(frame: str) -> ParsedSseFrame | None:
    event_type = ""
    data_lines: list[str] = []
    for raw_line in frame.splitlines():
        line = raw_line.rstrip("\r")
        if line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
    if not event_type or not data_lines:
        return None
    try:
        payload = json.loads("\n".join(data_lines))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        payload = {"value": payload}
    return ParsedSseFrame(event_type=event_type, payload=payload)


class RunEventNotifier:
    """本地 Subscriber 唤醒器；事实读取始终回到 PostgreSQL。"""

    def __init__(self) -> None:
        self._conditions: dict[uuid.UUID, asyncio.Condition] = defaultdict(
            asyncio.Condition
        )

    async def publish(self, run_id: uuid.UUID) -> None:
        condition = self._conditions[run_id]
        async with condition:
            condition.notify_all()

    async def wait(self, run_id: uuid.UUID, timeout: float) -> None:
        condition = self._conditions[run_id]
        async with condition:
            try:
                await asyncio.wait_for(condition.wait(), timeout=max(0.1, timeout))
            except TimeoutError:
                return

    def forget(self, run_id: uuid.UUID) -> None:
        self._conditions.pop(run_id, None)


run_event_notifier = RunEventNotifier()


class ChatRunEventSink:
    """把 GraphRuntime 的可见 SSE 帧转为严格递增的持久化事件。"""

    def __init__(self, run_id: uuid.UUID) -> None:
        self.run_id = run_id

    async def emit(self, event_type: str, payload: dict[str, Any]) -> int:
        safe_payload = _redact_client_payload(payload)
        async with AsyncSessionLocal() as db:
            event = await chat_runs.append_event(
                db,
                run_id=self.run_id,
                event_type=event_type,
                payload=safe_payload,
            )
        await run_event_notifier.publish(self.run_id)
        return event.sequence

    async def consume_frame(self, frame: str) -> ParsedSseFrame | None:
        parsed = parse_sse_frame(frame)
        if parsed is None:
            return None
        # run_end 由终态提交单元统一生成，避免出现“事件已完成、消息未落库”。
        if parsed.event_type != "run_end":
            await self.emit(parsed.event_type, parsed.payload)
        return parsed


def encode_sse_event(event_type: str, payload: dict[str, Any], sequence: int) -> str:
    return (
        f"id: {sequence}\n"
        f"event: {event_type}\n"
        f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )
