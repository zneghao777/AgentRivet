"""会话与对话路由：非流式 + SSE 流式 + HITL resume。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..agents.model_endpoint import ModelEndpointError, validate_model_base_url
from ..agents.models import ResolvedAgentCapabilities
from ..agents.resolver import resolve_agent_capabilities, resolve_orchestration_agents
from ..chat.attachments import (
    ChatAttachmentError,
    delete_attachment,
    prepare_attachments,
    save_attachment,
)
from ..config import settings
from ..harness.app_runtime import harness_app
from ..harness.context import (
    ToolContext,
    reset_tool_context,
    set_tool_context,
)
from ..harness.session_manager import derive_session_title
from ..knowledge.service import knowledge_service
from ..mcp.manager import mcp_manager
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
VISION_MODEL = "deepseek-v4-flash-vision-exp"


def _requested_mcp_ids(refs) -> set[uuid.UUID] | None:
    values: set[uuid.UUID] = set()
    for ref in refs or []:
        if ref.kind != "mcp_tool":
            continue
        try:
            values.add(uuid.UUID(ref.id))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="MCP capability id 无效") from exc
    return values or None


async def _ensure_session_title(db: AsyncSession, sess, message: str) -> None:
    if not sess.title or sess.title in {"新对话", "新会话"}:
        title = derive_session_title(message)
        await repo.set_session_title(db, sess.id, title)
        sess.title = title


def _request_model_and_attachments(
    *,
    body: MessageSendRequest | MessageStreamRequest,
    tenant_id: str,
    session_id: uuid.UUID,
    agent: ResolvedAgentCapabilities | None = None,
) -> tuple[str, str | None, str | None, list[dict]]:
    model_id = body.model or (agent.model if agent else None) or settings.LLM_MODEL
    model_base_url: str | None = agent.model_base_url if agent else None
    model_api_key: str | None = None
    if body.provider_config is not None:
        try:
            model_base_url = validate_model_base_url(body.provider_config.base_url)
        except ModelEndpointError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        model_api_key = (
            body.provider_config.api_key.get_secret_value()
            if body.provider_config.api_key is not None
            else ""
        )
    try:
        attachments = prepare_attachments(tenant_id, session_id, body.attachment_ids)
    except ChatAttachmentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if any(item.get("kind") == "image" for item in attachments) and model_id != VISION_MODEL:
        raise HTTPException(
            status_code=422,
            detail=f"图片附件需要使用视觉模型 {VISION_MODEL}",
        )
    return model_id, model_base_url, model_api_key, attachments


def _body_agent_id(body: MessageSendRequest | MessageStreamRequest) -> uuid.UUID | None:
    if not body.agent_id:
        return None
    try:
        return uuid.UUID(body.agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="agent_id 无效") from exc


async def _resolve_request_agent(
    db: AsyncSession,
    *,
    tenant_id: str,
    session,
    body: MessageSendRequest | MessageStreamRequest,
) -> ResolvedAgentCapabilities:
    if not settings.AGENT_HUB_ENABLED:
        return ResolvedAgentCapabilities()
    requested = _body_agent_id(body)
    return await resolve_agent_capabilities(
        db,
        tenant_id=tenant_id,
        agent_id=requested if requested is not None else session.agent_id,
    )


async def _resolve_orchestration_context(
    db: AsyncSession, *, tenant_id: str
):
    if not settings.MULTI_AGENT_ENABLED:
        return {}
    return await resolve_orchestration_agents(db, tenant_id=tenant_id)


def _resolve_skill_names(
    body: MessageSendRequest | MessageStreamRequest,
    agent: ResolvedAgentCapabilities,
) -> list[str]:
    if not agent.active:
        return body.skills or []
    bound = agent.allowed_skill_names or []
    if body.skills is None:
        return bound
    requested = set(body.skills)
    return [name for name in bound if name in requested]


def _resolve_mcp_ids(
    body: MessageSendRequest | MessageStreamRequest,
    agent: ResolvedAgentCapabilities,
) -> set[uuid.UUID] | None:
    requested = _requested_mcp_ids(body.capabilities)
    if not agent.active:
        return requested
    bound = agent.allowed_capability_ids or set()
    return bound if requested is None else bound & requested


@router.post("/{session_id}/attachments", response_model=ApiResponse)
async def upload_chat_attachment(
    session_id: uuid.UUID,
    file: UploadFile = File(...),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """上传本次会话使用的图片或文档。"""
    sess = await repo.get_session(db, session_id)
    if sess is None or sess.status == "deleted":
        raise HTTPException(status_code=404, detail="session not found")
    content = await file.read(settings.CHAT_ATTACHMENT_MAX_SIZE_MB * 1024 * 1024 + 1)
    try:
        metadata = save_attachment(
            auth.tenant.tenant_id,
            session_id,
            file.filename or "attachment",
            content,
        )
    except ChatAttachmentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await audit.audit(
        auth.tenant.tenant_id,
        "chat.attachment.upload",
        user_id=auth.user_id,
        session_id=session_id,
        detail={"name": metadata["name"], "kind": metadata["kind"], "size": metadata["size"]},
    )
    return ApiResponse(data=metadata)


@router.delete("/{session_id}/attachments/{attachment_id}", response_model=ApiResponse)
async def remove_chat_attachment(
    session_id: uuid.UUID,
    attachment_id: str,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """删除尚未发送或不再需要的聊天附件。"""
    sess = await repo.get_session(db, session_id)
    if sess is None or sess.status == "deleted":
        raise HTTPException(status_code=404, detail="session not found")
    try:
        delete_attachment(auth.tenant.tenant_id, session_id, attachment_id)
    except ChatAttachmentError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ApiResponse(data={"deleted": True})


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
    kwargs = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "user_msg": user_msg,
        "assistant_msg": assistant_msg,
        "session_id": session_id,
        "session_title": session_title,
    }
    if settings.MQ_ENABLED and settings.MQ_SESSION_ENABLED:
        from ..mq.publisher import publish_nowait
        from ..observability.tracing import redact_text

        payload = {
            **kwargs,
            "session_id": str(session_id),
            "user_msg": redact_text(user_msg) or "",
            "assistant_msg": redact_text(assistant_msg) or "",
        }
        if publish_nowait(
            "session.postprocess",
            payload,
            fallback=lambda: _run_memory_extract(**kwargs),
        ):
            return
    task = asyncio.create_task(_run_memory_extract(**kwargs))
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
    if sess is None or sess.tenant_id != auth.tenant.tenant_id or sess.status == "deleted":
        raise HTTPException(status_code=404, detail="session not found")

    start = time.monotonic()
    if harness_app.runtime is None:
        raise HTTPException(status_code=503, detail="harness not ready")

    resolved_agent = await _resolve_request_agent(
        db, tenant_id=auth.tenant.tenant_id, session=sess, body=body
    )
    model_id, model_base_url, model_api_key, attachments = _request_model_and_attachments(
        body=body,
        tenant_id=auth.tenant.tenant_id,
        session_id=session_id,
        agent=resolved_agent,
    )

    request_id = f"req-{uuid.uuid4().hex[:12]}"
    await _ensure_session_title(db, sess, body.message)
    user_message_row = await repo.add_message(
        db,
        session_id=session_id,
        role="user",
        content=body.message,
        request_id=request_id,
        step_index=1,
    )
    skill_names = _resolve_skill_names(body, resolved_agent)
    kb_names = body.knowledge_bases or []

    # RAG 上下文注入：解析会话绑定的知识库白名单，供 knowledge_search 工具读取
    requested_kb_ids = await knowledge_service.resolve_ids_by_names(
        db, tenant_id=auth.tenant.tenant_id, names=kb_names
    )
    if resolved_agent.active and resolved_agent.allowed_kb_ids:
        allowed_kb_ids = (
            resolved_agent.allowed_kb_ids
            if body.knowledge_bases is None
            else [value for value in requested_kb_ids if value in set(resolved_agent.allowed_kb_ids)]
        )
    else:
        allowed_kb_ids = requested_kb_ids
    # 长期记忆自动注入：对话前检索当前用户记忆（MEMORY_SPEC §6.2 方式 A）
    memory_context = ""
    if resolved_agent.memory_enabled:
        memory_context = await _retrieve_memory_context(
            db,
            tenant_id=auth.tenant.tenant_id,
            user_id=auth.user_id,
            query=body.message,
        )
    requested_mcp = _resolve_mcp_ids(body, resolved_agent)
    dynamic_tools = await mcp_manager.resolve_tools(
        db,
        tenant_id=auth.tenant.tenant_id,
        requested_capability_ids=requested_mcp,
    )
    orchestration_agents = await _resolve_orchestration_context(
        db, tenant_id=auth.tenant.tenant_id
    )
    set_tool_context(
        ToolContext(
            tenant_id=auth.tenant.tenant_id,
            user_id=auth.user_id,
            allowed_kb_ids=allowed_kb_ids,
            dynamic_tools=dynamic_tools,
            model_id=model_id,
            model_base_url=model_base_url,
            model_api_key=model_api_key,
            attachments=attachments,
            agent_id=resolved_agent.agent_id,
            agent_name=resolved_agent.agent_name,
            agent_system_prompt=resolved_agent.system_prompt_block,
            allowed_skill_names=resolved_agent.allowed_skill_names if resolved_agent.active else None,
            allowed_tool_names=resolved_agent.allowed_tool_names if resolved_agent.active else None,
            allowed_capability_ids=requested_mcp,
            memory_enabled=resolved_agent.memory_enabled,
            orchestration_agents=orchestration_agents,
            session_id=session_id,
            message_id=user_message_row.id,
            request_id=request_id,
        )
    )
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
            message_id=user_message_row.id,
        )
    finally:
        reset_tool_context()

    # 提取最终回复
    reply = _extract_final_answer(result, body.message)
    duration_ms = int((time.monotonic() - start) * 1000)
    obs = result.get("_observability") or {}
    trace_id = obs.get("trace_id")
    if trace_id:
        await repo.link_messages_to_trace(db, request_id=request_id, trace_id=trace_id)
    routed_name = (result.get("router_decision") or {}).get("target_agent")
    routed_agent = orchestration_agents.get(routed_name) if routed_name else None
    await repo.add_message(
        db,
        session_id=session_id,
        role="assistant",
        content=reply,
        token_usage=obs.get("token_usage"),
        trace_id=trace_id,
        request_id=request_id,
        step_index=2,
        agent_id=routed_agent.agent_id if routed_agent else resolved_agent.agent_id,
    )

    # 对话结束后台提炼长期记忆（不阻塞响应，MEMORY_SPEC §6.1）
    if resolved_agent.memory_enabled:
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
        detail={"model": model_id, "tool_calls": tool_calls_count, "agent_id": str(resolved_agent.agent_id) if resolved_agent.agent_id else None},
    )
    metrics.record_request(
        auth.tenant.tenant_id,
        ok=run_ok,
        duration_ms=duration_ms,
        token_usage=token_usage,
        agent_id=str(resolved_agent.agent_id) if resolved_agent.agent_id else None,
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
    if sess is None or sess.tenant_id != auth.tenant.tenant_id or sess.status == "deleted":
        raise HTTPException(status_code=404, detail="session not found")
    if harness_app.runtime is None:
        raise HTTPException(status_code=503, detail="harness not ready")

    resolved_agent = await _resolve_request_agent(
        db, tenant_id=auth.tenant.tenant_id, session=sess, body=body
    )
    model_id, model_base_url, model_api_key, attachments = _request_model_and_attachments(
        body=body,
        tenant_id=auth.tenant.tenant_id,
        session_id=session_id,
        agent=resolved_agent,
    )

    request_id = f"req-{uuid.uuid4().hex[:12]}"
    await _ensure_session_title(db, sess, body.message)
    user_message_row = await repo.add_message(
        db,
        session_id=session_id,
        role="user",
        content=body.message,
        request_id=request_id,
        step_index=1,
    )
    skill_names = _resolve_skill_names(body, resolved_agent)
    kb_names = body.knowledge_bases or []
    thread_id = sess.thread_id
    tenant_id = auth.tenant.tenant_id
    user_id = auth.user_id
    client_ip = get_client_ip(request)

    # 解析会话绑定的知识库白名单（用于 SSE generator 内注入工具上下文）
    requested_kb_ids = await knowledge_service.resolve_ids_by_names(
        db, tenant_id=tenant_id, names=kb_names
    )
    if resolved_agent.active and resolved_agent.allowed_kb_ids:
        allowed_kb_ids = (
            resolved_agent.allowed_kb_ids
            if body.knowledge_bases is None
            else [value for value in requested_kb_ids if value in set(resolved_agent.allowed_kb_ids)]
        )
    else:
        allowed_kb_ids = requested_kb_ids
    # 长期记忆自动注入：对话前检索当前用户记忆（MEMORY_SPEC §6.2 方式 A）
    memory_context = ""
    if resolved_agent.memory_enabled:
        memory_context = await _retrieve_memory_context(
            db, tenant_id=tenant_id, user_id=user_id, query=body.message
        )
    requested_mcp = _resolve_mcp_ids(body, resolved_agent)
    dynamic_tools = await mcp_manager.resolve_tools(
        db,
        tenant_id=tenant_id,
        requested_capability_ids=requested_mcp,
    )
    orchestration_agents = await _resolve_orchestration_context(db, tenant_id=tenant_id)

    metrics.session_open(str(session_id))
    trace_id: str | None = None
    routed_agent_name: str | None = None

    async def event_stream():
        nonlocal trace_id, routed_agent_name
        reply_parts: list[str] = []
        stream_start = time.monotonic()
        run_status = "ok"
        # RAG 上下文注入：SSE 生成器运行在独立 task，必须在内部设置 ContextVar
        set_tool_context(
            ToolContext(
                tenant_id=tenant_id,
                user_id=user_id,
                allowed_kb_ids=allowed_kb_ids,
                dynamic_tools=dynamic_tools,
                model_id=model_id,
                model_base_url=model_base_url,
                model_api_key=model_api_key,
                attachments=attachments,
                agent_id=resolved_agent.agent_id,
                agent_name=resolved_agent.agent_name,
                agent_system_prompt=resolved_agent.system_prompt_block,
                allowed_skill_names=resolved_agent.allowed_skill_names if resolved_agent.active else None,
                allowed_tool_names=resolved_agent.allowed_tool_names if resolved_agent.active else None,
                allowed_capability_ids=requested_mcp,
                memory_enabled=resolved_agent.memory_enabled,
                orchestration_agents=orchestration_agents,
                session_id=session_id,
                message_id=user_message_row.id,
                request_id=request_id,
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
                message_id=user_message_row.id,
            ):
                if "event: trace_start" in frame:
                    try:
                        data = next(
                            (line[6:] for line in frame.splitlines() if line.startswith("data: ")),
                            "{}",
                        )
                        trace_id = json.loads(data).get("trace_id") or trace_id
                    except json.JSONDecodeError:
                        pass
                if "event: orchestration.router_decision" in frame:
                    try:
                        data = next(
                            (line[6:] for line in frame.splitlines() if line.startswith("data: ")),
                            "{}",
                        )
                        routed_agent_name = json.loads(data).get("target_agent") or routed_agent_name
                    except json.JSONDecodeError:
                        pass
                # 从 run_end 事件提取实际状态（C-07：不固定按成功写）
                if "event: run_end" in frame:
                    try:
                        data = next(
                            (line[6:] for line in frame.splitlines() if line.startswith("data: ")),
                            "{}",
                        )
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
            yield 'event: error\ndata: {"code":50000,"message":"stream error"}\n\n'
        finally:
            reset_tool_context()
            metrics.session_close(str(session_id))
            duration_ms = int((time.monotonic() - stream_start) * 1000)
            reply = "".join(reply_parts).strip()
            if trace_id:
                await repo.link_messages_to_trace(db, request_id=request_id, trace_id=trace_id)
            if reply:
                routed_agent = (
                    orchestration_agents.get(routed_agent_name)
                    if routed_agent_name
                    else None
                )
                await repo.add_message(
                    db,
                    session_id=session_id,
                    role="assistant",
                    content=reply,
                    trace_id=trace_id,
                    request_id=request_id,
                    step_index=2,
                    agent_id=routed_agent.agent_id if routed_agent else resolved_agent.agent_id,
                )
            # 对话结束后台提炼长期记忆（不阻塞响应，MEMORY_SPEC §6.1）
            if reply and resolved_agent.memory_enabled:
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
                detail={"model": model_id, "agent_id": str(resolved_agent.agent_id) if resolved_agent.agent_id else None},
                ip=client_ip,
            )
            # C-06/C-07：使用真实耗时与状态
            metrics.record_request(
                tenant_id,
                ok=(run_status == "ok"),
                duration_ms=duration_ms,
                agent_id=str(resolved_agent.agent_id) if resolved_agent.agent_id else None,
            )

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
            yield 'event: error\ndata: {"code":50002,"message":"resume failed"}\n\n'

    await audit.audit(
        auth.tenant.tenant_id,
        "chat.resume",
        user_id=auth.user_id,
        session_id=session_id,
    )
    return StreamingResponse(event_stream(), media_type="text/event-stream")


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
