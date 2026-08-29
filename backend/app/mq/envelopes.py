"""MQ 统一消息信封与 TraceContext 透传。"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from ..observability.tracing import TraceContext, get_trace_context

MESSAGE_TYPES = frozenset(
    {"obs.span", "obs.log", "obs.audit", "session.postprocess"}
)


class EnvelopeError(ValueError):
    """消息信封格式无效。"""


@dataclass(frozen=True)
class MessageEnvelope:
    message_id: str
    type: str
    version: int
    created_at: str
    retry_count: int
    trace: dict[str, Any]
    payload: dict[str, Any]

    def to_bytes(self) -> bytes:
        return json.dumps(
            asdict(self), ensure_ascii=False, separators=(",", ":"), default=_json_default
        ).encode("utf-8")


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    raise TypeError(f"不支持的 MQ 字段类型: {type(value).__name__}")


def trace_snapshot(ctx: TraceContext | None = None) -> dict[str, Any]:
    """提取可跨进程传播的最小 Trace 上下文。"""
    ctx = ctx or get_trace_context()
    if ctx is None:
        return {
            "trace_id": None,
            "span_id": None,
            "request_id": None,
            "tenant_id": "default",
            "user_id": None,
            "session_id": None,
            "thread_id": None,
        }
    return {
        "trace_id": ctx.trace_id,
        "span_id": ctx.current_span_id,
        "request_id": ctx.request_id,
        "tenant_id": ctx.tenant_id,
        "user_id": ctx.user_id,
        "session_id": str(ctx.session_id) if ctx.session_id else None,
        "thread_id": ctx.thread_id,
    }


def make_envelope(
    message_type: str,
    payload: dict[str, Any],
    *,
    retry_count: int = 0,
    message_id: str | None = None,
    trace: dict[str, Any] | None = None,
) -> MessageEnvelope:
    if message_type not in MESSAGE_TYPES:
        raise EnvelopeError(f"不支持的消息类型: {message_type}")
    return MessageEnvelope(
        message_id=message_id or str(uuid.uuid4()),
        type=message_type,
        version=1,
        created_at=datetime.now(UTC).isoformat(),
        retry_count=max(0, retry_count),
        trace=trace or trace_snapshot(),
        payload=payload,
    )


def decode_envelope(body: bytes) -> MessageEnvelope:
    try:
        raw = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnvelopeError("消息不是有效 UTF-8 JSON") from exc
    if not isinstance(raw, dict):
        raise EnvelopeError("消息信封必须是 JSON object")
    required = {"message_id", "type", "version", "created_at", "retry_count", "trace", "payload"}
    missing = required - raw.keys()
    if missing:
        raise EnvelopeError(f"消息信封缺少字段: {', '.join(sorted(missing))}")
    if raw["type"] not in MESSAGE_TYPES or raw["version"] != 1:
        raise EnvelopeError("消息类型或版本不受支持")
    if not isinstance(raw["payload"], dict) or not isinstance(raw["trace"], dict):
        raise EnvelopeError("trace 和 payload 必须是 JSON object")
    try:
        uuid.UUID(str(raw["message_id"]))
        retry_count = int(raw["retry_count"])
        datetime.fromisoformat(str(raw["created_at"]).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise EnvelopeError("message_id、created_at 或 retry_count 无效") from exc
    return MessageEnvelope(
        message_id=str(raw["message_id"]),
        type=str(raw["type"]),
        version=1,
        created_at=str(raw["created_at"]),
        retry_count=max(0, retry_count),
        trace=raw["trace"],
        payload=raw["payload"],
    )


def trace_context_from_envelope(envelope: MessageEnvelope) -> TraceContext | None:
    trace = envelope.trace
    trace_id = trace.get("trace_id")
    span_id = trace.get("span_id")
    if not trace_id or not span_id:
        return None
    session_id = trace.get("session_id")
    try:
        parsed_session_id = uuid.UUID(session_id) if session_id else None
    except (TypeError, ValueError):
        parsed_session_id = None
    return TraceContext(
        trace_id=str(trace_id),
        root_span_id=str(trace_id)[:16],
        current_span_id=str(span_id),
        parent_span_id=str(span_id),
        request_id=trace.get("request_id"),
        tenant_id=str(trace.get("tenant_id") or "default"),
        user_id=trace.get("user_id"),
        session_id=parsed_session_id,
        thread_id=trace.get("thread_id"),
    )


def restore_payload_types(payload: dict[str, Any]) -> dict[str, Any]:
    """按已知持久化字段还原 datetime 和 UUID。"""
    restored = dict(payload)
    for key in ("started_at", "ended_at", "timestamp", "observed_at"):
        value = restored.get(key)
        if isinstance(value, str):
            try:
                restored[key] = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                pass
    session_id = restored.get("session_id")
    if isinstance(session_id, str):
        try:
            restored["session_id"] = uuid.UUID(session_id)
        except ValueError:
            pass
    return restored
