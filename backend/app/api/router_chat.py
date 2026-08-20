"""会话与对话路由：非流式 + SSE 流式 + HITL resume。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..harness.app_runtime import harness_app
from ..harness.context import (
    ToolContext,
    reset_tool_context,
    set_tool_context,
)
from ..harness.session_manager import derive_session_title, session_manager
from ..knowledge.service import knowledge_service
from ..memory.service import memory_service
from ..observability import audit
from ..observability.metrics import metrics
from ..storage import repositories as repo
from ..storage.db import get_db
from .deps import AuthContext, get_client_ip, require_auth
from .schemas import (
    ApiResponse,
    MessageResponse,
    MessageSendRequest,
    MessageStreamRequest,
    ResumeRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sessions", tags=["chat"])


async def _ensure_session_title(db: AsyncSession, sess, message: str) -> None:
    if not sess.title or sess.title in {"新对话", "新会话"}:
        title = derive_session_title(message)
        await repo.set_session_title(db, sess.id, title)
        sess.title = title


# 后台记忆提炼任务集（防 GC + 异常兜底 + 有界并发，对齐 PARSE_CONCURRENCY）
_extract_tasks: set[asyncio.Task] = set()
_EXTRACT_CONCURRENCY = 3
_extract_semaphore = asyncio.Semaphore(_EXTRACT_CONCURRENCY)


def _schedule_memory_extract(
    *,
    tenant_id: str,
    user_id: str,
    user_msg: str,
    assistant_msg: str,
    session_id: uuid.UUID,
    session_title: str | None,
) -> None:
    """对话结束后后台调度记忆提炼（MEMORY_SPEC §6.1），不阻塞响应。

    内部独立 DB session 执行；失败仅记录日志。有界 Semaphore 防止并发高峰。
    """
    if not settings.MEMORY_ENABLED:
        return
    task = asyncio.create_task(
        _run_memory_extract(
            tenant_id=tenant_id,
            user_id=user_id,
            user_msg=user_msg,
            assistant_msg=assistant_msg,
            session_id=session_id,
            session_title=session_title,
        )
    )
    _extract_tasks.add(task)
    task.add_done_callback(_extract_tasks.discard)


async def _run_memory_extract(
    *,
    tenant_id: str,
    user_id: str,
    user_msg: str,
    assistant_msg: str,
    session_id: uuid.UUID,
    session_title: str | None,
) -> None:
    """后台记忆提炼任务：独立 session，限并发 + 异常兜底。"""
    from ..storage.db import AsyncSessionLocal

    async with _extract_semaphore:
        try:
            async with AsyncSessionLocal() as db:
                written = await memory_service.extract_from_conversation(
                    db,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    user_msg=user_msg,
                    assistant_msg=assistant_msg,
                    session_id=session_id,
                    session_title=session_title,
                )
                if written:
                    await audit.audit(
                        tenant_id,
                        "memory.extract",
                        user_id=user_id,
                        session_id=session_id,
                        detail={"written": written},
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("后台记忆提炼失败: %s", exc)


async def _retrieve_memory_context(
    db: AsyncSession, *, tenant_id: str, user_id: str, query: str
) -> str:
    """对话前检索用户长期记忆并格式化为 system prompt 注入文本（MEMORY_SPEC §6.2）。"""
    if not settings.MEMORY_ENABLED or not settings.MEMORY_AUTO_INJECT:
        return ""
    hits = await memory_service.retrieve_relevant(
        db, tenant_id=tenant_id, user_id=user_id, query=query
    )
    return memory_service.format_for_prompt(hits)


@router.post("/{session_id}/messages", response_model=ApiResponse)
async def send_message(
    session_id: uuid.UUID,
    body: MessageSendRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """发送消息（非流式，返回完整回复）。"""
    sess = await repo.get_session(db, session_id)
    if sess is None or sess.status == "deleted":
        raise HTTPException(status_code=404, detail="session not found")

    start = time.monotonic()
    if harness_app.runtime is None:
        raise HTTPException(status_code=503, detail="harness not ready")

    await _ensure_session_title(db, sess, body.message)
    await repo.add_message(
        db, session_id=session_id, role="user", content=body.message
    )
    skill_names = body.skills or []
    kb_names = body.knowledge_bases or []

    # RAG 上下文注入：解析会话绑定的知识库白名单，供 knowledge_search 工具读取
    allowed_kb_ids = await knowledge_service.resolve_ids_by_names(
        db, tenant_id=auth.tenant.tenant_id, names=kb_names
    )
    # 长期记忆自动注入：对话前检索当前用户记忆（MEMORY_SPEC §6.2 方式 A）
    memory_context = await _retrieve_memory_context(
        db,
        tenant_id=auth.tenant.tenant_id,
        user_id=auth.user_id,
        query=body.message,
    )
    set_tool_context(
        ToolContext(
            tenant_id=auth.tenant.tenant_id,
            user_id=auth.user_id,
            allowed_kb_ids=allowed_kb_ids,
        )
    )
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    try:
        result = await harness_app.runtime.ainvoke(
            sess.thread_id,
            body.message,
            auth.tenant.tenant_id,
            auth.user_id,
            skill_names,
            memory_context,
            session_id=str(session_id),
            request_id=request_id,
        )
    finally:
        reset_tool_context()

    # 提取最终回复
    reply = _extract_final_answer(result, body.message)
    duration_ms = int((time.monotonic() - start) * 1000)
    obs = result.get("_observability") or {}
    await repo.add_message(
        db, session_id=session_id, role="assistant", content=reply
    )

    # 对话结束后台提炼长期记忆（不阻塞响应，MEMORY_SPEC §6.1）
    _schedule_memory_extract(
        tenant_id=auth.tenant.tenant_id,
        user_id=auth.user_id,
        user_msg=body.message,
        assistant_msg=reply,
        session_id=session_id,
        session_title=sess.title,
    )

    # 从运行结果提取工具调用次数与 token 用量
    tool_calls_count, token_usage = _extract_usage(result)

    # 审计 + 指标（使用真实状态，C-07）
    run_ok = obs.get("status", "ok") == "ok"
    await audit.audit(
        auth.tenant.tenant_id,
        "chat.send",
        user_id=auth.user_id,
        session_id=session_id,
        detail={"model": settings.LLM_MODEL, "tool_calls": tool_calls_count},
    )
    metrics.record_request(
        auth.tenant.tenant_id, ok=run_ok, duration_ms=duration_ms, token_usage=token_usage
    )

    return ApiResponse(
        data=MessageResponse(
            session_id=str(session_id),
            reply=reply,
            token_usage=token_usage,
            tool_calls=tool_calls_count,
            duration_ms=duration_ms,
        )
    )


@router.post("/{session_id}/messages/stream")
async def stream_message(
    session_id: uuid.UUID,
    body: MessageStreamRequest,
    request: Request,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """发送消息（SSE 流式）。

    MUST：连接关闭或客户端断开后清理运行资源。
    """
    sess = await repo.get_session(db, session_id)
    if sess is None or sess.status == "deleted":
        raise HTTPException(status_code=404, detail="session not found")
    if harness_app.runtime is None:
        raise HTTPException(status_code=503, detail="harness not ready")

    await _ensure_session_title(db, sess, body.message)
    await repo.add_message(
        db, session_id=session_id, role="user", content=body.message
    )
    skill_names = body.skills or []
    kb_names = body.knowledge_bases or []
    thread_id = sess.thread_id
    tenant_id = auth.tenant.tenant_id
    user_id = auth.user_id
    client_ip = get_client_ip(request)

    # 解析会话绑定的知识库白名单（用于 SSE generator 内注入工具上下文）
    allowed_kb_ids = await knowledge_service.resolve_ids_by_names(
        db, tenant_id=tenant_id, names=kb_names
    )
    # 长期记忆自动注入：对话前检索当前用户记忆（MEMORY_SPEC §6.2 方式 A）
    memory_context = await _retrieve_memory_context(
        db, tenant_id=tenant_id, user_id=user_id, query=body.message
    )

    metrics.session_open(str(session_id))
    request_id = f"req-{uuid.uuid4().hex[:12]}"

    async def event_stream():
        reply_parts: list[str] = []
        stream_start = time.monotonic()
        run_status = "ok"
        # RAG 上下文注入：SSE 生成器运行在独立 task，必须在内部设置 ContextVar
        set_tool_context(
            ToolContext(
                tenant_id=tenant_id,
                user_id=user_id,
                allowed_kb_ids=allowed_kb_ids,
            )
        )
        try:
            async for frame in harness_app.runtime.astream(
                thread_id,
                body.message,
                tenant_id,
                user_id,
                skill_names,
                session_id=str(session_id),
                memory_context=memory_context,
                request_id=request_id,
            ):
                # 从 run_end 事件提取实际状态（C-07：不固定按成功写）
                if "event: run_end" in frame:
                    try:
                        data = next((line[6:] for line in frame.splitlines() if line.startswith("data: ")), "{}")
                        payload = json.loads(data)
                        if payload.get("status") != "success":
                            run_status = "error"
                    except json.JSONDecodeError:
                        pass
                delta = _extract_stream_delta(frame)
                if delta:
                    reply_parts.append(delta)
                yield frame
        except Exception as exc:  # noqa: BLE001
            logger.exception("SSE 流异常")
            run_status = "error"
            yield "event: error\ndata: {\"code\":50000,\"message\":\"stream error\"}\n\n"
        finally:
            reset_tool_context()
            metrics.session_close(str(session_id))
            duration_ms = int((time.monotonic() - stream_start) * 1000)
            reply = "".join(reply_parts).strip()
            if reply:
                await repo.add_message(
                    db, session_id=session_id, role="assistant", content=reply
                )
            # 对话结束后台提炼长期记忆（不阻塞响应，MEMORY_SPEC §6.1）
            if reply:
                _schedule_memory_extract(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    user_msg=body.message,
                    assistant_msg=reply,
                    session_id=session_id,
                    session_title=sess.title,
                )
            await audit.audit(
                tenant_id,
                "chat.stream",
                user_id=user_id,
                session_id=session_id,
                detail={"model": settings.LLM_MODEL},
                ip=client_ip,
            )
            # C-06/C-07：使用真实耗时与状态
            metrics.record_request(tenant_id, ok=(run_status == "ok"), duration_ms=duration_ms)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Request-Id": request_id,
        },
    )


def _extract_stream_delta(frame: str) -> str:
    """提取 SSE message_delta 文本，用于流结束后的历史持久化。"""
    event = ""
    data = ""
    for line in frame.splitlines():
        if line.startswith("event:"):
            event = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data = line.removeprefix("data:").strip()
    if event != "message_delta" or not data:
        return ""
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return ""
    content = payload.get("content", "")
    return content if isinstance(content, str) else ""


@router.post("/{session_id}/resume")
async def resume(
    session_id: uuid.UUID,
    body: ResumeRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """HITL 恢复：向挂起的 interrupt 提交输入后继续执行（流式）。"""
    sess = await repo.get_session(db, session_id)
    if sess is None or sess.status == "deleted":
        raise HTTPException(status_code=404, detail="session not found")
    if harness_app.runtime is None:
        raise HTTPException(status_code=503, detail="harness not ready")

    request_id = f"req-{uuid.uuid4().hex[:12]}"

    async def event_stream():
        try:
            async for frame in harness_app.runtime.resume(
                sess.thread_id,
                body.input,
                body.decision,
                tenant_id=auth.tenant.tenant_id,
                user_id=auth.user_id,
                session_id=str(session_id),
                request_id=request_id,
            ):
                yield frame
        except Exception as exc:  # noqa: BLE001
            yield "event: error\ndata: {\"code\":50002,\"message\":\"resume failed\"}\n\n"

    await audit.audit(
        auth.tenant.tenant_id,
        "chat.resume",
        user_id=auth.user_id,
        session_id=session_id,
    )
    return StreamingResponse(
        event_stream(), media_type="text/event-stream"
    )


def _extract_final_answer(result: dict, fallback: str) -> str:
    """从运行结果中提取最终回复文本。"""
    messages = result.get("messages", [])
    if messages:
        last = messages[-1]
        content = getattr(last, "content", None)
        if isinstance(content, str) and content:
            return content
    return f"（演示回复）已收到：{fallback}"


def _extract_usage(result: dict) -> tuple[int, dict[str, int]]:
    """从运行结果提取工具调用次数与 token 用量。"""
    messages = result.get("messages", [])
    tool_calls_count = 0
    token_usage: dict[str, int] = {"prompt": 0, "completion": 0, "total": 0}
    for msg in messages:
        tcs = getattr(msg, "tool_calls", None)
        if tcs:
            tool_calls_count += len(tcs)
        um = getattr(msg, "usage_metadata", None) or {}
        if um:
            token_usage["prompt"] = um.get("input_tokens", 0)
            token_usage["completion"] = um.get("output_tokens", 0)
            token_usage["total"] = um.get("total_tokens", 0)
    return tool_calls_count, token_usage
