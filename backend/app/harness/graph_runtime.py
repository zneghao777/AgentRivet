"""图运行封装：invoke / stream / interrupt 恢复，转换为 SSE 事件 + 观测采集。

观测（OBSERVABILITY_SPEC v2 §5.3）：
- 以 astream_events(version="v2") 的 run_id / parent_ids 为链路映射唯一事实来源；
- 维护 run_id → span_id 映射，按最近父 run_id 计算 parent_span_id，修复 C-02；
- 每次 LLM 调用独立记录 Token，根只存汇总，修复 C-03 / C-04 / C-05；
- 覆盖流式 / 非流式 ainvoke / HITL resume / 后台任务，修复 C-01；
- 采样决策在根 Trace 统一传播（C-09）；SSE 发送 trace_start（§12.4）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from langgraph.graph.state import CompiledStateGraph

from ..config import settings
from ..observability.costing import compute_cost as _compute_cost
from . import events as ev
from .context import get_tool_context

logger = logging.getLogger(__name__)

# SSE 帧类型：字符串
SseEvent = str


def _span_event(
    name: str,
    phase: str,
    *,
    timestamp: datetime | None = None,
    summary: str | None = None,
    status: str | None = None,
    details: dict | None = None,
) -> dict:
    """生成可持久化、可排序的标准 Span 事件。"""
    return {
        "name": name,
        "phase": phase,
        "timestamp": (timestamp or datetime.now(UTC)).isoformat(),
        "summary": summary,
        "status": status,
        "details": details or {},
    }


def _append_span_event(
    pending: dict,
    phase: str,
    *,
    timestamp: datetime | None = None,
    summary: str | None = None,
    status: str | None = None,
    details: dict | None = None,
) -> None:
    if settings.CONTENT_CAPTURE_MODE == "metadata_only":
        summary = None
    pending.setdefault("events", []).append(
        _span_event(
            pending.get("name", "span"),
            phase,
            timestamp=timestamp,
            summary=summary,
            status=status,
            details=details,
        )
    )


def _cost_for_pending(pending: dict) -> dict:
    """对 LLM/Embedding 子 span 用当前价目表计算成本（OBSERVABILITY_SPEC §6.4）。

    使用内置默认价目表即时计算；价格版本表由后台聚合任务按生效时间校准。
    """
    if pending.get("type") not in ("llm", "embedding"):
        return {}
    return _compute_cost(pending.get("model") or "", pending.get("token_usage"))


def _config(thread_id: str, tenant_id: str) -> dict:
    return {
        "configurable": {
            "thread_id": thread_id,
            "tenant_id": tenant_id,
        }
    }


class GraphRuntime:
    """封装图运行，对外提供统一接口。"""

    def __init__(self, app: CompiledStateGraph, checkpointer: Any | None = None) -> None:
        self.app = app
        self.checkpointer = checkpointer

    async def astream(
        self,
        thread_id: str,
        message: str,
        tenant_id: str,
        user_id: str,
        skill_names: list[str],
        session_id: str | None = None,
        memory_context: str = "",
        request_id: str | None = None,
        message_id: int | None = None,
        links: tuple[str, ...] | None = None,
    ) -> AsyncIterator[str]:
        """流式运行，内部将 LangGraph 事件转换为 SSE 事件。"""
        cfg = _config(thread_id, tenant_id)
        start = time.monotonic()

        sess_uuid = None
        if session_id:
            try:
                sess_uuid = uuid.UUID(session_id)
            except ValueError:
                sess_uuid = None
            yield ev.session_start(session_id, thread_id)

        from ..observability import tracing

        root_span = tracing.start_root_span(
            name="agent.run",
            tenant_id=tenant_id,
            session_id=sess_uuid,
            user_id=user_id,
            request_id=request_id,
            thread_id=thread_id,
            links=links,
            attributes={
                "thread_id": thread_id,
                "user_message": tracing.sanitize_text(message, 256),
                "skill_loaded": skill_names,
                "memory_injected": bool(memory_context),
            },
        )
        # SSE：Trace 创建后发送 trace_start（§12.4）
        yield ev.trace_start(
            trace_id=root_span.trace_id,
            request_id=request_id,
            session_id=session_id,
        )

        msg_id = ""
        tool_calls_count = 0
        # 根 span 只保存汇总 Token（由每次 LLM 累加）
        token_usage: dict[str, int] = {"input": 0, "output": 0, "total": 0}
        run_status = "ok"
        run_error: str | None = None
        _pending_reply: list[str] = []

        q: asyncio.Queue[Any] = asyncio.Queue()

        async def _run() -> None:
            try:
                async for event in self.app.astream_events(
                    {
                        "messages": [("user", message)],
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "skill_loaded": skill_names,
                        "tool_calls_total": 0,
                        "interrupt_count": 0,
                        "final_answer": "",
                        "memory_context": memory_context,
                        "session_id": session_id or "",
                        "message_id": message_id,
                        "request_id": request_id or "",
                    },
                    config=cfg,
                    version="v2",
                ):
                    await q.put(event)
            except asyncio.CancelledError:
                logger.info("流式运行被取消 thread=%s", thread_id)
            except Exception as exc:  # noqa: BLE001
                logger.exception("图运行异常 thread=%s", thread_id)
                await q.put({"type": "_error", "exc": exc})
            finally:
                await q.put(None)

        task = asyncio.create_task(_run())

        # run_id -> 进行中的 span 记录（含 span_id，供父子链路）
        pending_spans: dict[str, dict] = {}

        def _register_span(
            run_id: str,
            *,
            name: str,
            span_type: str,
            parent_span_id: str | None,
            input_summary: str | None = None,
            attributes: dict | None = None,
        ) -> tuple[dict, bool]:
            # 同一 run_id 只分配一次 span_id（避免重复 start 事件覆盖导致父子错乱）
            existing = pending_spans.get(run_id)
            if existing is not None:
                merged_attributes = existing.get("attributes") or {}
                merged_attributes.update(attributes or {})
                existing["attributes"] = merged_attributes
                if input_summary and not existing.get("input_summary"):
                    existing["input_summary"] = input_summary
                return existing, False
            pending_spans[run_id] = {
                "span_id": tracing._new_span_id(),
                "name": name,
                "type": span_type,
                "parent_span_id": parent_span_id,
                "started_at": datetime.now(UTC),
                "input_summary": input_summary,
                "attributes": attributes,
            }
            _append_span_event(
                pending_spans[run_id],
                "start",
                summary=input_summary,
            )
            return pending_spans[run_id], True

        def _resolve_parent(parent_ids: list[str]) -> str | None:
            """按最近父 run_id 计算 parent_span_id（修复 C-02）。"""
            for pid in reversed(parent_ids):
                if pid in pending_spans:
                    return pending_spans[pid]["span_id"]
            return root_span.span_id

        def _close_span(
            run_id: str, *, status: str = "ok", data: dict | None = None
        ) -> dict | None:
            """按 run_id 关闭一个进行中的子 span，并按真实父节点挂载。"""
            pending = pending_spans.pop(run_id, None)
            if not pending:
                return None
            ended = datetime.now(UTC)
            duration_ms = max(0, int((ended - pending["started_at"]).total_seconds() * 1000))
            _append_span_event(
                pending,
                "end",
                timestamp=ended,
                summary=pending.get("output_summary") or pending.get("error"),
                status=status,
                details={"duration_ms": duration_ms},
            )
            cost = _cost_for_pending(pending)
            tracing.record_child_span(
                trace_id=root_span.trace_id,
                parent_span_id=pending["parent_span_id"],
                tenant_id=tenant_id,
                session_id=sess_uuid,
                user_id=user_id,
                span_name=pending["name"],
                span_type=pending["type"],
                status=status,
                model=pending.get("model"),
                token_usage=pending.get("token_usage"),
                input_summary=pending.get("input_summary"),
                output_summary=pending.get("output_summary"),
                duration_ms=duration_ms,
                started_at=pending["started_at"],
                ended_at=ended,
                attributes=pending.get("attributes"),
                error=pending.get("error"),
                span_id=pending["span_id"],
                cost_input=cost.get("cost_input"),
                cost_output=cost.get("cost_output"),
                cost_total=cost.get("cost_total"),
                currency=cost.get("currency"),
                model_provider=pending.get("model_provider"),
                model_name_requested=pending.get("model_requested"),
                model_name_resolved=pending.get("model"),
                finish_reason=pending.get("finish_reason"),
                ttft_ms=pending.get("ttft_ms"),
                retry_count=pending.get("retry_count", 0),
                run_id=pending.get("run_id"),
                parent_run_id=pending.get("parent_run_id"),
                model_parameters=pending.get("model_parameters"),
                events=pending.get("events"),
            )
            pending["status"] = status
            pending["duration_ms"] = duration_ms
            return pending

        def _step_end_frame(pending: dict | None) -> str | None:
            if pending is None:
                return None
            return ev.run_step_end(
                step_id=pending["span_id"],
                status=pending.get("status", "ok"),
                duration_ms=pending.get("duration_ms", 0),
                summary=_public_step_summary(pending),
                error=pending.get("error"),
                token_usage=pending.get("token_usage"),
                references=pending.get("references"),
            )

        final_step_frames: list[str] = []
        final_answer = ""
        clarification_only = False
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=settings.AGENT_TIMEOUT_SECONDS)
                except TimeoutError:
                    run_status = "timeout"
                    run_error = "graph execution timeout"
                    # 关闭所有进行中的子 span 为 timeout（C-08）
                    for rid in list(pending_spans):
                        pending_spans[rid]["error"] = run_error
                        frame = _step_end_frame(_close_span(rid, status="timeout"))
                        if frame:
                            yield frame
                    yield ev.error(50001, run_error)
                    break
                if event is None:
                    break

                etype = event.get("event", "")
                data = event.get("data") or {}
                run_id = event.get("run_id") or ""
                parent_ids = event.get("parent_ids") or []
                if event.get("type") == "_error":
                    exc = event.get("exc")
                    run_status = "error"
                    run_error = tracing.sanitize_error(exc)
                    for rid in list(pending_spans):
                        pending_spans[rid]["error"] = run_error
                        frame = _step_end_frame(_close_span(rid, status="error"))
                        if frame:
                            yield frame
                    yield ev.error(50001, f"graph error: {exc}")
                    break

                if etype == "on_custom_event" and str(event.get("name", "")).startswith(
                    "orchestration."
                ):
                    orchestration_name = str(event.get("name"))
                    payload = data if isinstance(data, dict) else {"value": data}
                    for parent_run_id in reversed(parent_ids):
                        parent = pending_spans.get(parent_run_id)
                        if parent is not None:
                            attributes = parent.get("attributes") or {}
                            attributes[
                                orchestration_name.removeprefix("orchestration.")
                            ] = payload
                            parent["attributes"] = attributes
                            break
                    if orchestration_name == "orchestration.clarification":
                        clarification_only = True
                    if orchestration_name == "orchestration.error":
                        run_status = "error"
                        run_error = str(payload.get("message") or "orchestration failed")
                        yield ev.error(50003, run_error)
                    else:
                        yield ev.sse_frame(orchestration_name, payload)

                elif etype == "on_chat_model_stream":
                    chunk = data.get("chunk")
                    if chunk is not None and _is_public_stream_event(event):
                        text = _extract_text(chunk)
                        if text:
                            _pending_reply.append(text)
                            if not msg_id:
                                yield ev.message_start()
                                msg_id = "msg_stream"
                            yield ev.message_delta(msg_id, text[:64])
                        # TTFT：首个非空 chunk（§5.3）
                        if run_id in pending_spans and "ttft_recorded" not in pending_spans[run_id]:
                            pending_spans[run_id]["ttft_recorded"] = True
                            pending_spans[run_id]["ttft_ms"] = int(
                                (
                                    datetime.now(UTC) - pending_spans[run_id]["stream_started"]
                                ).total_seconds()
                                * 1000
                            )
                            _append_span_event(
                                pending_spans[run_id],
                                "first_token",
                                details={"ttft_ms": pending_spans[run_id]["ttft_ms"]},
                            )
                            yield ev.run_step_update(
                                step_id=pending_spans[run_id]["span_id"],
                                phase="first_token",
                                detail={"ttft_ms": pending_spans[run_id]["ttft_ms"]},
                            )

                elif etype == "on_chat_model_start":
                    # 记录每次 LLM 调用的独立 span（C-03/C-04/C-05）
                    model = (
                        data.get("model")
                        or get_tool_context().model_id
                        or settings.LLM_MODEL
                    )
                    parent_id = _resolve_parent(parent_ids)
                    input_summary = _summarize_input(data.get("input"))
                    pending, created = _register_span(
                        run_id,
                        name="llm.chat",
                        span_type="llm",
                        parent_span_id=parent_id,
                        input_summary=input_summary,
                        attributes={
                            "model_requested": model,
                            "model_resolved": model,
                            "streaming": True,
                            "prompt_input": tracing.sanitize_text(input_summary, 512),
                        },
                    )
                    if created:
                        yield ev.run_step_start(
                            step_id=pending["span_id"],
                            kind="llm",
                            name=pending["name"],
                            parent_id=pending["parent_span_id"],
                            summary=model,
                            started_at=pending["started_at"].isoformat(),
                        )
                    pending_spans[run_id]["model"] = model
                    pending_spans[run_id]["model_requested"] = model
                    pending_spans[run_id]["run_id"] = run_id
                    pending_spans[run_id]["parent_run_id"] = parent_ids[-1] if parent_ids else None
                    # 首个 chunk 到达时记录 first_token event / TTFT
                    pending_spans[run_id]["stream_started"] = datetime.now(UTC)

                elif etype == "on_chat_model_end":
                    output = data.get("output")
                    um = getattr(output, "usage_metadata", None) or {}
                    usage = {
                        "input": um.get("input_tokens", 0),
                        "output": um.get("output_tokens", 0),
                        "total": um.get("total_tokens", 0),
                    }
                    # 根只汇总（不覆盖，累加）
                    for k, v in usage.items():
                        token_usage[k] = token_usage.get(k, 0) + v
                    pending = pending_spans.get(run_id)
                    if pending is not None:
                        pending["token_usage"] = usage
                        pending["output_summary"] = _summarize_output(output)
                        if hasattr(output, "response_metadata"):
                            rm = output.response_metadata or {}
                            if rm.get("model_name"):
                                pending["model"] = rm["model_name"]
                            if rm.get("finish_reason"):
                                pending["finish_reason"] = rm["finish_reason"]
                    frame = _step_end_frame(_close_span(run_id))
                    if frame:
                        yield frame

                elif etype in {"on_chat_model_error", "on_llm_error"}:
                    exc = data.get("error")
                    pending = pending_spans.get(run_id)
                    if pending is not None:
                        pending["error"] = tracing.sanitize_error(exc)
                    frame = _step_end_frame(_close_span(run_id, status="error"))
                    if frame:
                        yield frame

                elif etype == "on_tool_start":
                    tool_name = event.get("name", "")
                    tool_input = data.get("input", {})
                    tool_calls_count += 1
                    parent_id = _resolve_parent(parent_ids)
                    input_summary = _summarize_tool_input(tool_name, tool_input)
                    pending, created = _register_span(
                        run_id,
                        name=f"tool.{tool_name}",
                        span_type="tool",
                        parent_span_id=parent_id,
                        input_summary=input_summary,
                        attributes={
                            "tool": tool_name,
                            "tool_input": tracing.redact_object(tool_input, limit=256),
                        },
                    )
                    public_arguments = _public_tool_arguments(tool_input)
                    if created:
                        yield ev.run_step_start(
                            step_id=pending["span_id"],
                            kind="tool",
                            name=tool_name,
                            parent_id=pending["parent_span_id"],
                            summary=_public_tool_arguments_text(public_arguments),
                            started_at=pending["started_at"].isoformat(),
                        )
                    yield ev.tool_start(
                        pending["span_id"],
                        tool_name,
                        public_arguments,
                    )

                elif etype == "on_tool_end":
                    out = data.get("output", "")
                    tool_name = event.get("name", "")
                    pending = pending_spans.get(run_id)
                    if pending is not None:
                        pending["references"] = _extract_knowledge_references(tool_name, out)
                        pending["output_summary"] = _summarize_tool_output(
                            tool_name, out, pending["references"]
                        )
                        pending["attributes"]["tool_output_summary"] = pending["output_summary"]
                        if pending["references"]:
                            pending["attributes"]["references"] = pending["references"]
                    closed = _close_span(run_id)
                    frame = _step_end_frame(closed)
                    if frame:
                        yield frame
                    if closed:
                        yield ev.tool_end(
                            closed["span_id"],
                            tool_name,
                            _public_step_summary(closed) or "执行完成",
                        )

                elif etype == "on_tool_error":
                    tool_name = event.get("name", "") or "tool"
                    exc = data.get("error")
                    pending = pending_spans.get(run_id)
                    if pending is not None:
                        pending["error"] = tracing.sanitize_error(exc)
                    frame = _step_end_frame(
                        _close_span(run_id, status="error", data={"error": exc})
                    )
                    if frame:
                        yield frame
                    yield ev.error(50002, f"tool error: {exc}")

                elif etype == "on_chain_start":
                    node_name = event.get("name", "") or "agent"
                    if node_name and node_name not in {"__start__", "__end__"}:
                        node_input = data.get("input")
                        task_card = node_input.get("task") if isinstance(node_input, dict) else None
                        parent_id = _resolve_parent(parent_ids)
                        pending, created = _register_span(
                            run_id,
                            name=f"node.{node_name}",
                            span_type="node",
                            parent_span_id=parent_id,
                            input_summary=_summarize_input(node_input),
                            attributes={
                                "task_id": task_card.get("task_id"),
                                "agent_name": task_card.get("target_agent"),
                            }
                            if isinstance(task_card, dict)
                            else None,
                        )
                        if created:
                            yield ev.run_step_start(
                                step_id=pending["span_id"],
                                kind="node",
                                name=pending["name"],
                                parent_id=pending["parent_span_id"],
                                started_at=pending["started_at"].isoformat(),
                            )

                elif etype == "on_chain_error":
                    node_name = event.get("name", "") or "agent"
                    exc = data.get("error")
                    pending = pending_spans.get(run_id)
                    if pending is not None:
                        pending["error"] = tracing.sanitize_error(exc)
                    frame = _step_end_frame(
                        _close_span(run_id, status="error", data={"error": exc})
                    )
                    if frame:
                        yield frame

                elif etype == "on_chain_end":
                    node_name = event.get("name", "") or "agent"
                    output = data.get("output")
                    if isinstance(output, dict) and output.get("final_answer"):
                        final_answer = str(output["final_answer"])
                    if not parent_ids and isinstance(output, dict):
                        state_error = str(output.get("orchestration_error") or "")
                        if (
                            state_error
                            and state_error != "partial_failure"
                            and run_status == "ok"
                        ):
                            run_status = "error"
                            run_error = state_error
                            yield ev.error(
                                50003,
                                final_answer or "编排执行失败，请稍后重试。",
                            )
                    if node_name and node_name not in {"__start__", "__end__"}:
                        frame = _step_end_frame(_close_span(run_id))
                        if frame:
                            yield frame
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            # HITL 中断检测（§5.3）：流正常结束但仍有未关闭节点 → interrupted
            if run_status == "ok" and pending_spans:
                run_status = "interrupted"
            for rid in list(pending_spans):
                close_status = "interrupted" if run_status == "interrupted" else run_status
                if run_error and not pending_spans[rid].get("error"):
                    pending_spans[rid]["error"] = run_error
                frame = _step_end_frame(_close_span(rid, status=close_status))
                if frame:
                    final_step_frames.append(frame)
            duration_ms = int((time.monotonic() - start) * 1000)
            root_span.finalize(
                status=run_status,
                token_usage=token_usage,
                error=run_error,
                attributes={
                    "reply": "".join(_pending_reply).strip() or None,
                    "tool_calls": tool_calls_count,
                    "skill_loaded": skill_names,
                    "memory_injected": bool(memory_context),
                },
            )

        duration_ms = int((time.monotonic() - start) * 1000)
        for frame in final_step_frames:
            yield frame
        if not msg_id and not clarification_only:
            yield ev.message_start()
            yield ev.message_delta("msg_stream", final_answer or _dummy_reply())
        yield ev.message_end(token_usage)
        yield ev.run_end(
            status="success" if run_status == "ok" else run_status,
            duration_ms=duration_ms,
            tool_calls=tool_calls_count,
        )

    async def ainvoke(
        self,
        thread_id: str,
        message: str,
        tenant_id: str,
        user_id: str,
        skill_names: list[str],
        memory_context: str = "",
        session_id: str | None = None,
        request_id: str | None = None,
        message_id: int | None = None,
        links: tuple[str, ...] | None = None,
    ) -> dict:
        """一次性运行（非流式），返回最终状态（含 _observability 摘要）。"""
        from ..observability import tracing

        cfg = _config(thread_id, tenant_id)
        start = time.monotonic()
        sess_uuid = None
        if session_id:
            try:
                sess_uuid = uuid.UUID(session_id)
            except ValueError:
                sess_uuid = None

        root_span = tracing.start_root_span(
            name="agent.run",
            tenant_id=tenant_id,
            session_id=sess_uuid,
            user_id=user_id,
            request_id=request_id,
            thread_id=thread_id,
            links=links,
            attributes={
                "thread_id": thread_id,
                "user_message": tracing.sanitize_text(message, 256),
                "skill_loaded": skill_names,
                "memory_injected": bool(memory_context),
            },
        )

        token_usage: dict[str, int] = {"input": 0, "output": 0, "total": 0}
        run_status = "ok"
        run_error: str | None = None
        pending_spans: dict[str, dict] = {}
        result: dict = {}

        def _resolve_parent(parent_ids: list[str]) -> str | None:
            for pid in reversed(parent_ids):
                if pid in pending_spans:
                    return pending_spans[pid]["span_id"]
            return root_span.span_id

        def _close_span(run_id: str, *, status: str = "ok", error: str | None = None) -> None:
            pending = pending_spans.pop(run_id, None)
            if not pending:
                return
            ended = datetime.now(UTC)
            duration_ms = max(0, int((ended - pending["started_at"]).total_seconds() * 1000))
            _append_span_event(
                pending,
                "end",
                timestamp=ended,
                summary=pending.get("output_summary") or error or pending.get("error"),
                status=status,
                details={"duration_ms": duration_ms},
            )
            cost = _cost_for_pending(pending)
            tracing.record_child_span(
                trace_id=root_span.trace_id,
                parent_span_id=pending["parent_span_id"],
                tenant_id=tenant_id,
                session_id=sess_uuid,
                user_id=user_id,
                span_name=pending["name"],
                span_type=pending["type"],
                status=status,
                model=pending.get("model"),
                token_usage=pending.get("token_usage"),
                input_summary=pending.get("input_summary"),
                output_summary=pending.get("output_summary"),
                duration_ms=duration_ms,
                started_at=pending["started_at"],
                ended_at=ended,
                attributes=pending.get("attributes"),
                error=error or pending.get("error"),
                span_id=pending["span_id"],
                cost_input=cost.get("cost_input"),
                cost_output=cost.get("cost_output"),
                cost_total=cost.get("cost_total"),
                currency=cost.get("currency"),
                model_provider=pending.get("model_provider"),
                model_name_requested=pending.get("model_requested"),
                model_name_resolved=pending.get("model"),
                finish_reason=pending.get("finish_reason"),
                ttft_ms=pending.get("ttft_ms"),
                retry_count=pending.get("retry_count", 0),
                run_id=pending.get("run_id"),
                parent_run_id=pending.get("parent_run_id"),
                model_parameters=pending.get("model_parameters"),
                events=pending.get("events"),
            )

        try:
            async for event in self.app.astream_events(
                {
                    "messages": [("user", message)],
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "skill_loaded": skill_names,
                    "memory_context": memory_context,
                    "session_id": session_id or "",
                    "message_id": message_id,
                    "request_id": request_id or "",
                },
                config=cfg,
                version="v2",
            ):
                etype = event.get("event", "")
                data = event.get("data") or {}
                run_id = event.get("run_id") or ""
                parent_ids = event.get("parent_ids") or []

                if etype == "on_chat_model_start":
                    model = (
                        data.get("model")
                        or get_tool_context().model_id
                        or settings.LLM_MODEL
                    )
                    pending_spans[run_id] = {
                        "span_id": tracing._new_span_id(),
                        "name": "llm.chat",
                        "type": "llm",
                        "parent_span_id": _resolve_parent(parent_ids),
                        "started_at": datetime.now(UTC),
                        "model": model,
                        "model_requested": model,
                        "run_id": run_id,
                        "parent_run_id": parent_ids[-1] if parent_ids else None,
                        "input_summary": _summarize_input(data.get("input")),
                        "streaming": False,
                    }
                    _append_span_event(
                        pending_spans[run_id],
                        "start",
                        summary=pending_spans[run_id].get("input_summary"),
                    )
                elif etype == "on_chat_model_end":
                    output = data.get("output")
                    um = getattr(output, "usage_metadata", None) or {}
                    usage = {
                        "input": um.get("input_tokens", 0),
                        "output": um.get("output_tokens", 0),
                        "total": um.get("total_tokens", 0),
                    }
                    for k, v in usage.items():
                        token_usage[k] = token_usage.get(k, 0) + v
                    pending = pending_spans.get(run_id)
                    if pending is not None:
                        pending["token_usage"] = usage
                        pending["output_summary"] = _summarize_output(output)
                    _close_span(run_id)
                elif etype in {"on_chat_model_error", "on_llm_error"}:
                    exc = data.get("error")
                    pending = pending_spans.get(run_id)
                    if pending is not None:
                        pending["error"] = tracing.sanitize_error(exc)
                    _close_span(run_id, status="error")
                elif etype == "on_tool_start":
                    tool_name = event.get("name", "")
                    tool_input = data.get("input", {})
                    pending_spans[run_id] = {
                        "span_id": tracing._new_span_id(),
                        "name": f"tool.{tool_name}",
                        "type": "tool",
                        "parent_span_id": _resolve_parent(parent_ids),
                        "started_at": datetime.now(UTC),
                        "input_summary": _summarize_tool_input(tool_name, tool_input),
                        "attributes": {
                            "tool": tool_name,
                            "tool_input": tracing.redact_object(tool_input, limit=256),
                        },
                    }
                    _append_span_event(
                        pending_spans[run_id],
                        "start",
                        summary=pending_spans[run_id].get("input_summary"),
                    )
                elif etype == "on_tool_end":
                    out = data.get("output", "")
                    tool_name = event.get("name", "")
                    pending = pending_spans.get(run_id)
                    if pending is not None:
                        pending["output_summary"] = _summarize_tool_output(tool_name, out)
                    _close_span(run_id)
                elif etype == "on_tool_error":
                    exc = data.get("error")
                    pending = pending_spans.get(run_id)
                    if pending is not None:
                        pending["error"] = tracing.sanitize_error(exc)
                    _close_span(run_id, status="error")
                elif etype == "on_chain_start":
                    node_name = event.get("name", "") or "agent"
                    if node_name and node_name not in {"__start__", "__end__"}:
                        node_input = data.get("input")
                        task_card = node_input.get("task") if isinstance(node_input, dict) else None
                        pending_spans[run_id] = {
                            "span_id": tracing._new_span_id(),
                            "name": f"node.{node_name}",
                            "type": "node",
                            "parent_span_id": _resolve_parent(parent_ids),
                            "started_at": datetime.now(UTC),
                            "input_summary": _summarize_input(node_input),
                            "attributes": {
                                "task_id": task_card.get("task_id"),
                                "agent_name": task_card.get("target_agent"),
                            }
                            if isinstance(task_card, dict)
                            else None,
                        }
                        _append_span_event(
                            pending_spans[run_id],
                            "start",
                            summary=pending_spans[run_id].get("input_summary"),
                        )
                elif etype == "on_chain_end":
                    node_name = event.get("name", "") or "agent"
                    output = data.get("output")
                    if not parent_ids and isinstance(output, dict):
                        result = output
                    if node_name and node_name not in {"__start__", "__end__"}:
                        _close_span(run_id)
                elif etype == "on_chain_error":
                    exc = data.get("error")
                    pending = pending_spans.get(run_id)
                    if pending is not None:
                        pending["error"] = tracing.sanitize_error(exc)
                    _close_span(run_id, status="error")
        except asyncio.CancelledError:
            run_status = "canceled"
            run_error = "client disconnected"
            for rid in list(pending_spans):
                _close_span(rid, status="canceled")
            raise
        except Exception as exc:  # noqa: BLE001
            run_status = "error"
            run_error = tracing.sanitize_error(exc)
            for rid in list(pending_spans):
                _close_span(rid, status="error", error=run_error)

        if not result:
            try:
                snapshot = await self.app.aget_state(cfg)
                result = dict(snapshot.values or {})
            except Exception:  # noqa: BLE001
                result = {}
        # 根 span 汇总
        root_span.finalize(
            status=run_status,
            token_usage=token_usage,
            error=run_error,
            attributes={
                "memory_injected": bool(memory_context),
            },
        )
        result["_observability"] = {
            "trace_id": root_span.trace_id,
            "request_id": request_id,
            "status": run_status,
            "token_usage": token_usage,
            "duration_ms": int((time.monotonic() - start) * 1000),
        }
        return result

    async def resume(
        self,
        thread_id: str,
        user_input: str,
        decision: str | None = None,
        *,
        tenant_id: str = "default",
        user_id: str | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
        link_trace_id: str | None = None,
    ) -> AsyncIterator[str]:
        """HITL 恢复：向挂起的 interrupt 提交输入后继续执行（流式）。"""
        from langgraph.types import Command

        from ..observability import tracing

        cfg = _config(thread_id, tenant_id)
        start = time.monotonic()

        sess_uuid = None
        if session_id:
            try:
                sess_uuid = uuid.UUID(session_id)
            except ValueError:
                sess_uuid = None

        links = (link_trace_id,) if link_trace_id else None
        root_span = tracing.start_root_span(
            name="agent.resume",
            tenant_id=tenant_id,
            session_id=sess_uuid,
            user_id=user_id,
            request_id=request_id,
            thread_id=thread_id,
            links=links,
            attributes={
                "thread_id": thread_id,
                "user_input": tracing.sanitize_text(user_input, 256),
                "linked_trace_id": link_trace_id,
                "links": list(links) if links else None,
            },
        )
        yield ev.trace_start(
            trace_id=root_span.trace_id,
            request_id=request_id,
            session_id=session_id,
        )

        msg_id = ""
        tool_calls_count = 0
        token_usage: dict[str, int] = {"input": 0, "output": 0, "total": 0}
        run_status = "ok"
        _pending_reply: list[str] = []
        pending_spans: dict[str, dict] = {}

        def _resolve_parent(parent_ids: list[str]) -> str | None:
            for pid in reversed(parent_ids):
                if pid in pending_spans:
                    return pending_spans[pid]["span_id"]
            return root_span.span_id

        def _close_span(run_id: str, *, status: str = "ok") -> dict | None:
            pending = pending_spans.pop(run_id, None)
            if not pending:
                return None
            ended = datetime.now(UTC)
            duration_ms = max(0, int((ended - pending["started_at"]).total_seconds() * 1000))
            _append_span_event(
                pending,
                "end",
                timestamp=ended,
                summary=pending.get("output_summary") or pending.get("error"),
                status=status,
                details={"duration_ms": duration_ms},
            )
            cost = _cost_for_pending(pending)
            tracing.record_child_span(
                trace_id=root_span.trace_id,
                parent_span_id=pending["parent_span_id"],
                tenant_id=tenant_id,
                session_id=sess_uuid,
                user_id=user_id,
                span_name=pending["name"],
                span_type=pending["type"],
                status=status,
                model=pending.get("model"),
                token_usage=pending.get("token_usage"),
                input_summary=pending.get("input_summary"),
                output_summary=pending.get("output_summary"),
                duration_ms=duration_ms,
                started_at=pending["started_at"],
                ended_at=ended,
                attributes=pending.get("attributes"),
                error=pending.get("error"),
                span_id=pending["span_id"],
                cost_input=cost.get("cost_input"),
                cost_output=cost.get("cost_output"),
                cost_total=cost.get("cost_total"),
                currency=cost.get("currency"),
                model_provider=pending.get("model_provider"),
                model_name_requested=pending.get("model_requested"),
                model_name_resolved=pending.get("model"),
                finish_reason=pending.get("finish_reason"),
                ttft_ms=pending.get("ttft_ms"),
                retry_count=pending.get("retry_count", 0),
                run_id=pending.get("run_id"),
                parent_run_id=pending.get("parent_run_id"),
                model_parameters=pending.get("model_parameters"),
                events=pending.get("events"),
            )
            pending["status"] = status
            pending["duration_ms"] = duration_ms
            return pending

        def _step_end_frame(pending: dict | None) -> str | None:
            if pending is None:
                return None
            return ev.run_step_end(
                step_id=pending["span_id"],
                status=pending.get("status", "ok"),
                duration_ms=pending.get("duration_ms", 0),
                summary=_public_step_summary(pending),
                error=pending.get("error"),
                token_usage=pending.get("token_usage"),
                references=pending.get("references"),
            )

        try:
            async for event in self.app.astream_events(
                Command(resume=user_input),
                config=cfg,
                version="v2",
            ):
                etype = event.get("event", "")
                data = event.get("data") or {}
                run_id = event.get("run_id") or ""
                parent_ids = event.get("parent_ids") or []

                if etype == "on_chat_model_stream":
                    chunk = data.get("chunk")
                    if chunk is not None:
                        text = _extract_text(chunk)
                        if text:
                            _pending_reply.append(text)
                            if not msg_id:
                                yield ev.message_start()
                                msg_id = "msg_resume"
                            yield ev.message_delta(msg_id, text[:64])
                        if run_id in pending_spans and "ttft_recorded" not in pending_spans[run_id]:
                            pending_spans[run_id]["ttft_recorded"] = True
                            pending_spans[run_id]["ttft_ms"] = int(
                                (
                                    datetime.now(UTC) - pending_spans[run_id]["stream_started"]
                                ).total_seconds()
                                * 1000
                            )
                            _append_span_event(
                                pending_spans[run_id],
                                "first_token",
                                details={"ttft_ms": pending_spans[run_id]["ttft_ms"]},
                            )
                            yield ev.run_step_update(
                                step_id=pending_spans[run_id]["span_id"],
                                phase="first_token",
                                detail={"ttft_ms": pending_spans[run_id]["ttft_ms"]},
                            )
                elif etype == "on_chat_model_start":
                    model = (
                        data.get("model")
                        or get_tool_context().model_id
                        or settings.LLM_MODEL
                    )
                    pending_spans[run_id] = {
                        "span_id": tracing._new_span_id(),
                        "name": "llm.chat",
                        "type": "llm",
                        "parent_span_id": _resolve_parent(parent_ids),
                        "started_at": datetime.now(UTC),
                        "model": model,
                        "model_requested": model,
                        "run_id": run_id,
                        "parent_run_id": parent_ids[-1] if parent_ids else None,
                        "input_summary": _summarize_input(data.get("input")),
                        "stream_started": datetime.now(UTC),
                    }
                    _append_span_event(
                        pending_spans[run_id],
                        "start",
                        summary=pending_spans[run_id].get("input_summary"),
                    )
                    pending = pending_spans[run_id]
                    yield ev.run_step_start(
                        step_id=pending["span_id"],
                        kind="llm",
                        name=pending["name"],
                        parent_id=pending["parent_span_id"],
                        summary=model,
                        started_at=pending["started_at"].isoformat(),
                    )
                elif etype == "on_chat_model_end":
                    output = data.get("output")
                    um = getattr(output, "usage_metadata", None) or {}
                    usage = {
                        "input": um.get("input_tokens", 0),
                        "output": um.get("output_tokens", 0),
                        "total": um.get("total_tokens", 0),
                    }
                    for k, v in usage.items():
                        token_usage[k] = token_usage.get(k, 0) + v
                    pending = pending_spans.get(run_id)
                    if pending is not None:
                        pending["token_usage"] = usage
                        pending["output_summary"] = _summarize_output(output)
                    frame = _step_end_frame(_close_span(run_id))
                    if frame:
                        yield frame
                elif etype == "on_tool_start":
                    tool_name = event.get("name", "")
                    tool_input = data.get("input", {})
                    tool_calls_count += 1
                    pending_spans[run_id] = {
                        "span_id": tracing._new_span_id(),
                        "name": f"tool.{tool_name}",
                        "type": "tool",
                        "parent_span_id": _resolve_parent(parent_ids),
                        "started_at": datetime.now(UTC),
                        "input_summary": _summarize_tool_input(tool_name, tool_input),
                        "attributes": {
                            "tool": tool_name,
                            "tool_input": tracing.redact_object(tool_input, limit=256),
                        },
                    }
                    _append_span_event(
                        pending_spans[run_id],
                        "start",
                        summary=pending_spans[run_id].get("input_summary"),
                    )
                    pending = pending_spans[run_id]
                    public_arguments = _public_tool_arguments(tool_input)
                    yield ev.run_step_start(
                        step_id=pending["span_id"],
                        kind="tool",
                        name=tool_name,
                        parent_id=pending["parent_span_id"],
                        summary=_public_tool_arguments_text(public_arguments),
                        started_at=pending["started_at"].isoformat(),
                    )
                    yield ev.tool_start(
                        pending["span_id"],
                        tool_name,
                        public_arguments,
                    )
                elif etype == "on_tool_end":
                    out = data.get("output", "")
                    tool_name = event.get("name", "")
                    pending = pending_spans.get(run_id)
                    if pending is not None:
                        pending["references"] = _extract_knowledge_references(tool_name, out)
                        pending["output_summary"] = _summarize_tool_output(
                            tool_name, out, pending["references"]
                        )
                        if pending["references"]:
                            pending.setdefault("attributes", {})["references"] = pending["references"]
                    closed = _close_span(run_id)
                    frame = _step_end_frame(closed)
                    if frame:
                        yield frame
                    if closed:
                        yield ev.tool_end(
                            closed["span_id"],
                            tool_name,
                            _public_step_summary(closed) or "执行完成",
                        )
                elif etype == "on_tool_error":
                    exc = data.get("error")
                    pending = pending_spans.get(run_id)
                    if pending is not None:
                        pending["error"] = tracing.sanitize_error(exc)
                    frame = _step_end_frame(_close_span(run_id, status="error"))
                    if frame:
                        yield frame
                elif etype == "on_chain_start":
                    node_name = event.get("name", "") or "agent"
                    if node_name and node_name not in {"__start__", "__end__"}:
                        pending_spans[run_id] = {
                            "span_id": tracing._new_span_id(),
                            "name": f"node.{node_name}",
                            "type": "node",
                            "parent_span_id": _resolve_parent(parent_ids),
                            "started_at": datetime.now(UTC),
                            "input_summary": _summarize_input(data.get("input")),
                        }
                        _append_span_event(
                            pending_spans[run_id],
                            "start",
                            summary=pending_spans[run_id].get("input_summary"),
                        )
                        pending = pending_spans[run_id]
                        yield ev.run_step_start(
                            step_id=pending["span_id"],
                            kind="node",
                            name=pending["name"],
                            parent_id=pending["parent_span_id"],
                            started_at=pending["started_at"].isoformat(),
                        )
                elif etype == "on_chain_end":
                    node_name = event.get("name", "") or "agent"
                    if node_name and node_name not in {"__start__", "__end__"}:
                        frame = _step_end_frame(_close_span(run_id))
                        if frame:
                            yield frame
                elif etype == "on_chain_error":
                    exc = data.get("error")
                    pending = pending_spans.get(run_id)
                    if pending is not None:
                        pending["error"] = tracing.sanitize_error(exc)
                    frame = _step_end_frame(_close_span(run_id, status="error"))
                    if frame:
                        yield frame
                elif etype in {"on_chat_model_error", "on_llm_error"}:
                    exc = data.get("error")
                    pending = pending_spans.get(run_id)
                    if pending is not None:
                        pending["error"] = tracing.sanitize_error(exc)
                    frame = _step_end_frame(_close_span(run_id, status="error"))
                    if frame:
                        yield frame
        except Exception as exc:  # noqa: BLE001
            run_status = "error"
            yield ev.error(50001, f"resume failed: {exc}")
            root_span.finalize(
                status="error",
                attributes={"reply": "".join(_pending_reply).strip() or None},
            )
            return

        duration_ms = int((time.monotonic() - start) * 1000)
        yield ev.message_end(token_usage)
        yield ev.run_end(
            status="success" if run_status == "ok" else run_status,
            duration_ms=duration_ms,
            tool_calls=tool_calls_count,
        )
        root_span.finalize(
            status=run_status,
            token_usage=token_usage,
            attributes={"reply": "".join(_pending_reply).strip() or None},
        )


def _message_values(value: Any) -> list[Any]:
    """展开 LangChain 的单层/批量消息输入，同时保留 ``(role, content)``。"""

    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
        return [value]
    if isinstance(value, (list, tuple)):
        messages: list[Any] = []
        for item in value:
            messages.extend(_message_values(item))
        return messages
    return [value]


def _summarize_message(message: Any) -> str | None:
    """将一条 LangChain 消息转换成短文本，避免把对象 repr 直接落库。"""
    from ..observability import tracing

    role: str | None = None
    content: Any = None
    tool_calls: Any = None
    if isinstance(message, tuple) and len(message) == 2:
        role, content = str(message[0]), message[1]
    elif isinstance(message, dict):
        role = str(message.get("role") or message.get("type") or "message")
        content = message.get("content")
        tool_calls = message.get("tool_calls")
    else:
        role = str(getattr(message, "type", None) or message.__class__.__name__)
        content = getattr(message, "content", None)
        tool_calls = getattr(message, "tool_calls", None)

    text: str | None = None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = [
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        text = " ".join(part for part in parts if part)

    if text:
        return tracing.sanitize_text(f"{role}: {text}", 180)
    if tool_calls:
        calls: list[str] = []
        for call in tool_calls[:5]:
            if not isinstance(call, dict):
                continue
            name = call.get("name") or "tool"
            args = call.get("args")
            keys = ", ".join(str(key) for key in list(args)[:10]) if isinstance(args, dict) else ""
            calls.append(f"{name}({keys})")
        if calls:
            return tracing.sanitize_text(f"{role}: tool_calls {', '.join(calls)}", 180)
    return tracing.sanitize_text(str(role), 180)


def _summarize_input(input_data: Any) -> str | None:
    """记录 LangGraph 节点/LLM 的真实输入形状，并生成脱敏短摘要。"""
    from ..observability import tracing

    if input_data is None:
        return None
    message_input = input_data.get("messages") if isinstance(input_data, dict) else input_data
    if message_input:
        messages = _message_values(message_input)
        summaries = [summary for item in messages[-3:] if (summary := _summarize_message(item))]
        if summaries:
            return tracing.sanitize_text(" | ".join(summaries), 512)

    sanitized = tracing.redact_object(input_data, limit=512)
    return tracing.sanitize_text(str(sanitized), 512)


def _summarize_prompt(input_data: Any) -> str | None:
    """兼容旧调用名；LLM prompt 与节点输入使用同一真实事件摘要器。"""

    return _summarize_input(input_data)


def _summarize_output(output: Any) -> str | None:
    """LLM 出参摘要：取回复文本，截断。"""
    from ..observability import tracing

    content = getattr(output, "content", None) if output is not None else None
    if isinstance(content, str) and content:
        return tracing.sanitize_text(content, 512)
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                return tracing.sanitize_text(item.get("text", ""), 512)
    tool_calls = getattr(output, "tool_calls", None) if output is not None else None
    if tool_calls:
        calls: list[str] = []
        for call in tool_calls[:5]:
            if not isinstance(call, dict):
                continue
            name = str(call.get("name") or "tool")
            args = call.get("args")
            keys = ", ".join(str(key) for key in list(args)[:10]) if isinstance(args, dict) else ""
            calls.append(f"{name}({keys})")
        if calls:
            return tracing.sanitize_text(f"请求调用工具：{'、'.join(calls)}", 512)
    return None


def _summarize_tool_input(tool_name: str, tool_input: Any) -> str:
    """工具入参摘要：保留脱敏后的短参数，供 Trace 与历史过程回放。"""
    from ..observability import tracing

    if isinstance(tool_input, dict):
        redacted = tracing.redact_object(tool_input, limit=320)
        payload = json.dumps(redacted, ensure_ascii=False, separators=(", ", ": "))
        return tracing.redact_text(f"{tool_name}({payload})", 400) or f"{tool_name}()"
    value = tracing.redact_text(str(tool_input), 240)
    return f"{tool_name}({value or 'input'})"


def _tool_output_text(out: Any) -> str:
    content = getattr(out, "content", out)
    return str(content or "")


_REFERENCE_HEADER_RE = re.compile(r"^\[片段\s+(\d+)\s+\|\s+(.+)\]$", re.MULTILINE)
_REFERENCE_ID_RE = re.compile(r"KB-([0-9a-fA-F-]{36})/(\d+)")
_REFERENCE_LINE_RE = re.compile(r"第\s*(\d+)\s*-\s*(\d+)\s*行")


def _extract_knowledge_references(tool_name: str, out: Any) -> list[dict[str, Any]]:
    """从 knowledge_search 完整输出提取结构化来源，避免 UI 依赖截断正文。"""
    from ..observability import tracing

    if tool_name != "knowledge_search":
        return []
    text = _tool_output_text(out)
    matches = list(_REFERENCE_HEADER_RE.finditer(text))
    references: list[dict[str, Any]] = []
    for index, match in enumerate(matches[:10]):
        fields: dict[str, str] = {}
        for part in match.group(2).split(" | "):
            if ": " in part:
                key, value = part.split(": ", 1)
                fields[key.strip()] = value.strip()
            elif part.startswith("相关度 "):
                fields["相关度"] = part.removeprefix("相关度 ").strip()
        reference_match = _REFERENCE_ID_RE.search(fields.get("引用", ""))
        if not reference_match:
            continue
        line_match = _REFERENCE_LINE_RE.search(fields.get("位置", ""))
        excerpt_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        excerpt = text[match.end():excerpt_end].strip()
        score_raw = fields.get("相关度")
        try:
            score = float(score_raw) if score_raw is not None else None
        except ValueError:
            score = None
        chunk_raw = fields.get("分块", "").lstrip("#")
        doc_raw = fields.get("文档ID", "")
        references.append(
            {
                "rank": int(match.group(1)),
                "kb_id": reference_match.group(1),
                "chunk_id": int(reference_match.group(2)),
                "doc_id": int(doc_raw) if doc_raw.isdigit() else None,
                "source_file": fields.get("来源") or "未知文档",
                "chunk_index": int(chunk_raw) if chunk_raw.isdigit() else None,
                "heading": None
                if fields.get("章节") in {None, "未标注章节"}
                else fields.get("章节"),
                "line_start": int(line_match.group(1)) if line_match else None,
                "line_end": int(line_match.group(2)) if line_match else None,
                "score": score,
                "strategy": fields.get("策略"),
                "excerpt": tracing.redact_text(excerpt, 220),
            }
        )
    return references


def _reference_summary(references: list[dict[str, Any]]) -> str | None:
    if not references:
        return None
    sources = list(dict.fromkeys(str(item["source_file"]) for item in references))
    source_text = "、".join(sources[:2])
    if len(sources) > 2:
        source_text += f" 等 {len(sources)} 个文档"
    return f"命中 {len(references)} 个相关片段 · {source_text}"


def _summarize_tool_output(
    tool_name: str,
    out: Any,
    references: list[dict[str, Any]] | None = None,
) -> str | None:
    """工具出参摘要：截断为短摘要。"""
    from ..observability import tracing

    if tool_name == "knowledge_search":
        return _reference_summary(references or _extract_knowledge_references(tool_name, out))
    return tracing.redact_text(_tool_output_text(out), 200)


def _public_tool_arguments(tool_input: Any) -> dict[str, Any]:
    """为聊天过程面板生成脱敏工具参数；metadata_only 仅展示工具名称。"""
    from ..observability import tracing

    if settings.CONTENT_CAPTURE_MODE == "metadata_only":
        return {}
    redacted = tracing.redact_object(tool_input, limit=240)
    if isinstance(redacted, dict):
        return redacted
    return {"input": redacted}


def _public_tool_arguments_text(arguments: dict[str, Any]) -> str | None:
    if not arguments:
        return None
    return json.dumps(arguments, ensure_ascii=False, separators=(", ", ": "))


def _public_step_summary(pending: dict) -> str | None:
    """向聊天 UI 暴露脱敏后的真实输出，不暴露 Prompt 或模型私有推理。"""
    from ..observability import tracing

    if settings.CONTENT_CAPTURE_MODE == "metadata_only":
        return None
    if pending.get("references"):
        return _reference_summary(pending["references"])
    if pending.get("type") == "llm":
        return tracing.redact_text(pending.get("output_summary"), 512)
    if pending.get("type") != "tool":
        return None
    return tracing.redact_text(
        pending.get("output_summary") or pending.get("error"), 240
    )


def _is_public_stream_event(event: dict) -> bool:
    """只把面向用户的主 Agent/直派/Synthesizer token 写入 SSE。"""
    node = str((event.get("metadata") or {}).get("langgraph_node") or "")
    if not node:
        return True
    return node in {"agent", "main", "direct_worker", "synthesizer"}


def _extract_text(chunk: Any) -> str:
    """从 LLM chunk 中提取文本。"""
    if chunk is None:
        return ""
    if hasattr(chunk, "content"):
        c = chunk.content
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            parts = []
            for item in c:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
            return "".join(parts)
    return ""


def _dummy_reply() -> str:
    return "（未收到模型回复，请检查 LLM_API_KEY 配置）"
