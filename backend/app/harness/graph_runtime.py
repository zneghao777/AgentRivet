"""图运行封装：invoke / stream / interrupt 恢复，转换为 SSE 事件。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from langgraph.graph.state import CompiledStateGraph

from ..config import settings
from . import events as ev

logger = logging.getLogger(__name__)

# SSE 帧类型：字符串
SseEvent = str


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
    ) -> AsyncIterator[str]:
        """流式运行，内部将 LangGraph 事件转换为 SSE 事件。

        session_id 为可选的日志/审计关联（用于 session_start 事件）。
        """
        cfg = _config(thread_id, tenant_id)
        start = time.monotonic()

        # session_start 事件
        if session_id:
            yield ev.session_start(session_id, thread_id)

        # 消息文本增量组装
        msg_id = ""
        tool_calls_count = 0
        token_usage: dict[str, int] = {"prompt": 0, "completion": 0, "total": 0}

        # 用队列桥接 LangGraph stream 与 async generator
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

        # 事件类型 → SSE 帧
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=settings.AGENT_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    yield ev.error(50001, "graph execution timeout")
                    break
                if event is None:
                    break

                # LangGraph astream_events v2：顶层字段为 event，数据在 data 内
                etype = event.get("event", "")
                data = event.get("data") or {}
                if event.get("type") == "_error":
                    exc = event.get("exc")
                    yield ev.error(50001, f"graph error: {exc}")
                    break

                if etype == "on_chat_model_stream":
                    chunk = data.get("chunk")
                    if chunk is not None:
                        text = _extract_text(chunk)
                        if text:
                            if not msg_id:
                                yield ev.message_start()
                                msg_id = "msg_stream"
                            yield ev.message_delta(msg_id, text[:64])

                elif etype == "on_chat_model_end":
                    output = data.get("output")
                    um = getattr(output, "usage_metadata", None) or {}
                    token_usage["prompt"] = um.get("input_tokens", 0)
                    token_usage["completion"] = um.get("output_tokens", 0)
                    token_usage["total"] = um.get("total_tokens", 0)

                elif etype == "on_tool_start":
                    tool_name = event.get("name", "")
                    tool_input = data.get("input", {})
                    tool_calls_count += 1
                    yield ev.tool_start(
                        tool_name,
                        tool_input if isinstance(tool_input, dict) else {"input": str(tool_input)},
                    )

                elif etype == "on_tool_end":
                    out = data.get("output", "")
                    tool_name = event.get("name", "")
                    yield ev.tool_end(tool_name, str(out))
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # 收尾
        duration_ms = int((time.monotonic() - start) * 1000)
        if not msg_id:
            yield ev.message_start()
            yield ev.message_delta("msg_stream", _dummy_reply())
        yield ev.message_end(token_usage)
        yield ev.run_end(
            status="success", duration_ms=duration_ms, tool_calls=tool_calls_count
        )

    async def ainvoke(
        self,
        thread_id: str,
        message: str,
        tenant_id: str,
        user_id: str,
        skill_names: list[str],
    ) -> dict:
        """一次性运行（非流式），返回最终状态。"""
        cfg = _config(thread_id, tenant_id)
        result = await self.app.ainvoke(
            {
                "messages": [("user", message)],
                "tenant_id": tenant_id,
                "user_id": user_id,
                "skill_loaded": skill_names,
            },
            config=cfg,
        )
        return result

    async def resume(
        self, thread_id: str, user_input: str, decision: str | None = None
    ) -> AsyncIterator[str]:
        """HITL 恢复：向挂起的 interrupt 提交输入后继续执行（流式）。"""
        from langgraph.types import Command

        cfg = _config(thread_id, "default")
        start = time.monotonic()
        msg_id = ""
        token_usage: dict[str, int] = {"prompt": 0, "completion": 0, "total": 0}

        try:
            async for event in self.app.astream_events(
                Command(resume=user_input),
                config=cfg,
                version="v2",
            ):
                if event.get("event", "") == "on_chat_model_stream":
                    chunk = (event.get("data") or {}).get("chunk")
                    if chunk is not None:
                        text = _extract_text(chunk)
                        if text:
                            if not msg_id:
                                yield ev.message_start()
                                msg_id = "msg_resume"
                            yield ev.message_delta(msg_id, text[:64])
        except Exception as exc:  # noqa: BLE001
            yield ev.error(50001, f"resume failed: {exc}")
            return

        duration_ms = int((time.monotonic() - start) * 1000)
        yield ev.message_end(token_usage)
        yield ev.run_end(status="success", duration_ms=duration_ms, tool_calls=0)


def _extract_text(chunk: Any) -> str:
    """从 LLM chunk 中提取文本。"""
    if chunk is None:
        return ""
    # AIMessageChunk
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
