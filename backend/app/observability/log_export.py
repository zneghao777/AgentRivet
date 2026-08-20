"""结构化日志导出（OBSERVABILITY_SPEC §8.1）。

将应用 logging 记录转发到 `observability_logs`：
- 自动注入当前 `TraceContext`（trace_id / span_id / request_id / session_id / tenant_id / user_id）；
- 与 span 共用批量写队列，记录类型为 log；
- 默认只转发 WARN 及以上，避免高频 DEBUG 写放大；可配置 `OBS_LOG_MIN_LEVEL`。
"""

from __future__ import annotations

import logging
import traceback
from datetime import UTC, datetime

from . import tracing

logger = logging.getLogger(__name__)

# 转发的最低日志级别（数字）
_DEFAULT_MIN_LEVEL = logging.WARNING

# 避免递归：本模块自己打日志时不再转发
_transfer_active = False


class ObservabilityLogHandler(logging.Handler):
    """把标准 logging 记录写入 observability_logs 的 handler。"""

    def __init__(self, min_level: int = _DEFAULT_MIN_LEVEL) -> None:
        super().__init__(level=min_level)
        self._min_level = min_level

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.levelno < self._min_level:
                return
            # 过滤采集器自身日志，避免写失败时回环（§8.1 可靠性）
            if record.name.startswith("app.observability.tracing"):
                return
            _export_record(record)
        except Exception:  # noqa: BLE001
            # 日志转发失败绝不能影响业务
            return


def _severity_from_level(levelno: int) -> str:
    if levelno >= logging.CRITICAL:
        return "FATAL"
    if levelno >= logging.ERROR:
        return "ERROR"
    if levelno >= logging.WARNING:
        return "WARN"
    if levelno >= logging.INFO:
        return "INFO"
    return "DEBUG"


def _severity_number(levelno: int) -> int:
    # OTel severity number: DEBUG=5 INFO=9 WARN=13 ERROR=17 FATAL=21
    return {10: 5, 20: 9, 30: 13, 40: 17, 50: 21}.get(levelno, 9)


def _export_record(record: logging.LogRecord) -> None:
    global _transfer_active
    if _transfer_active:
        return
    _transfer_active = True
    try:
        ctx = tracing.get_trace_context()
        snapshot = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC),
            "observed_at": datetime.now(UTC),
            "severity_text": _severity_from_level(record.levelno),
            "severity_number": _severity_number(record.levelno),
            "body": record.getMessage(),
            "trace_id": ctx.trace_id if ctx else None,
            "span_id": ctx.current_span_id if ctx else None,
            "request_id": ctx.request_id if ctx else None,
            "session_id": ctx.session_id if ctx else None,
            "tenant_id": ctx.tenant_id if ctx else None,
            "user_id": ctx.user_id if ctx else None,
            "logger_name": record.name,
            "module": record.module,
            "function": record.funcName or None,
            "service_name": getattr(record, "service_name", None),
            "instance_id": getattr(record, "instance_id", None),
            "environment": getattr(record, "environment", None),
            "exception_type": None,
            "exception_message": None,
            "stacktrace": None,
            "attributes": {},
        }
        if record.exc_info and record.exc_info[1] is not None:
            exc_type, exc_value, exc_tb = record.exc_info
            snapshot["exception_type"] = exc_type.__name__ if exc_type else None
            snapshot["exception_message"] = str(exc_value)
            snapshot["stacktrace"] = "".join(traceback.format_exception(*record.exc_info))
        # 附加自定义字段（record.__dict__ 中的非标准属性）
        extra = {
            k: v
            for k, v in record.__dict__.items()
            if k
            not in {
                "name",
                "msg",
                "args",
                "levelname",
                "levelno",
                "pathname",
                "filename",
                "module",
                "exc_info",
                "exc_text",
                "stack_info",
                "lineno",
                "funcName",
                "created",
                "msecs",
                "relativeCreated",
                "thread",
                "threadName",
                "processName",
                "process",
                "message",
                "taskName",
                "service_name",
                "instance_id",
                "environment",
            }
            and not k.startswith("_")
        }
        if extra:
            snapshot["attributes"] = {str(k): str(v) for k, v in extra.items()}
        tracing.enqueue_log_nowait(snapshot)
    finally:
        _transfer_active = False


_installed = False


def install_log_handler() -> None:
    """把 handler 挂到根 logger（幂等）。"""
    global _installed
    if _installed:
        return
    try:
        from ..config import settings

        min_level = logging.WARNING
        if hasattr(settings, "OBS_LOG_MIN_LEVEL"):
            min_level = getattr(logging, settings.OBS_LOG_MIN_LEVEL, logging.WARNING)
    except Exception:  # noqa: BLE001
        min_level = logging.WARNING
    handler = ObservabilityLogHandler(min_level=min_level)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger().addHandler(handler)
    _installed = True
