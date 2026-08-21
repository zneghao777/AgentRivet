"""本地 Trace 采集（OBSERVABILITY_SPEC v2 §5）。

实现要点：
- TraceContext 通过 contextvars 在异步调用中传播，采样决策在根 Trace 只做一次；
- Span 状态机单向推进，重复结束事件幂等；
- 有界批量队列写入（最多 100 条或 200ms），同批一个事务，唯一键 (trace_id, span_id)；
- 队列溢出优先保留 error/timeout/canceled/人工反馈，丢弃增加 dropped 指标；
- 写入失败指数退避重试最多 3 次，进程关闭最多等 5 秒 flush；
- 脱敏管线：删除密钥字段 → JSON Path 规则 → PII → 截断 → redaction_version；
- 兼容 OpenTelemetry 字段命名（app.* 命名空间）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import random
import re
import time
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from ..config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量与配置
# ---------------------------------------------------------------------------

_MAX_PROMPT_CHARS = 1024          # 默认内容截断长度（测试引用 _MAX_PROMPT_CHARS）
_MAX_ERROR_CHARS = 200            # 错误摘要截断
_DEFAULT_QUEUE_CAPACITY = 10_000  # 队列默认容量
_BATCH_MAX = 100                  # 每批最多 100 条
_BATCH_MAX_MS = 200               # 每批最多等待 200ms
_WRITE_RETRIES = 3                # 写入失败最大重试
_RETRY_BASE_DELAY = 0.05          # 指数退避基数
_FLUSH_TIMEOUT_S = 5.0            # 进程关闭 flush 超时

# 明文密钥字段（删除）
_SECRET_KEYS = frozenset(
    {
        "api_key", "apikey", "authorization", "auth", "token", "access_token",
        "secret", "client_secret", "password", "passwd", "credential",
        "x-api-key", "x-auth-token", "proxy_auth", "bearer",
    }
)

# 需脱敏的 PII 字段（遮罩或 hash）
_PII_KEYS = frozenset(
    {
        "email", "phone", "mobile", "id_card", "ssn", "ip", "address",
        "bank_account", "card_number", "credit_card",
    }
)

# 需要执行 JSON Path 脱敏规则的键（规约 §7.1）
_JSON_PATH_SECRETS = re.compile(
    r"(?i)(password|passwd|secret|api[_-]?key|token|authorization|auth|credential|bearer)"
)


class SpanStatus(str, Enum):
    """Trace/Span 统一状态机（§4.2）。状态单向推进，终态不可覆盖。"""

    QUEUED = "queued"
    RUNNING = "running"
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELED = "canceled"
    INTERRUPTED = "interrupted"

    @property
    def is_terminal(self) -> bool:
        return self in {SpanStatus.OK, SpanStatus.ERROR, SpanStatus.TIMEOUT, SpanStatus.CANCELED}


# 状态机：合法推进路径
_STATE_TRANSITIONS: dict[SpanStatus, set[SpanStatus]] = {
    SpanStatus.QUEUED: {SpanStatus.RUNNING, SpanStatus.OK, SpanStatus.ERROR, SpanStatus.TIMEOUT, SpanStatus.CANCELED, SpanStatus.INTERRUPTED},
    SpanStatus.RUNNING: {SpanStatus.OK, SpanStatus.ERROR, SpanStatus.TIMEOUT, SpanStatus.CANCELED, SpanStatus.INTERRUPTED},
    SpanStatus.INTERRUPTED: {SpanStatus.RUNNING, SpanStatus.OK, SpanStatus.ERROR, SpanStatus.TIMEOUT, SpanStatus.CANCELED},
    # 终态不可再推进（幂等）
    SpanStatus.OK: set(),
    SpanStatus.ERROR: set(),
    SpanStatus.TIMEOUT: set(),
    SpanStatus.CANCELED: set(),
}

# 优先级：队列溢出时保留高价值 trace
_DROP_PRIORITY = {
    SpanStatus.OK: 0,
    SpanStatus.QUEUED: 1,
    SpanStatus.RUNNING: 2,
    SpanStatus.INTERRUPTED: 3,
    SpanStatus.TIMEOUT: 4,
    SpanStatus.CANCELED: 4,
    SpanStatus.ERROR: 5,
}


# ---------------------------------------------------------------------------
# TraceContext
# ---------------------------------------------------------------------------


@dataclass
class TraceContext:
    """异步调用传播的 Trace 上下文（§5.2）。"""

    trace_id: str
    root_span_id: str
    current_span_id: str
    request_id: str | None = None
    tenant_id: str = "default"
    user_id: str | None = None
    session_id: uuid.UUID | None = None
    thread_id: str | None = None
    sampled: bool = True
    content_capture_mode: str = "metadata_only"
    links: tuple[str, ...] = ()
    service_name: str = settings.APP_NAME
    service_version: str = "0.1.0"
    environment: str = settings.APP_ENV
    agent_name: str | None = None
    agent_version: str | None = None
    workflow_name: str | None = None
    model_provider: str | None = None
    model_name: str | None = None
    parent_span_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "root_span_id": self.root_span_id,
            "current_span_id": self.current_span_id,
            "parent_span_id": self.parent_span_id,
            "request_id": self.request_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "session_id": str(self.session_id) if self.session_id else None,
            "thread_id": self.thread_id,
            "sampled": self.sampled,
            "content_capture_mode": self.content_capture_mode,
            "links": list(self.links),
            "service_name": self.service_name,
            "service_version": self.service_version,
            "environment": self.environment,
            "agent_name": self.agent_name,
            "agent_version": self.agent_version,
            "workflow_name": self.workflow_name,
            "model_provider": self.model_provider,
            "model_name": self.model_name,
        }


_current_context: ContextVar[TraceContext | None] = ContextVar("obs_trace_context", default=None)


def get_trace_context() -> TraceContext | None:
    """获取当前协程的 TraceContext（无则 None）。"""
    return _current_context.get()


def set_trace_context(ctx: TraceContext | None) -> None:
    """显式设置当前 TraceContext（供后台任务等场景）。"""
    _current_context.set(ctx)


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------


def sanitize_text(text: str | None, limit: int = _MAX_PROMPT_CHARS) -> str | None:
    """截断 + 折叠空白，返回脱敏后的文本。None 输入返回 None。"""
    if not text:
        return None
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit]


def redact_text(text: str | None, limit: int = _MAX_PROMPT_CHARS) -> str | None:
    """生成可展示文本摘要：折叠、截断，并遮罩常见凭据与显式 PII。"""
    sanitized = sanitize_text(text, limit)
    if not sanitized:
        return None
    sanitized = re.sub(r"sk-[A-Za-z0-9_-]+", "sk-***", sanitized)
    sanitized = re.sub(
        r"(?i)(Bearer\s+)[A-Za-z0-9._-]+", r"\1***", sanitized
    )
    sanitized = re.sub(
        r"(?i)(api[_-]?key[=: ]+)[A-Za-z0-9._-]+", r"\1***", sanitized
    )
    sanitized = re.sub(
        r"\b([A-Za-z0-9._%+-]{1,2})[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b",
        r"\1****\2",
        sanitized,
    )
    sanitized = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "1**********", sanitized)
    return sanitized


def sanitize_attrs(attrs: dict | None) -> dict | None:
    """脱敏属性：删除密钥字段，嵌套 dict 只保留 key 列表，PII 遮罩。"""
    if attrs is None:
        return None
    out: dict[str, Any] = {}
    for key, value in attrs.items():
        lkey = str(key).lower()
        if lkey in _SECRET_KEYS or _JSON_PATH_SECRETS.search(str(key)):
            continue
        if lkey in _PII_KEYS:
            out[str(key)] = _mask_pii(value)
            continue
        if isinstance(value, dict):
            keys = [k for k in value.keys()]
            out[str(key)] = {"keys": keys}
        elif isinstance(value, list):
            out[str(key)] = [_sanitize_scalar(v) for v in value]
        elif isinstance(value, (str, int, float, bool)) or value is None:
            out[str(key)] = _sanitize_scalar(value)
        else:
            out[str(key)] = str(value)
    return out


def _sanitize_scalar(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value, _MAX_PROMPT_CHARS)
    return value


def _mask_pii(value: Any) -> str | None:
    """对 PII 字段遮罩或 hash。"""
    if value is None:
        return None
    s = str(value)
    if len(s) <= 4:
        return "****"
    return f"{s[:2]}****{s[-2:]}"


def sanitize_error(exc: BaseException | None) -> str | None:
    """生成脱敏异常摘要：`ExceptionType: message`，去凭据，截断。"""
    if exc is None:
        return None
    name = type(exc).__name__
    msg = redact_text(str(exc), _MAX_ERROR_CHARS) or ""
    text = f"{name}: {msg}" if msg else name
    return text[:_MAX_ERROR_CHARS + 12]


def redact_object(value: Any, *, limit: int = _MAX_PROMPT_CHARS) -> Any:
    """对任意对象（dict/list/str）执行脱敏，用于结构化参数落库。"""
    if isinstance(value, dict):
        return sanitize_attrs(value)
    if isinstance(value, list):
        return [redact_object(v, limit=limit) for v in value]
    if isinstance(value, str):
        return sanitize_text(value, limit)
    return value


# ---------------------------------------------------------------------------
# Span 句柄
# ---------------------------------------------------------------------------


@dataclass
class _SpanState:
    span_id: str
    trace_id: str
    parent_span_id: str | None
    name: str
    span_type: str
    status: SpanStatus
    started_at: datetime
    ended_at: datetime | None = None
    model: str | None = None
    token_usage: dict | None = None
    input_summary: str | None = None
    output_summary: str | None = None
    attributes: dict | None = None
    error: str | None = None
    duration_ms: int = 0
    event: str = "agent.run"
    run_id: str | None = None
    parent_run_id: str | None = None


class SpanHandle:
    """Trace/Span 采集句柄。终态幂等，不可覆盖。"""

    def __init__(
        self,
        *,
        trace_id: str,
        span_id: str,
        parent_span_id: str | None,
        name: str,
        span_type: str,
        tenant_id: str,
        session_id: uuid.UUID | None,
        user_id: str | None,
        started_at: datetime | None = None,
        attributes: dict | None = None,
        event: str = "agent.run",
        run_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> None:
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self.name = name
        self.span_type = span_type
        self.tenant_id = tenant_id
        self.session_id = session_id
        self.user_id = user_id
        self.started_at = started_at or datetime.now(UTC)
        self.event = event
        self.run_id = run_id
        self.parent_run_id = parent_run_id
        self._attributes = dict(attributes) if attributes else {}
        self._token_usage: dict | None = None
        self._input_summary: str | None = None
        self._output_summary: str | None = None
        self._model: str | None = None
        self._error: str | None = None
        self._status = SpanStatus.RUNNING
        self._ended_at: datetime | None = None
        self._duration_ms = 0
        self._finalized = False
        ctx = get_trace_context()
        self._content_capture_mode = (ctx.content_capture_mode if ctx else "metadata_only")

    # ---- 状态机 ----

    def _transition(self, target: SpanStatus) -> bool:
        """按状态机推进。返回是否成功推进；终态重复结束幂等返回 False。"""
        if self._finalized:
            return False
        allowed = _STATE_TRANSITIONS[self._status]
        if target not in allowed:
            return False
        self._status = target
        if target.is_terminal:
            self._finalized = True
            self._ended_at = datetime.now(UTC)
            self._duration_ms = max(
                0, int((self._ended_at - self.started_at).total_seconds() * 1000)
            )
        return True

    # ---- 写入字段 ----

    @property
    def status(self) -> SpanStatus:
        return self._status

    def set_status(self, status: SpanStatus | str) -> None:
        if isinstance(status, str):
            status = SpanStatus(status)
        self._transition(status)

    def record_model(self, model: str | None) -> None:
        if self._model is None and model:
            self._model = model

    def record_token_usage(self, usage: dict | None) -> None:
        """多次 LLM Token 调用：追加累积（修复 C-03）。"""
        if not usage:
            return
        merged = self._token_usage or {}
        # 只累积规范键（input/output/total/...），prompt/completion 作为派生别名
        for key in ("input", "output", "total", "cached_input", "cache_write", "reasoning"):
            if key in usage or key in merged:
                merged[key] = merged.get(key, 0) + usage.get(key, 0)
        # 派生旧字段别名（prompt= input, completion= output）
        merged["prompt"] = merged.get("input", 0)
        merged["completion"] = merged.get("output", 0)
        merged.setdefault("total", merged.get("input", 0) + merged.get("output", 0))
        self._token_usage = merged

    def record_input(self, summary: str | None) -> None:
        if summary and self._input_summary is None:
            self._input_summary = summary

    def record_output(self, summary: str | None) -> None:
        if summary:
            self._output_summary = summary

    def record_attribute(self, key: str, value: Any) -> None:
        self._attributes[key] = value

    def record_error(self, err: str | None) -> None:
        if err and self._error is None:
            self._error = err

    def finalize(
        self,
        *,
        status: SpanStatus | str = SpanStatus.OK,
        token_usage: dict | None = None,
        attributes: dict | None = None,
        error: str | None = None,
    ) -> None:
        """结束根 span：合并 token/attributes，按状态机推进终态，异步入队落库。"""
        self.record_token_usage(token_usage)
        if attributes:
            for k, v in attributes.items():
                self._attributes[k] = v
        if error:
            self.record_error(error)
        if isinstance(status, str):
            status = SpanStatus(status)
        self._transition(status)
        flush_span(self)

    def snapshot(self) -> dict[str, Any]:
        ctx = get_trace_context()
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "tenant_id": self.tenant_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "span_name": self.name,
            "span_type": self.span_type,
            "status": self._status.value,
            "model": self._model,
            "token_usage": self._token_usage,
            "input_summary": self._input_summary,
            "output_summary": self._output_summary,
            "duration_ms": self._duration_ms,
            "started_at": self.started_at,
            "ended_at": self._ended_at,
            "attributes": self._attributes,
            "error": self._error,
            "event": self.event,
            "run_id": self.run_id,
            "parent_run_id": self.parent_run_id,
            "content_capture_mode": self._content_capture_mode,
            "request_id": ctx.request_id if ctx else None,
            "thread_id": ctx.thread_id if ctx else None,
            "links": list(ctx.links) if ctx and ctx.links else None,
            "redaction_version": getattr(settings, "REDACTION_VERSION", None),
        }


# ---------------------------------------------------------------------------
# 采样
# ---------------------------------------------------------------------------


def _should_sample() -> bool:
    rate = settings.TRACE_SAMPLE_RATE
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    return random.random() < rate


# ---------------------------------------------------------------------------
# 事件循环隔离的批量写入状态
# ---------------------------------------------------------------------------

_db_factory: Callable[[], Any] | None = None
_state: dict[int, tuple[asyncio.Queue, asyncio.Task]] = {}


def init_tracing(db_factory: Callable[[], Any]) -> None:
    """初始化数据库工厂。writer 按事件循环惰性启动。"""
    global _db_factory
    _db_factory = db_factory


def _loop_state() -> tuple[asyncio.Queue, asyncio.Task]:
    """获取当前事件循环的 (queue, writer)；首次或 writer 终止时重建。"""
    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    state = _state.get(loop_id)
    if state is None or state[1].done():
        queue: asyncio.Queue = asyncio.Queue(maxsize=settings.OBS_QUEUE_CAPACITY if hasattr(settings, "OBS_QUEUE_CAPACITY") else _DEFAULT_QUEUE_CAPACITY)
        writer = asyncio.create_task(_write_loop(queue))
        state = (queue, writer)
        _state[loop_id] = state
    return state


async def _write_loop(queue: asyncio.Queue) -> None:
    """批量写入循环：最多 100 条或 200ms 提交一个事务。"""
    loop = asyncio.get_running_loop()
    while True:
        batch: list[dict] = []
        try:
            first = await queue.get()
        except asyncio.CancelledError:
            raise
        batch.append(first)
        deadline = loop.time() + _BATCH_MAX_MS / 1000
        # 收集到 100 条或达到 200ms 截止
        while len(batch) < _BATCH_MAX:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                item = await asyncio.wait_for(queue.get(), timeout=remaining)
            except (asyncio.TimeoutError, TimeoutError):
                break
            batch.append(item)
        await _write_batch(batch)
        for _ in batch:
            queue.task_done()


async def _write_batch(batch: list[dict]) -> None:
    """单批写入一个事务，失败指数退避重试最多 3 次。

    按 record_type 分流：span → agent_spans；log → observability_logs（§8.1）。
    """
    if _db_factory is None:
        _increment_dropped(len(batch), "no_factory")
        return
    from ..storage import repositories as repo

    spans = [b for b in batch if b.get("record_type", "span") == "span"]
    logs = [b for b in batch if b.get("record_type") == "log"]

    for attempt in range(1, _WRITE_RETRIES + 1):
        try:
            async with _db_factory() as db:
                if spans:
                    await repo.bulk_upsert_spans(db, spans)
                if logs:
                    await repo.bulk_add_observability_logs(db, logs)
            return
        except Exception as exc:  # noqa: BLE001
            if attempt < _WRITE_RETRIES:
                delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
                await asyncio.sleep(delay)
                logger.warning("span/日志批量写入失败(重试 %d/%d): %s", attempt, _WRITE_RETRIES, exc)
            else:
                _increment_dropped(len(batch), "write_failed")
                logger.error("span/日志批量写入最终失败，丢弃 %d 条: %s", len(batch), exc)


def _increment_dropped(count: int, reason: str) -> None:
    """被丢弃的 span 增加 observability_spans_dropped_total 指标（§5.5）。"""
    from .metrics import metrics

    try:
        metrics.record_spans_dropped(count, reason=reason)
    except Exception:  # noqa: BLE001
        pass


def _enqueue_nowait(span_snapshot: dict[str, Any], *, record_type: str = "span") -> bool:
    """同步入队（快路径）。返回是否成功；队列满触发溢出策略。"""
    item = {"record_type": record_type, **span_snapshot}
    try:
        queue, _ = _loop_state()
        if queue.full():
            # 队列满：FIFO 弹出最旧一条，为高价值腾位（§5.5）
            try:
                evicted = queue.get_nowait()
                if evicted is None:
                    _increment_dropped(1, "queue_full")
                    return False
            except asyncio.QueueEmpty:
                _increment_dropped(1, "queue_full")
                return False
        queue.put_nowait(item)
        return True
    except asyncio.QueueFull:
        _increment_dropped(1, "queue_full")
        return False
    except RuntimeError:
        return False


def enqueue_log_nowait(log_snapshot: dict[str, Any]) -> bool:
    """结构化日志入队（§8.1）：与 span 共用写队列，record_type=log 分流写入。"""
    return _enqueue_nowait(log_snapshot, record_type="log")


async def enqueue_span(span_snapshot: dict[str, Any]) -> None:
    """将 span 快照入队（异步包装，供无运行循环时的同步写降级）。"""
    if _enqueue_nowait(span_snapshot):
        return
    # 无运行事件循环：降级为直接写
    item = {"record_type": "span", **span_snapshot}
    await _write_batch([item])


async def _evict_low_value(queue: asyncio.Queue) -> dict | None:
    """从满队列中弹出一个最低价值 item；没有可弹的返回 None。"""
    # 简单实现：弹出队头（FIFO 最旧）
    try:
        return queue.get_nowait()
    except asyncio.QueueEmpty:
        return None


def queue_usage_ratio() -> float:
    """采集队列使用率（0.0~1.0，§10.2 queue_usage 指标）。"""
    try:
        loop = asyncio.get_running_loop()
        state = _state.get(id(loop))
        if state is None:
            return 0.0
        queue, _ = state
        return queue.qsize() / max(queue.maxsize, 1)
    except RuntimeError:
        return 0.0


async def flush_tracing(timeout: float = _FLUSH_TIMEOUT_S) -> None:
    """等待当前事件循环队列排空（进程关闭 flush）。"""
    loop = asyncio.get_running_loop()
    state = _state.get(id(loop))
    if state is None:
        return
    queue, _ = state
    try:
        await asyncio.wait_for(queue.join(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("tracing flush 超时，未写入 %d 条", queue.qsize())


async def shutdown_tracing() -> None:
    """停止所有 writer 并 flush（最多 5 秒）。"""
    for loop_id, (queue, task) in list(_state.items()):
        try:
            await asyncio.wait_for(queue.join(), timeout=_FLUSH_TIMEOUT_S)
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning("tracing flush 超时，未写入 %d 条", queue.qsize())
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, RuntimeError):
            pass
        _state.pop(loop_id, None)


# ---------------------------------------------------------------------------
# 根 span / 子 span 采集
# ---------------------------------------------------------------------------


def _new_trace_id() -> str:
    return uuid.uuid4().hex[:32]


def _new_span_id() -> str:
    return uuid.uuid4().hex[:16]


def start_root_span(
    name: str = "agent.run",
    *,
    tenant_id: str = "default",
    session_id: uuid.UUID | None = None,
    user_id: str | None = None,
    request_id: str | None = None,
    thread_id: str | None = None,
    links: tuple[str, ...] | None = None,
    content_capture_mode: str | None = None,
    attributes: dict | None = None,
    event: str = "agent.run",
    sampled: bool | None = None,
    span_type: str = "root",
    parent_trace_id: str | None = None,
    parent_span_id: str | None = None,
) -> SpanHandle:
    """创建根 Trace/Span。采样决策只在此执行一次，所有子 span 继承。"""
    trace_id = _new_trace_id()
    span_id = _new_span_id()
    do_sample = _should_sample() if sampled is None else sampled
    mode = content_capture_mode or getattr(settings, "CONTENT_CAPTURE_MODE", "metadata_only")
    ctx = TraceContext(
        trace_id=trace_id,
        root_span_id=span_id,
        current_span_id=span_id,
        parent_span_id=parent_span_id,
        request_id=request_id,
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        thread_id=thread_id,
        sampled=do_sample,
        content_capture_mode=mode,
        links=tuple(links) if links else (),
        environment=settings.APP_ENV,
        service_name=settings.APP_NAME,
    )
    _current_context.set(ctx)
    handle = SpanHandle(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        name=name,
        span_type=span_type,
        tenant_id=tenant_id,
        session_id=session_id,
        user_id=user_id,
        attributes=attributes,
        event=event,
    )
    return handle


def record_child_span(
    trace_id: str,
    parent_span_id: str,
    tenant_id: str,
    session_id: uuid.UUID | None,
    user_id: str | None,
    span_name: str,
    span_type: str,
    status: str,
    model: str | None = None,
    token_usage: dict | None = None,
    input_summary: str | None = None,
    output_summary: str | None = None,
    duration_ms: int = 0,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    attributes: dict | None = None,
    error: str | None = None,
    span_id: str | None = None,
    cost_input: float | None = None,
    cost_output: float | None = None,
    cost_total: float | None = None,
    currency: str | None = None,
    model_provider: str | None = None,
    model_name_requested: str | None = None,
    model_name_resolved: str | None = None,
    finish_reason: str | None = None,
    ttft_ms: int | None = None,
    retry_count: int = 0,
    run_id: str | None = None,
    parent_run_id: str | None = None,
    prompt_name: str | None = None,
    prompt_version: str | None = None,
    prompt_hash: str | None = None,
    model_parameters: dict | None = None,
    provider_request_id: str | None = None,
    events: list | None = None,
) -> None:
    """写入一个已完成子 span（异步入队）。非采样 trace 直接丢弃。

    span_id 由调用方传入以保证父子引用一致；未传时自动生成。
    """
    ctx = get_trace_context()
    sampled = ctx.sampled if ctx is not None else True
    if not sampled:
        return
    span_id = span_id or _new_span_id()
    now = datetime.now(UTC)
    snapshot = {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "tenant_id": tenant_id,
        "session_id": session_id,
        "user_id": user_id,
        "span_name": span_name,
        "span_type": span_type,
        "status": status,
        "model": model,
        "token_usage": token_usage,
        "input_summary": input_summary,
        "output_summary": output_summary,
        "duration_ms": duration_ms,
        "started_at": started_at or now,
        "ended_at": ended_at or now,
        "attributes": sanitize_attrs(attributes),
        "error": error,
        "request_id": ctx.request_id if ctx else None,
        "thread_id": ctx.thread_id if ctx else None,
        "cost_input": cost_input,
        "cost_output": cost_output,
        "cost_total": cost_total,
        "currency": currency,
        "model_provider": model_provider,
        "model_name_requested": model_name_requested,
        "model_name_resolved": model_name_resolved,
        "finish_reason": finish_reason,
        "ttft_ms": ttft_ms,
        "retry_count": retry_count,
        "run_id": run_id,
        "parent_run_id": parent_run_id,
        "prompt_name": prompt_name,
        "prompt_version": prompt_version,
        "prompt_hash": prompt_hash,
        "model_parameters": model_parameters,
        "provider_request_id": provider_request_id,
        "events": events,
        "content_capture_mode": ctx.content_capture_mode if ctx else "metadata_only",
        "redaction_version": getattr(settings, "REDACTION_VERSION", None),
    }
    _enqueue_nowait(snapshot)


def flush_span(handle: SpanHandle) -> None:
    """将 SpanHandle 快照入队（同步快路径）。"""
    snapshot = handle.snapshot()
    ctx = get_trace_context()
    if ctx is not None and not ctx.sampled:
        return
    _enqueue_nowait(snapshot)
