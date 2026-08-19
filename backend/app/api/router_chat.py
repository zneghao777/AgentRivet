"""会话与对话路由：非流式 + SSE 流式 + HITL resume。"""

from __future__ import annotations

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
    set_tool_context(
        ToolContext(
            tenant_id=auth.tenant.tenant_id,
            user_id=auth.user_id,
            allowed_kb_ids=allowed_kb_ids,
        )
    )
    try:
        result = await harness_app.runtime.ainvoke(
            sess.thread_id, body.message, auth.tenant.tenant_id, auth.user_id, skill_names
        )
    finally:
        reset_tool_context()

    # 提取最终回复
    reply = _extract_final_answer(result, body.message)
    duration_ms = int((time.monotonic() - start) * 1000)
    await repo.add_message(
        db, session_id=session_id, role="assistant", content=reply
    )

    # 从运行结果提取工具调用次数与 token 用量
    tool_calls_count, token_usage = _extract_usage(result)

    # 审计 + 指标
    await audit.audit(
        auth.tenant.tenant_id,
        "chat.send",
        user_id=auth.user_id,
        session_id=session_id,
        detail={"model": settings.LLM_MODEL, "tool_calls": tool_calls_count},
    )
    metrics.record_request(
        auth.tenant.tenant_id, ok=True, duration_ms=duration_ms, token_usage=token_usage
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

    metrics.session_open(str(session_id))

    async def event_stream():
        reply_parts: list[str] = []
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
            ):
                delta = _extract_stream_delta(frame)
                if delta:
                    reply_parts.append(delta)
                yield frame
        except Exception as exc:  # noqa: BLE001
            logger.exception("SSE 流异常")
            yield "event: error\ndata: {\"code\":50000,\"message\":\"stream error\"}\n\n"
        finally:
            reset_tool_context()
            metrics.session_close(str(session_id))
            reply = "".join(reply_parts).strip()
            if reply:
                await repo.add_message(
                    db, session_id=session_id, role="assistant", content=reply
                )
            await audit.audit(
                tenant_id,
                "chat.stream",
                user_id=user_id,
                session_id=session_id,
                detail={"model": settings.LLM_MODEL},
                ip=client_ip,
            )
            metrics.record_request(tenant_id, ok=True, duration_ms=1)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
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

    async def event_stream():
        try:
            async for frame in harness_app.runtime.resume(
                sess.thread_id, body.input, body.decision
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
