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
import logging
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from langgraph.graph.state import CompiledStateGraph

from ..config import settings
from ..observability.costing import compute_cost as _compute_cost
from . import events as ev

logger = logging.getLogger(__name__)

# SSE 帧类型：字符串
SseEvent = str


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
            attributes: dict | None = None,
        ) -> None:
            # 同一 run_id 只分配一次 span_id（避免重复 start 事件覆盖导致父子错乱）
            existing = pending_spans.get(run_id)
            if existing is not None:
                existing.setdefault("attributes", {}).update(attributes or {})
                return
            pending_spans[run_id] = {
                "span_id": tracing._new_span_id(),
                "name": name,
                "type": span_type,
                "parent_span_id": parent_span_id,
                "started_at": datetime.now(UTC),
                "attributes": attributes,
            }

        def _resolve_parent(parent_ids: list[str]) -> str | None:
            """按最近父 run_id 计算 parent_span_id（修复 C-02）。"""
            for pid in reversed(parent_ids):
                if pid in pending_spans:
                    return pending_spans[pid]["span_id"]
            return root_span.span_id

        def _close_span(run_id: str, *, status: str = "ok", data: dict | None = None) -> None:
            """按 run_id 关闭一个进行中的子 span，并按真实父节点挂载。"""
            pending = pending_spans.pop(run_id, None)
            if not pending:
                return
            ended = datetime.now(UTC)
            duration_ms = max(
                0, int((ended - pending["started_at"]).total_seconds() * 1000)
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
                        _close_span(rid, status="timeout")
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
                        _close_span(rid, status="error")
                    yield ev.error(50001, f"graph error: {exc}")
                    break

                if etype == "on_chat_model_stream":
                    chunk = data.get("chunk")
                    if chunk is not None:
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
                                (datetime.now(UTC) - pending_spans[run_id]["stream_started"]).total_seconds() * 1000
                            )

                elif etype == "on_chat_model_start":
                    # 记录每次 LLM 调用的独立 span（C-03/C-04/C-05）
                    model = data.get("model") or settings.LLM_MODEL
                    parent_id = _resolve_parent(parent_ids)
                    _register_span(
                        run_id,
                        name="llm.chat",
                        span_type="llm",
                        parent_span_id=parent_id,
                        attributes={
                            "model_requested": model,
                            "model_resolved": model,
                            "streaming": True,
                            "prompt_input": tracing.sanitize_text(
                                _summarize_prompt(data.get("input")), 512
                            ),
                        },
                    )
                    pending_spans[run_id]["model"] = model
                    pending_spans[run_id]["model_requested"] = model
                    pending_spans[run_id]["run_id"] = run_id
                    pending_spans[run_id]["parent_run_id"] = (
                        parent_ids[-1] if parent_ids else None
                    )
                    pending_spans[run_id]["input_summary"] = _summarize_prompt(data.get("input"))
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
                    _close_span(run_id)

                elif etype == "on_tool_start":
                    tool_name = event.get("name", "")
                    tool_input = data.get("input", {})
                    tool_calls_count += 1
                    parent_id = _resolve_parent(parent_ids)
                    _register_span(
                        run_id,
                        name=f"tool.{tool_name}",
                        span_type="tool",
                        parent_span_id=parent_id,
                        attributes={
                            "tool": tool_name,
                            "tool_input": tracing.redact_object(tool_input, limit=256),
                        },
                    )
                    pending_spans[run_id]["input_summary"] = _summarize_tool_input(tool_name, tool_input)
                    yield ev.tool_start(
                        tool_name,
                        tool_input if isinstance(tool_input, dict) else {"input": str(tool_input)},
                    )

                elif etype == "on_tool_end":
                    out = data.get("output", "")
                    tool_name = event.get("name", "")
                    pending = pending_spans.get(run_id)
                    if pending is not None:
                        pending["output_summary"] = _summarize_tool_output(tool_name, out)
                        pending["attributes"]["tool_output_summary"] = _summarize_tool_output(tool_name, out)
                    _close_span(run_id)
                    yield ev.tool_end(tool_name, str(out))

                elif etype == "on_tool_error":
                    tool_name = event.get("name", "") or "tool"
                    exc = data.get("error")
                    pending = pending_spans.get(run_id)
                    if pending is not None:
                        pending["error"] = tracing.sanitize_error(exc)
                    _close_span(run_id, status="error", data={"error": exc})
                    yield ev.error(50002, f"tool error: {exc}")

                elif etype == "on_chain_start":
                    node_name = event.get("name", "") or "agent"
                    if node_name and node_name not in {"__start__", "__end__"}:
                        parent_id = _resolve_parent(parent_ids)
                        _register_span(
                            run_id,
                            name=f"node.{node_name}",
                            span_type="node",
                            parent_span_id=parent_id,
                        )

                elif etype == "on_chain_error":
                    node_name = event.get("name", "") or "agent"
                    exc = data.get("error")
                    pending = pending_spans.get(run_id)
                    if pending is not None:
                        pending["error"] = tracing.sanitize_error(exc)
                    _close_span(run_id, status="error", data={"error": exc})

                elif etype == "on_chain_end":
                    node_name = event.get("name", "") or "agent"
                    if node_name and node_name not in {"__start__", "__end__"}:
                        _close_span(run_id)
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
                    _close_span(rid, status="interrupted")
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
        if not msg_id:
            yield ev.message_start()
            yield ev.message_delta("msg_stream", _dummy_reply())
        yield ev.message_end(token_usage)
        yield ev.run_end(
            status="success" if run_status == "ok" else "error",
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
                },
                config=cfg,
                version="v2",
            ):
                etype = event.get("event", "")
                data = event.get("data") or {}
                run_id = event.get("run_id") or ""
                parent_ids = event.get("parent_ids") or []

                if etype == "on_chat_model_start":
                    model = data.get("model") or settings.LLM_MODEL
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
                        "input_summary": _summarize_prompt(data.get("input")),
                        "streaming": False,
                    }
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
                        pending_spans[run_id] = {
                            "span_id": tracing._new_span_id(),
                            "name": f"node.{node_name}",
                            "type": "node",
                            "parent_span_id": _resolve_parent(parent_ids),
                            "started_at": datetime.now(UTC),
                        }
                elif etype == "on_chain_end":
                    node_name = event.get("name", "") or "agent"
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

        result = await self.app.ainvoke(
            {
                "messages": [("user", message)],
                "tenant_id": tenant_id,
                "user_id": user_id,
                "skill_loaded": skill_names,
                "memory_context": memory_context,
            },
            config=cfg,
        )
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
        token_usage: dict[str, int] = {"input": 0, "output": 0, "total": 0}
        run_status = "ok"
        _pending_reply: list[str] = []
        pending_spans: dict[str, dict] = {}

        def _resolve_parent(parent_ids: list[str]) -> str | None:
            for pid in reversed(parent_ids):
                if pid in pending_spans:
                    return pending_spans[pid]["span_id"]
            return root_span.span_id

        def _close_span(run_id: str, *, status: str = "ok") -> None:
            pending = pending_spans.pop(run_id, None)
            if not pending:
                return
            ended = datetime.now(UTC)
            duration_ms = max(0, int((ended - pending["started_at"]).total_seconds() * 1000))
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
                elif etype == "on_chat_model_start":
                    model = data.get("model") or settings.LLM_MODEL
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
                        "input_summary": _summarize_prompt(data.get("input")),
                    }
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
                elif etype == "on_chain_start":
                    node_name = event.get("name", "") or "agent"
                    if node_name and node_name not in {"__start__", "__end__"}:
                        pending_spans[run_id] = {
                            "span_id": tracing._new_span_id(),
                            "name": f"node.{node_name}",
                            "type": "node",
                            "parent_span_id": _resolve_parent(parent_ids),
                            "started_at": datetime.now(UTC),
                        }
                elif etype == "on_chain_end":
                    node_name = event.get("name", "") or "agent"
                    if node_name and node_name not in {"__start__", "__end__"}:
                        _close_span(run_id)
                elif etype == "on_chain_error":
                    exc = data.get("error")
                    pending = pending_spans.get(run_id)
                    if pending is not None:
                        pending["error"] = tracing.sanitize_error(exc)
                    _close_span(run_id, status="error")
                elif etype == "on_chat_model_error":
                    exc = data.get("error")
                    pending = pending_spans.get(run_id)
                    if pending is not None:
                        pending["error"] = tracing.sanitize_error(exc)
                    _close_span(run_id, status="error")
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
        yield ev.run_end(status="success", duration_ms=duration_ms, tool_calls=0)
        root_span.finalize(
            status=run_status,
            token_usage=token_usage,
            attributes={"reply": "".join(_pending_reply).strip() or None},
        )


def _summarize_prompt(input_data: Any) -> str | None:
    """LLM prompt 摘要：取 messages 首条用户文本，截断。"""
    from ..observability import tracing

    if not input_data:
        return None
    messages = input_data.get("messages") if isinstance(input_data, dict) else None
    if messages:
        for msg in messages:
            content = getattr(msg, "content", None)
            if isinstance(content, str) and content:
                return tracing.sanitize_text(content, 512)
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        return tracing.sanitize_text(item.get("text", ""), 512)
    return None


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
    return None


def _summarize_tool_input(tool_name: str, tool_input: Any) -> str:
    """工具入参摘要：仅存参数 key 列表 + 短文本。"""
    if isinstance(tool_input, dict):
        keys = ", ".join(list(tool_input.keys())[:20])
        return f"{tool_name}({keys})"
    return f"{tool_name}(input)"


def _summarize_tool_output(tool_name: str, out: Any) -> str | None:
    """工具出参摘要：截断为短摘要。"""

    s = str(out)
    if len(s) > 200:
        s = s[:200] + "…"
    return s or None


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
