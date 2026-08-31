"""会话与对话路由：非流式 + SSE 流式 + HITL resume。"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator

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
    get_tool_context,
    reset_tool_context,
    set_tool_context,
)
from ..harness.graph_runtime import is_first_feedback_event_name
from ..harness.llm_guard import LLMGuardError, LLMOutcomeTimeoutError
from ..harness.semantic_cache import (
    SemanticCacheScope,
    is_semantic_cache_answer_candidate,
    semantic_cache,
)
from ..harness.session_manager import derive_session_title
from ..harness.stream_gate import (
    GuardTicket,
    StreamQueueFullError,
    StreamQueueTimeoutError,
    stream_gate,
    user_stream_lock,
)
from ..knowledge.service import knowledge_service
from ..mcp.manager import mcp_manager
from ..memory.service import memory_service
from ..observability import audit
from ..observability.metrics import metrics
from ..security.api_key import hash_api_key
from ..storage import repositories as repo
from ..storage.db import get_db
from .deps import AuthContext, get_client_ip, require_auth
from .schemas import (
    ApiResponse,
    ChatRunCreateRequest,
    ChatRunResumeRequest,
    MessageResponse,
    MessageSendRequest,
    MessageStreamRequest,
    ResumeRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sessions", tags=["chat"])
VISION_MODEL = "deepseek-v4-flash-vision-exp"
_semantic_cache_tasks: set[asyncio.Task] = set()
_semantic_cache_store_semaphore = asyncio.Semaphore(
    max(1, settings.SEMANTIC_CACHE_STORE_CONCURRENCY)
)
_semantic_cache_closing = False
SSE_HEARTBEAT_INTERVAL_SECONDS = 15.0


def _sse_frame(event: str, payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n"


def _sse_event_name(frame: str) -> str:
    return next(
        (
            line.removeprefix("event:").strip()
            for line in frame.splitlines()
            if line.startswith("event:")
        ),
        "",
    )


async def _observe_sse_first_outcome(
    stream: AsyncIterator[str], *, request_started_monotonic: float
) -> AsyncIterator[str]:
    """Measure the first user-visible feedback when its SSE frame is emitted."""
    recorded = False
    async for frame in stream:
        if not recorded:
            event_name = _sse_event_name(frame)
            if event_name in {"message_delta", "error"} or is_first_feedback_event_name(
                event_name
            ):
                metrics.record_guard_first_outcome(
                    (time.monotonic() - request_started_monotonic) * 1000
                )
                recorded = True
        yield frame


def _stream_lock_principal(request: Request, auth: AuthContext) -> str:
    """Use a real user ID, or the authenticated API-key identity before login exists."""
    if auth.user_id and auth.user_id != settings.ANONYMOUS_USER_ID:
        return auth.user_id
    api_key_id = getattr(request.state, "api_key_id", None)
    if not api_key_id:
        api_key_id = hash_api_key(auth.tenant.api_key)
    return f"api-key:{api_key_id}"


def _semantic_cache_scope(
    request: Request,
    auth: AuthContext,
    *,
    resolved_agent: ResolvedAgentCapabilities,
    model_id: str,
    model_base_url: str | None,
    model_api_key: str | None,
    body: MessageSendRequest | MessageStreamRequest,
    skill_names: list[str],
) -> SemanticCacheScope:
    """Build a privacy-preserving cache scope for the effective model identity."""
    from ..agents.base_agent import SYSTEM_PROMPT_TEMPLATE
    from ..skills.manager import skill_manager

    skill_versions = []
    for name in sorted(skill_names):
        skill = skill_manager.get(name)
        if skill is None:
            skill_versions.append({"name": name, "missing": True})
            continue
        skill_versions.append(
            {
                "name": name,
                "version": skill.version,
                "description": skill.description,
                "body_hash": hashlib.sha256(skill.body_md.encode("utf-8")).hexdigest(),
                "enabled": skill.enabled,
            }
        )
    configuration = {
        "cache_schema": settings.SEMANTIC_CACHE_SCHEMA_VERSION,
        # Embeddings from different models/endpoints are not comparable even
        # when their dimensions happen to match.  Changing the embedding
        # identity must therefore create a fresh cache namespace.
        "embedding_model": settings.EMBEDDING_MODEL,
        "embedding_base_url": settings.EMBEDDING_BASE_URL,
        "embedding_dim": settings.EMBEDDING_DIM,
        # Generation settings can materially change the cached answer shape.
        "llm_temperature": settings.LLM_TEMPERATURE,
        "llm_max_tokens": settings.LLM_MAX_TOKENS,
        "llm_fallback_models": settings.LLM_FALLBACK_MODELS,
        "credential_fingerprint": hashlib.sha256(
            (
                model_api_key
                if model_api_key is not None
                else settings.LLM_API_KEY
            ).encode("utf-8")
        ).hexdigest(),
        "agent_memory_enabled": resolved_agent.memory_enabled,
        "default_prompt_hash": hashlib.sha256(
            SYSTEM_PROMPT_TEMPLATE.encode("utf-8")
        ).hexdigest(),
        "agent_status": resolved_agent.status,
        "agent_system_prompt": resolved_agent.system_prompt_block,
        "allowed_skills": sorted(resolved_agent.allowed_skill_names or []),
        "allowed_tools": sorted(resolved_agent.allowed_tool_names or []),
        "allowed_capabilities": sorted(
            str(value) for value in (resolved_agent.allowed_capability_ids or set())
        ),
        "allowed_kbs": sorted(
            str(value) for value in (resolved_agent.allowed_kb_ids or [])
        ),
        "request_skills": sorted(skill_names),
        "skill_versions": skill_versions,
        "request_kbs": sorted(body.knowledge_bases or []),
        "request_capabilities": sorted(
            f"{item.kind}:{item.id}" for item in (body.capabilities or [])
        ),
    }
    configuration_id = hashlib.sha256(
        json.dumps(
            configuration,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return SemanticCacheScope(
        tenant_id=auth.tenant.tenant_id,
        user_id=_stream_lock_principal(request, auth),
        agent_id=str(resolved_agent.agent_id or "default"),
        model_id=f"{model_base_url or settings.LLM_BASE_URL}|{model_id}",
        configuration_id=configuration_id,
    )


def _semantic_cache_tool_intent(
    body: MessageSendRequest | MessageStreamRequest,
    *,
    resolved_agent: ResolvedAgentCapabilities,
    skill_names: list[str],
    dynamic_tools: list,
    orchestration_agents: dict,
    memory_context: str,
    allowed_kb_ids: list[uuid.UUID] | None = None,
    has_session_history: bool = False,
) -> bool:
    """Conservatively bypass cache whenever the answer can depend on runtime state."""
    normalized = " ".join(body.message.casefold().split())
    dynamic_terms = (
        "当前",
        "现在",
        "今天",
        "日期",
        "几点",
        "最新",
        "实时",
        "搜索",
        "联网",
        "检索",
        "查询",
        "知识库",
        "记忆",
        "skill",
        "工具",
        "current",
        "latest",
        "today",
        "time",
        "date",
        "weather",
        "news",
        "price",
        "search",
        "browse",
        "lookup",
        "find ",
        "knowledge base",
        "memory",
        "plugin",
        "tool",
        "my ",
        "account",
        "balance",
        "order",
        "booking",
        "invoice",
        "subscription",
        "profile",
        "status",
        "previous",
        "above",
        "continue",
        "again",
        "我的",
        "账号",
        "账户",
        "余额",
        "订单",
        "状态",
        "公司",
        "内部",
        "制度",
        "文档",
        "资料",
    )
    stable_question_markers = (
        "什么是",
        "请解释",
        "解释一下",
        "介绍一下",
        "概念",
        "原理",
        "区别",
        "为什么",
        "what is ",
        "what are ",
        "explain ",
        "define ",
        "definition of ",
        "how does ",
        "difference between ",
        "why ",
    )
    dynamic_intent = any(term in normalized for term in dynamic_terms)
    stable_question = any(
        marker in normalized for marker in stable_question_markers
    )
    return bool(
        body.capabilities
        or has_session_history
        or body.knowledge_bases
        or body.attachment_ids
        or allowed_kb_ids
        or resolved_agent.allowed_kb_ids
        or resolved_agent.allowed_tool_names
        or orchestration_agents
        or dynamic_tools
        or memory_context.strip()
        or dynamic_intent
        or any(character.isdigit() for character in normalized)
        or not stable_question
    )


def _schedule_semantic_cache_store(
    question: str,
    answer: str,
    *,
    scope: SemanticCacheScope,
    tool_intent: bool,
    tool_used: bool,
    metadata: dict,
) -> None:
    """Write cache entries off the response path while retaining the task until done."""
    if (
        not settings.SEMANTIC_CACHE_ENABLED
        or _semantic_cache_closing
        or not is_semantic_cache_answer_candidate(answer)
    ):
        return
    if len(_semantic_cache_tasks) >= settings.SEMANTIC_CACHE_MAX_PENDING_STORES:
        logger.warning("语义缓存后台写入积压，已丢弃本次结果")
        return
    task = asyncio.create_task(
        _store_semantic_cache_bounded(
            question,
            answer,
            scope=scope,
            tool_intent=tool_intent,
            tool_used=tool_used,
            metadata=metadata,
        )
    )
    _semantic_cache_tasks.add(task)
    task.add_done_callback(_semantic_cache_task_done)


def _semantic_cache_task_done(task: asyncio.Task) -> None:
    _semantic_cache_tasks.discard(task)
    if task.cancelled():
        return
    try:
        error = task.exception()
    except asyncio.CancelledError:
        return
    if error is not None:
        logger.warning("语义缓存后台写入异常: %s", error)


def start_semantic_cache_tasks() -> None:
    """Allow cache writes for a newly started application lifespan."""
    global _semantic_cache_closing
    _semantic_cache_closing = False


async def shutdown_semantic_cache_tasks(timeout_seconds: float = 5.0) -> None:
    """Drain bounded cache writes before closing their Redis client."""
    global _semantic_cache_closing
    _semantic_cache_closing = True
    pending = list(_semantic_cache_tasks)
    if not pending:
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True),
            timeout=max(0.05, timeout_seconds),
        )
    except TimeoutError:
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def _store_semantic_cache_bounded(*args, **kwargs) -> None:
    async with _semantic_cache_store_semaphore:
        try:
            await asyncio.wait_for(
                semantic_cache.store(*args, **kwargs),
                timeout=settings.SEMANTIC_CACHE_OPERATION_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning("语义缓存后台写入超时，已丢弃")


async def _wait_for_disconnect(request: Request, interval_seconds: float = 5.0) -> None:
    """Poll Starlette's disconnect signal without blocking stream production."""
    while True:
        if await request.is_disconnected():
            return
        await asyncio.sleep(interval_seconds)


async def _renew_user_stream_lease(
    tenant_id: str,
    principal: str,
    session_id: str,
    request_id: str,
) -> bool:
    while True:
        await asyncio.sleep(user_stream_lock.renew_interval_seconds)
        if not await user_stream_lock.renew(
            tenant_id, principal, session_id, request_id
        ):
            logger.warning(
                "用户 SSE 流租约续期失败 tenant=%s request=%s",
                tenant_id,
                request_id,
            )
            return False


async def _cancel_guarded_llm_request(request_id: str) -> None:
    """Release request-scoped LLM tickets when the guard module is available."""
    try:
        from ..harness import llm_guard

        cancel = getattr(llm_guard, "cancel_request", None)
        if cancel is None:
            return
        result = cancel(request_id)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # noqa: BLE001
        logger.warning("取消 LLM 请求槽位失败 request=%s: %s", request_id, exc)


async def _await_outcome_deadline(
    awaitable,
    *,
    deadline_monotonic: float,
    request_id: str,
):
    """Await one non-stream stage without resetting the absolute 60s budget."""

    task = asyncio.ensure_future(awaitable)
    remaining = max(0.0, deadline_monotonic - time.monotonic())
    done, _ = await asyncio.wait({task}, timeout=remaining)
    if task in done:
        return task.result()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await _cancel_guarded_llm_request(request_id)
    raise LLMOutcomeTimeoutError()


async def _current_degrade_payload() -> dict | None:
    """Read the current degradation snapshot without coupling to its storage stage."""
    try:
        from ..harness import llm_guard

        controller = getattr(llm_guard, "degrade_controller", None)
        getter = getattr(llm_guard, "get_degrade_controller", None)
        if controller is None and getter is not None:
            controller = getter()
            if inspect.isawaitable(controller):
                controller = await controller
        if controller is None:
            return None
        snapshot = controller.snapshot() if hasattr(controller, "snapshot") else controller
        if inspect.isawaitable(snapshot):
            snapshot = await snapshot
        if isinstance(snapshot, dict):
            level = int(snapshot.get("level", 0) or 0)
            reason = str(snapshot.get("reason") or "high_utilization")
        else:
            level = int(getattr(snapshot, "level", snapshot) or 0)
            reason = str(getattr(snapshot, "reason", "high_utilization"))
        if level <= 0:
            return None
        effects = ["thinking_disabled", "max_tokens_reduced"]
        if level >= 2:
            effects.extend(("context_trimmed", "rag_top_k_reduced"))
        return {"level": min(level, 2), "reason": reason, "effects": effects}
    except Exception as exc:  # noqa: BLE001
        logger.debug("读取降级档位失败: %s", exc)
        return None


def _guard_error_payload(exc: Exception) -> tuple[dict, str] | None:
    """Map direct LLM guard failures that escape the graph runtime."""
    if not isinstance(exc, LLMGuardError):
        return None
    payload: dict[str, int | str] = {
        "code": exc.code,
        "message": str(exc),
    }
    if exc.retry_after_ms is not None:
        payload["retry_after_ms"] = exc.retry_after_ms
    return payload, exc.reason


class _SSEGuardLifecycle:
    """Shared response-lifetime admission, disconnect, heartbeat, and cleanup."""

    def __init__(
        self,
        request: Request,
        auth: AuthContext,
        session_id: uuid.UUID,
        request_id: str,
        *,
        outcome_deadline_monotonic: float,
    ) -> None:
        self.request = request
        self.request_id = request_id
        self.tenant_id = auth.tenant.tenant_id
        self.principal = _stream_lock_principal(request, auth)
        self.session_id = str(session_id)
        self.enabled = bool(getattr(settings, "GUARD_ENABLED", True))
        self.outcome_deadline_monotonic = outcome_deadline_monotonic
        self.outcome_delivered = False
        self.cancelled = False
        self.stream_admitted = False
        self.rejection_code: int | None = None
        self.lease_lost = False
        self.lock_acquired = False
        self.gate_ticket: GuardTicket | None = None
        self.disconnect_task: asyncio.Task[None] | None = None
        self.renew_task: asyncio.Task[bool] | None = None
        self.gate_task: asyncio.Task[GuardTicket] | None = None
        self.queue_update_task: asyncio.Task[dict] | None = None
        self.operation_task: asyncio.Task | None = None
        self.operation_result = None
        self.runtime_next_task: asyncio.Task[str] | None = None
        self.heartbeat_task: asyncio.Task[None] | None = None
        self.outcome_deadline_task: asyncio.Task[None] | None = None
        self.runtime_stream = None

    def start(self) -> None:
        if self.disconnect_task is None:
            self.disconnect_task = asyncio.create_task(
                _wait_for_disconnect(self.request)
            )
        if self.outcome_deadline_task is None and not self.outcome_delivered:
            self.outcome_deadline_task = asyncio.create_task(
                asyncio.sleep(
                    max(0.0, self.outcome_deadline_monotonic - time.monotonic())
                )
            )

    def mark_outcome_delivered(self) -> None:
        """Stop the first-outcome clock and release later tool-loop LLM calls."""
        if self.outcome_delivered:
            return
        self.outcome_delivered = True
        if self.outcome_deadline_task is not None:
            self.outcome_deadline_task.cancel()
        context = get_tool_context()
        if context.request_id == self.request_id:
            context.outcome_deadline_monotonic = None

    async def raise_outcome_deadline(self) -> None:
        """Abort work that missed the first-outcome SLA without calling it a disconnect."""

        error = LLMOutcomeTimeoutError()
        self.rejection_code = error.code
        metrics.record_guard_rejected(error.reason)
        self.mark_outcome_delivered()
        # Cancelling the graph task alone cannot stop a synchronous model call
        # already running in an executor. Release its request-scoped ticket and
        # leave a cancellation marker so the orphan cannot retry or take a new
        # slot after this stream has returned 50401.
        await _cancel_guarded_llm_request(self.request_id)
        if self.gate_ticket is not None:
            stream_gate.release(self.gate_ticket)
            self.gate_ticket = None
            self.stream_admitted = False
            metrics.set_guard_stream_inflight(stream_gate.inflight)
            metrics.set_guard_queue_length(stream_gate.queue_size)
        if self.lock_acquired:
            await user_stream_lock.release(
                self.tenant_id,
                self.principal,
                self.session_id,
                self.request_id,
            )
            self.lock_acquired = False
        if self.renew_task is not None and not self.renew_task.done():
            self.renew_task.cancel()
            await asyncio.gather(self.renew_task, return_exceptions=True)
        self.renew_task = None
        error.rejection_recorded = True
        raise error

    async def acquire_user(self) -> dict | None:
        """Acquire the per-principal lease before any persistent or expensive work."""
        self.start()
        if not self.enabled:
            return None
        self.lock_acquired = await user_stream_lock.acquire(
            self.tenant_id,
            self.principal,
            self.session_id,
            self.request_id,
        )
        if not self.lock_acquired:
            self.rejection_code = 40901
            metrics.record_guard_rejected("session_lock")
            return {
                "code": 40901,
                "message": "当前会话已有任务正在进行中",
                "retry_after_ms": 5000,
            }
        self.renew_task = asyncio.create_task(
            _renew_user_stream_lease(
                self.tenant_id,
                self.principal,
                self.session_id,
                self.request_id,
            )
        )
        if self.disconnect_task is not None and self.disconnect_task.done():
            await self.mark_cancelled()
        return None

    async def acquire_stream(self) -> AsyncIterator[str]:
        """Yield queue/error frames in real time and set ``stream_admitted``."""
        self.start()
        if self.cancelled:
            return
        if not self.enabled:
            self.stream_admitted = True
            return

        remaining = self.outcome_deadline_monotonic - time.monotonic()
        if remaining <= 0:
            await self.raise_outcome_deadline()
        queue_timeout = min(
            float(
                getattr(
                    stream_gate,
                    "queue_timeout_seconds",
                    settings.QUEUE_TIMEOUT_SECONDS,
                )
            ),
            remaining,
        )

        queue_updates: asyncio.Queue[dict] = asyncio.Queue()
        queue_started_monotonic: float | None = None

        def record_terminal_queue_wait() -> None:
            """Observe rejected queue waits as well as successfully admitted waits."""
            nonlocal queue_started_monotonic
            if queue_started_monotonic is None:
                return
            metrics.record_guard_queue_wait(
                max(0.0, time.monotonic() - queue_started_monotonic) * 1000
            )
            queue_started_monotonic = None

        async def on_position(
            position: int,
            queue_size: int,
            estimated_wait_ms: int,
        ) -> None:
            nonlocal queue_started_monotonic
            if queue_started_monotonic is None:
                queue_started_monotonic = time.monotonic()
            metrics.set_guard_queue_length(queue_size)
            await queue_updates.put(
                {
                    "position": position,
                    "queue_size": queue_size,
                    "estimated_wait_ms": estimated_wait_ms,
                }
            )

        self.gate_task = asyncio.create_task(
            stream_gate.acquire(
                on_position,
                timeout_seconds=queue_timeout,
            )
        )
        self.queue_update_task = asyncio.create_task(queue_updates.get())
        disconnect_task = self.disconnect_task
        if disconnect_task is None:  # pragma: no cover - start() invariant
            raise RuntimeError("disconnect watcher was not started")
        while not self.gate_task.done():
            waiters = {self.gate_task, self.queue_update_task, disconnect_task}
            if self.renew_task is not None:
                waiters.add(self.renew_task)
            if self.outcome_deadline_task is not None and not self.outcome_delivered:
                waiters.add(self.outcome_deadline_task)
            done, _ = await asyncio.wait(
                waiters,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if disconnect_task in done:
                await self.mark_cancelled()
                self.gate_task.cancel()
                await asyncio.gather(self.gate_task, return_exceptions=True)
                metrics.set_guard_queue_length(stream_gate.queue_size)
                return
            if self.renew_task in done and self._renewal_was_lost():
                self.gate_task.cancel()
                await asyncio.gather(self.gate_task, return_exceptions=True)
                metrics.set_guard_queue_length(stream_gate.queue_size)
                yield await self.handle_lease_lost()
                return
            if not self.outcome_delivered and self.outcome_deadline_task in done:
                self.gate_task.cancel()
                await asyncio.gather(self.gate_task, return_exceptions=True)
                metrics.set_guard_queue_length(stream_gate.queue_size)
                record_terminal_queue_wait()
                await self.raise_outcome_deadline()
            if self.queue_update_task in done:
                yield _sse_frame("queue", self.queue_update_task.result())
                self.queue_update_task = asyncio.create_task(queue_updates.get())
        if self.queue_update_task.done():
            yield _sse_frame("queue", self.queue_update_task.result())
        self.queue_update_task.cancel()
        await asyncio.gather(self.queue_update_task, return_exceptions=True)
        self.queue_update_task = None
        try:
            self.gate_ticket = self.gate_task.result()
        except StreamQueueFullError:
            self.rejection_code = 42900
            metrics.record_guard_rejected("queue_full")
            self.mark_outcome_delivered()
            yield _sse_frame(
                "error",
                {
                    "code": 42900,
                    "message": "系统繁忙，请稍后重试",
                    "retry_after_ms": max(
                        1000,
                        int(
                            getattr(settings, "QUEUE_TIMEOUT_SECONDS", 30.0)
                            * 1000
                        ),
                    ),
                },
            )
            return
        except StreamQueueTimeoutError:
            self.rejection_code = 50400
            metrics.record_guard_rejected("queue_timeout")
            record_terminal_queue_wait()
            self.mark_outcome_delivered()
            yield _sse_frame(
                "error",
                {
                    "code": 50400,
                    "message": "当前使用人数较多，请稍后重试",
                    "retry_after_ms": 5000,
                },
            )
            return
        finally:
            self.gate_task = None
            metrics.set_guard_queue_length(stream_gate.queue_size)
        self.stream_admitted = True
        metrics.set_guard_stream_inflight(stream_gate.inflight)
        metrics.set_guard_queue_length(stream_gate.queue_size)
        if self.gate_ticket.queued:
            metrics.record_guard_queue_wait(self.gate_ticket.wait_ms)
            queue_started_monotonic = None

    async def iter_operation(self, awaitable) -> AsyncIterator[str]:
        """Wait for pre-runtime async work with heartbeat/disconnect semantics."""
        self.start()
        disconnect_task = self.disconnect_task
        if disconnect_task is None:  # pragma: no cover - start() invariant
            raise RuntimeError("disconnect watcher was not started")
        self.operation_result = None
        self.operation_task = asyncio.create_task(awaitable)
        self.heartbeat_task = asyncio.create_task(
            asyncio.sleep(SSE_HEARTBEAT_INTERVAL_SECONDS)
        )
        try:
            while not self.operation_task.done():
                waiters = {
                    self.operation_task,
                    disconnect_task,
                    self.heartbeat_task,
                }
                if self.renew_task is not None:
                    waiters.add(self.renew_task)
                if self.outcome_deadline_task is not None and not self.outcome_delivered:
                    waiters.add(self.outcome_deadline_task)
                done, _ = await asyncio.wait(
                    waiters,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if disconnect_task in done:
                    await self.mark_cancelled()
                    self.operation_task.cancel()
                    await asyncio.gather(
                        self.operation_task,
                        return_exceptions=True,
                    )
                    return
                if self.renew_task in done and self._renewal_was_lost():
                    self.operation_task.cancel()
                    await asyncio.gather(
                        self.operation_task,
                        return_exceptions=True,
                    )
                    yield await self.handle_lease_lost()
                    return
                if not self.outcome_delivered and self.outcome_deadline_task in done:
                    self.operation_task.cancel()
                    await asyncio.gather(
                        self.operation_task,
                        return_exceptions=True,
                    )
                    await self.raise_outcome_deadline()
                if self.heartbeat_task in done:
                    yield ": keep-alive\n\n"
                    self.heartbeat_task = asyncio.create_task(
                        asyncio.sleep(SSE_HEARTBEAT_INTERVAL_SECONDS)
                    )
            self.operation_result = self.operation_task.result()
        finally:
            for task in (self.operation_task, self.heartbeat_task):
                if task is not None and not task.done():
                    task.cancel()
            tasks = [
                task
                for task in (self.operation_task, self.heartbeat_task)
                if task is not None
            ]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self.operation_task = None
            self.heartbeat_task = None

    async def iter_runtime(self, runtime_stream) -> AsyncIterator[str]:
        """Forward runtime frames with heartbeats and disconnect cancellation."""
        self.runtime_stream = runtime_stream
        disconnect_task = self.disconnect_task
        if disconnect_task is None:  # pragma: no cover - start() invariant
            raise RuntimeError("disconnect watcher was not started")
        self.runtime_next_task = asyncio.create_task(anext(runtime_stream))
        self.heartbeat_task = asyncio.create_task(
            asyncio.sleep(SSE_HEARTBEAT_INTERVAL_SECONDS)
        )
        try:
            while self.runtime_next_task is not None:
                waiters = {
                    self.runtime_next_task,
                    disconnect_task,
                    self.heartbeat_task,
                }
                if self.renew_task is not None:
                    waiters.add(self.renew_task)
                if self.outcome_deadline_task is not None and not self.outcome_delivered:
                    waiters.add(self.outcome_deadline_task)
                done, _ = await asyncio.wait(
                    waiters,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if disconnect_task in done:
                    await self.mark_cancelled()
                    self.runtime_next_task.cancel()
                    await asyncio.gather(
                        self.runtime_next_task,
                        return_exceptions=True,
                    )
                    self.runtime_next_task = None
                    break
                if self.renew_task in done and self._renewal_was_lost():
                    self.runtime_next_task.cancel()
                    await asyncio.gather(
                        self.runtime_next_task,
                        return_exceptions=True,
                    )
                    self.runtime_next_task = None
                    yield await self.handle_lease_lost()
                    break
                if self.runtime_next_task in done:
                    try:
                        frame = self.runtime_next_task.result()
                    except StopAsyncIteration:
                        self.runtime_next_task = None
                        if not self.outcome_delivered:
                            self.mark_outcome_delivered()
                            yield _sse_frame(
                                "error",
                                {
                                    "code": 50002,
                                    "message": "执行未生成回复，请稍后重试",
                                    "retry_after_ms": 1000,
                                },
                            )
                        break
                    event_name = _sse_event_name(frame)
                    is_outcome = event_name in {
                        "message_delta",
                        "error",
                    } or is_first_feedback_event_name(event_name)
                    is_terminal = "event: run_end" in frame
                    if is_outcome:
                        self.mark_outcome_delivered()
                    elif is_terminal and not self.outcome_delivered:
                        self.mark_outcome_delivered()
                        self.runtime_next_task = None
                        yield _sse_frame(
                            "error",
                            {
                                "code": 50002,
                                "message": "执行未生成回复，请稍后重试",
                                "retry_after_ms": 1000,
                            },
                        )
                        break
                    elif not self.outcome_delivered and self.outcome_deadline_task in done:
                        await self.raise_outcome_deadline()
                    yield frame
                    self.runtime_next_task = asyncio.create_task(anext(runtime_stream))
                elif not self.outcome_delivered and self.outcome_deadline_task in done:
                    self.runtime_next_task.cancel()
                    await asyncio.gather(
                        self.runtime_next_task,
                        return_exceptions=True,
                    )
                    self.runtime_next_task = None
                    await self.raise_outcome_deadline()
                if self.heartbeat_task in done:
                    yield ": keep-alive\n\n"
                    self.heartbeat_task = asyncio.create_task(
                        asyncio.sleep(SSE_HEARTBEAT_INTERVAL_SECONDS)
                    )
        finally:
            for task in (self.runtime_next_task, self.heartbeat_task):
                if task is not None and not task.done():
                    task.cancel()
            tasks = [
                task
                for task in (self.runtime_next_task, self.heartbeat_task)
                if task is not None
            ]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await runtime_stream.aclose()
            except (RuntimeError, asyncio.CancelledError):
                pass
            self.runtime_next_task = None
            self.heartbeat_task = None
            self.runtime_stream = None

    def _renewal_was_lost(self) -> bool:
        task = self.renew_task
        if task is None or not task.done() or task.cancelled():
            return False
        try:
            return task.result() is False
        except Exception:  # noqa: BLE001
            return True

    async def handle_lease_lost(self) -> str:
        self.lease_lost = True
        self.rejection_code = 40901
        metrics.record_guard_rejected("session_lock")
        await self.mark_cancelled()
        return _sse_frame(
            "error",
            {
                "code": 40901,
                "message": "对话租约已失效，请重新发送",
                "retry_after_ms": 1000,
            },
        )

    async def mark_cancelled(self) -> None:
        if self.cancelled:
            return
        self.cancelled = True
        metrics.record_stream_cancelled()
        if self.gate_ticket is not None:
            stream_gate.release(self.gate_ticket)
            self.gate_ticket = None
            self.stream_admitted = False
            metrics.set_guard_stream_inflight(stream_gate.inflight)
            metrics.set_guard_queue_length(stream_gate.queue_size)
        else:
            metrics.set_guard_stream_inflight(stream_gate.inflight)
            metrics.set_guard_queue_length(stream_gate.queue_size)
        if self.lock_acquired:
            await user_stream_lock.release(
                self.tenant_id,
                self.principal,
                self.session_id,
                self.request_id,
            )
            self.lock_acquired = False
        await _cancel_guarded_llm_request(self.request_id)

    async def close(self) -> None:
        for task in (
            self.runtime_next_task,
            self.heartbeat_task,
            self.gate_task,
            self.queue_update_task,
            self.operation_task,
            self.renew_task,
            self.outcome_deadline_task,
        ):
            if task is not None and not task.done():
                task.cancel()
        tasks = [
            task
            for task in (
                self.runtime_next_task,
                self.heartbeat_task,
                self.gate_task,
                self.queue_update_task,
                self.operation_task,
                self.renew_task,
                self.outcome_deadline_task,
            )
            if task is not None
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.runtime_stream is not None:
            try:
                await self.runtime_stream.aclose()
            except (RuntimeError, asyncio.CancelledError):
                pass
        if self.gate_ticket is not None:
            stream_gate.release(self.gate_ticket)
            self.gate_ticket = None
            metrics.set_guard_stream_inflight(stream_gate.inflight)
            metrics.set_guard_queue_length(stream_gate.queue_size)
        if self.lock_acquired:
            await user_stream_lock.release(
                self.tenant_id,
                self.principal,
                self.session_id,
                self.request_id,
            )
            self.lock_acquired = False
        if self.disconnect_task is not None:
            self.disconnect_task.cancel()
            await asyncio.gather(self.disconnect_task, return_exceptions=True)
        self.disconnect_task = None
        self.outcome_deadline_task = None


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
    body: MessageSendRequest | MessageStreamRequest | ResumeRequest,
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


def _body_agent_id(
    body: MessageSendRequest | MessageStreamRequest | ResumeRequest,
) -> uuid.UUID | None:
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
    body: MessageSendRequest | MessageStreamRequest | ResumeRequest,
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
    body: MessageSendRequest | MessageStreamRequest | ResumeRequest,
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
    body: MessageSendRequest | MessageStreamRequest | ResumeRequest,
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
    request: Request,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """发送消息（非流式，返回完整回复）。"""
    start = time.monotonic()
    outcome_deadline_monotonic = start + float(
        settings.GUARD_OUTCOME_DEADLINE_SECONDS
    )
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    sess = await _await_outcome_deadline(
        repo.get_session(db, session_id),
        deadline_monotonic=outcome_deadline_monotonic,
        request_id=request_id,
    )
    if sess is None or sess.tenant_id != auth.tenant.tenant_id or sess.status == "deleted":
        raise HTTPException(status_code=404, detail="session not found")

    if harness_app.runtime is None:
        raise HTTPException(status_code=503, detail="harness not ready")
    if harness_app.runtime.checkpointer is not None:
        pending = await harness_app.runtime.pending_approval(
            sess.thread_id,
            auth.tenant.tenant_id,
        )
        if pending is not None:
            raise HTTPException(
                status_code=409,
                detail="当前会话有待审批的工具调用，请先批准或拒绝",
            )
    has_session_history = bool(
        await _await_outcome_deadline(
            repo.list_messages(db, session_id, limit=1),
            deadline_monotonic=outcome_deadline_monotonic,
            request_id=request_id,
        )
    )

    resolved_agent = await _await_outcome_deadline(
        _resolve_request_agent(
            db, tenant_id=auth.tenant.tenant_id, session=sess, body=body
        ),
        deadline_monotonic=outcome_deadline_monotonic,
        request_id=request_id,
    )
    model_id, model_base_url, model_api_key, attachments = _request_model_and_attachments(
        body=body,
        tenant_id=auth.tenant.tenant_id,
        session_id=session_id,
        agent=resolved_agent,
    )

    await _await_outcome_deadline(
        _ensure_session_title(db, sess, body.message),
        deadline_monotonic=outcome_deadline_monotonic,
        request_id=request_id,
    )
    user_message_row = await _await_outcome_deadline(
        repo.add_message(
            db,
            session_id=session_id,
            role="user",
            content=body.message,
            request_id=request_id,
            step_index=1,
        ),
        deadline_monotonic=outcome_deadline_monotonic,
        request_id=request_id,
    )
    skill_names = _resolve_skill_names(body, resolved_agent)
    kb_names = body.knowledge_bases or []

    # RAG 上下文注入：解析会话绑定的知识库白名单，供 knowledge_search 工具读取
    requested_kb_ids = await _await_outcome_deadline(
        knowledge_service.resolve_ids_by_names(
            db, tenant_id=auth.tenant.tenant_id, names=kb_names
        ),
        deadline_monotonic=outcome_deadline_monotonic,
        request_id=request_id,
    )
    if resolved_agent.active and resolved_agent.allowed_kb_ids:
        allowed_kb_ids = (
            resolved_agent.allowed_kb_ids
            if body.knowledge_bases is None
            else [
                value
                for value in requested_kb_ids
                if value in set(resolved_agent.allowed_kb_ids)
            ]
        )
    else:
        allowed_kb_ids = requested_kb_ids
    # 长期记忆自动注入：对话前检索当前用户记忆（MEMORY_SPEC §6.2 方式 A）
    memory_context = ""
    if resolved_agent.memory_enabled:
        memory_context = await _await_outcome_deadline(
            _retrieve_memory_context(
                db,
                tenant_id=auth.tenant.tenant_id,
                user_id=auth.user_id,
                query=body.message,
            ),
            deadline_monotonic=outcome_deadline_monotonic,
            request_id=request_id,
        )
    requested_mcp = _resolve_mcp_ids(body, resolved_agent)
    dynamic_tools = await _await_outcome_deadline(
        mcp_manager.resolve_tools(
            db,
            tenant_id=auth.tenant.tenant_id,
            requested_capability_ids=requested_mcp,
        ),
        deadline_monotonic=outcome_deadline_monotonic,
        request_id=request_id,
    )
    orchestration_agents = await _await_outcome_deadline(
        _resolve_orchestration_context(db, tenant_id=auth.tenant.tenant_id),
        deadline_monotonic=outcome_deadline_monotonic,
        request_id=request_id,
    )
    cache_scope = _semantic_cache_scope(
        request,
        auth,
        resolved_agent=resolved_agent,
        model_id=model_id,
        model_base_url=model_base_url,
        model_api_key=model_api_key,
        body=body,
        skill_names=skill_names,
    )
    cache_tool_intent = _semantic_cache_tool_intent(
        body,
        resolved_agent=resolved_agent,
        skill_names=skill_names,
        dynamic_tools=dynamic_tools,
        orchestration_agents=orchestration_agents,
        memory_context=memory_context,
        allowed_kb_ids=allowed_kb_ids,
        has_session_history=has_session_history,
    )
    cache_hit = await _await_outcome_deadline(
        semantic_cache.lookup(
            body.message,
            scope=cache_scope,
            tool_intent=cache_tool_intent,
            has_attachments=bool(attachments),
        ),
        deadline_monotonic=outcome_deadline_monotonic,
        request_id=request_id,
    )
    if cache_hit is not None:
        duration_ms = int((time.monotonic() - start) * 1000)
        token_usage = {"prompt": 0, "completion": 0, "total": 0}
        await _await_outcome_deadline(
            repo.add_message(
                db,
                session_id=session_id,
                role="assistant",
                content=cache_hit.answer,
                token_usage=token_usage,
                request_id=request_id,
                step_index=2,
                agent_id=resolved_agent.agent_id,
            ),
            deadline_monotonic=outcome_deadline_monotonic,
            request_id=request_id,
        )
        await _await_outcome_deadline(
            audit.audit(
                auth.tenant.tenant_id,
                "chat.send",
                user_id=auth.user_id,
                session_id=session_id,
                detail={
                    "model": model_id,
                    "tool_calls": 0,
                    "agent_id": (
                        str(resolved_agent.agent_id)
                        if resolved_agent.agent_id
                        else None
                    ),
                    **cache_hit.response_metadata,
                },
            ),
            deadline_monotonic=outcome_deadline_monotonic,
            request_id=request_id,
        )
        metrics.record_request(
            auth.tenant.tenant_id,
            ok=True,
            duration_ms=duration_ms,
            token_usage=token_usage,
            agent_id=(
                str(resolved_agent.agent_id) if resolved_agent.agent_id else None
            ),
        )
        return ApiResponse(
            data=MessageResponse(
                session_id=str(session_id),
                reply=cache_hit.answer,
                token_usage=token_usage,
                tool_calls=0,
                duration_ms=duration_ms,
                cache_hit=True,
            )
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
            allowed_skill_names=(
                resolved_agent.allowed_skill_names if resolved_agent.active else None
            ),
            allowed_tool_names=(
                resolved_agent.allowed_tool_names if resolved_agent.active else None
            ),
            allowed_capability_ids=requested_mcp,
            memory_enabled=resolved_agent.memory_enabled,
            orchestration_agents=orchestration_agents,
            session_id=session_id,
            message_id=user_message_row.id,
            request_id=request_id,
            outcome_deadline_monotonic=outcome_deadline_monotonic,
        )
    )
    try:
        result = await _await_outcome_deadline(
            harness_app.runtime.ainvoke(
                sess.thread_id,
                body.message,
                auth.tenant.tenant_id,
                auth.user_id,
                skill_names,
                memory_context,
                session_id=str(session_id),
                request_id=request_id,
                message_id=user_message_row.id,
            ),
            deadline_monotonic=outcome_deadline_monotonic,
            request_id=request_id,
        )
    finally:
        reset_tool_context()

    obs = result.get("_observability") or {}
    if obs.get("status", "ok") != "ok":
        error_code = int(obs.get("error_code") or 50001)
        duration_ms = int((time.monotonic() - start) * 1000)
        trace_id = obs.get("trace_id")
        if trace_id:
            await _await_outcome_deadline(
                repo.link_messages_to_trace(
                    db,
                    request_id=request_id,
                    trace_id=trace_id,
                ),
                deadline_monotonic=outcome_deadline_monotonic,
                request_id=request_id,
            )
        metrics.record_request(
            auth.tenant.tenant_id,
            ok=False,
            duration_ms=duration_ms,
            agent_id=(
                str(resolved_agent.agent_id) if resolved_agent.agent_id else None
            ),
        )
        await _await_outcome_deadline(
            audit.audit(
                auth.tenant.tenant_id,
                "chat.send",
                user_id=auth.user_id,
                session_id=session_id,
                detail={
                    "model": model_id,
                    "agent_id": (
                        str(resolved_agent.agent_id)
                        if resolved_agent.agent_id
                        else None
                    ),
                    "status": "error",
                    "error_code": error_code,
                    "cache_hit": False,
                },
            ),
            deadline_monotonic=outcome_deadline_monotonic,
            request_id=request_id,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "code": error_code,
                "message": (
                    "执行未生成回复，请稍后重试"
                    if error_code == 50002
                    else "模型执行失败，请稍后重试"
                ),
                "retry_after_ms": 1_000,
            },
        )

    # 提取最终回复
    reply = _extract_final_answer(result, body.message)
    duration_ms = int((time.monotonic() - start) * 1000)
    trace_id = obs.get("trace_id")
    if trace_id:
        await _await_outcome_deadline(
            repo.link_messages_to_trace(
                db,
                request_id=request_id,
                trace_id=trace_id,
            ),
            deadline_monotonic=outcome_deadline_monotonic,
            request_id=request_id,
        )
    routed_name = (result.get("router_decision") or {}).get("target_agent")
    routed_agent = orchestration_agents.get(routed_name) if routed_name else None
    await _await_outcome_deadline(
        repo.add_message(
            db,
            session_id=session_id,
            role="assistant",
            content=reply,
            token_usage=obs.get("token_usage"),
            trace_id=trace_id,
            request_id=request_id,
            step_index=2,
            agent_id=routed_agent.agent_id if routed_agent else resolved_agent.agent_id,
        ),
        deadline_monotonic=outcome_deadline_monotonic,
        request_id=request_id,
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
    if run_ok:
        _schedule_semantic_cache_store(
            body.message,
            reply,
            scope=cache_scope,
            tool_intent=cache_tool_intent,
            tool_used=tool_calls_count > 0,
            metadata={
                "model": model_id,
                "agent_id": (
                    str(resolved_agent.agent_id) if resolved_agent.agent_id else None
                ),
            },
        )
    await _await_outcome_deadline(
        audit.audit(
            auth.tenant.tenant_id,
            "chat.send",
            user_id=auth.user_id,
            session_id=session_id,
            detail={
                "model": model_id,
                "tool_calls": tool_calls_count,
                "agent_id": (
                    str(resolved_agent.agent_id)
                    if resolved_agent.agent_id
                    else None
                ),
                "cache_hit": False,
            },
        ),
        deadline_monotonic=outcome_deadline_monotonic,
        request_id=request_id,
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
            cache_hit=False,
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

    ChatRun 启用后，该端点作为“创建 Run 后立即订阅”的兼容适配器；断开只
    关闭 Subscriber。关闭功能开关时保留旧式响应生命周期语义。
    """
    if settings.CHAT_RUN_ENABLED:
        from . import router_runs

        created = await router_runs.create_run(
            session_id,
            ChatRunCreateRequest.model_validate(body.model_dump()),
            request,
            request.headers.get("Idempotency-Key") or str(uuid.uuid4()),
            auth,
            db,
        )
        if not isinstance(created, ApiResponse):
            return created
        run_id = uuid.UUID(str(created.data["run_id"]))
        return await router_runs.subscribe_events(
            run_id,
            request,
            0,
            None,
            auth,
            db,
        )
    request_started_at = time.monotonic()
    outcome_deadline_monotonic = request_started_at + float(
        settings.GUARD_OUTCOME_DEADLINE_SECONDS
    )
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    tenant_id = auth.tenant.tenant_id
    user_id = auth.user_id
    client_ip = get_client_ip(request)

    trace_id: str | None = None
    routed_agent_name: str | None = None
    user_message_row = None

    async def persist_user_message(sess):
        nonlocal user_message_row
        if user_message_row is None:
            await _ensure_session_title(db, sess, body.message)
            user_message_row = await repo.add_message(
                db,
                session_id=session_id,
                role="user",
                content=body.message,
                request_id=request_id,
                step_index=1,
            )
        return user_message_row

    async def prepare_stream_preflight():
        """Resolve the session and all capabilities inside the response-lifetime race."""
        sess = await repo.get_session(db, session_id)
        if (
            sess is None
            or sess.tenant_id != tenant_id
            or sess.status == "deleted"
        ):
            raise HTTPException(status_code=404, detail="session not found")
        if harness_app.runtime is None:
            raise HTTPException(status_code=503, detail="harness not ready")
        if body.approval_mode == "manual" and harness_app.runtime.checkpointer is None:
            raise HTTPException(
                status_code=503,
                detail="请求审批模式需要可用的会话检查点服务",
            )
        if harness_app.runtime.checkpointer is not None:
            pending = await harness_app.runtime.pending_approval(sess.thread_id, tenant_id)
            if pending is not None:
                raise HTTPException(
                    status_code=409,
                    detail="当前会话有待审批的工具调用，请先批准或拒绝",
                )

        resolved_agent = await _resolve_request_agent(
            db,
            tenant_id=tenant_id,
            session=sess,
            body=body,
        )
        model_id, model_base_url, model_api_key, attachments = (
            _request_model_and_attachments(
                body=body,
                tenant_id=tenant_id,
                session_id=session_id,
                agent=resolved_agent,
            )
        )
        skill_names = _resolve_skill_names(body, resolved_agent)
        kb_names = body.knowledge_bases or []
        has_session_history = bool(
            await repo.list_messages(db, session_id, limit=1)
        )
        cache_scope = _semantic_cache_scope(
            request,
            auth,
            resolved_agent=resolved_agent,
            model_id=model_id,
            model_base_url=model_base_url,
            model_api_key=model_api_key,
            body=body,
            skill_names=skill_names,
        )
        requested_kb_ids = await knowledge_service.resolve_ids_by_names(
            db,
            tenant_id=tenant_id,
            names=kb_names,
        )
        if resolved_agent.active and resolved_agent.allowed_kb_ids:
            allowed_kb_ids = (
                resolved_agent.allowed_kb_ids
                if body.knowledge_bases is None
                else [
                    value
                    for value in requested_kb_ids
                    if value in set(resolved_agent.allowed_kb_ids)
                ]
            )
        else:
            allowed_kb_ids = requested_kb_ids
        memory_context = ""
        if resolved_agent.memory_enabled:
            memory_context = await _retrieve_memory_context(
                db,
                tenant_id=tenant_id,
                user_id=user_id,
                query=body.message,
            )
        requested_mcp = _resolve_mcp_ids(body, resolved_agent)
        dynamic_tools = await mcp_manager.resolve_tools(
            db,
            tenant_id=tenant_id,
            requested_capability_ids=requested_mcp,
        )
        orchestration_agents = await _resolve_orchestration_context(
            db,
            tenant_id=tenant_id,
        )
        cache_tool_intent = _semantic_cache_tool_intent(
            body,
            resolved_agent=resolved_agent,
            skill_names=skill_names,
            dynamic_tools=dynamic_tools,
            orchestration_agents=orchestration_agents,
            memory_context=memory_context,
            allowed_kb_ids=allowed_kb_ids,
            has_session_history=has_session_history,
        )
        return {
            "sess": sess,
            "resolved_agent": resolved_agent,
            "model_id": model_id,
            "model_base_url": model_base_url,
            "model_api_key": model_api_key,
            "attachments": attachments,
            "skill_names": skill_names,
            "thread_id": sess.thread_id,
            "cache_scope": cache_scope,
            "allowed_kb_ids": allowed_kb_ids,
            "memory_context": memory_context,
            "requested_mcp": requested_mcp,
            "dynamic_tools": dynamic_tools,
            "orchestration_agents": orchestration_agents,
            "cache_tool_intent": cache_tool_intent,
        }

    async def event_stream():
        nonlocal trace_id, routed_agent_name
        reply_parts: list[str] = []
        stream_start = request_started_at
        run_status = "ok"
        cancelled = False
        cache_hit = False
        cache_similarity: float | None = None
        tool_calls_count = 0
        session_opened = False
        context_set = False
        guard = _SSEGuardLifecycle(
            request,
            auth,
            session_id,
            request_id,
            outcome_deadline_monotonic=outcome_deadline_monotonic,
        )
        sess = None
        resolved_agent = ResolvedAgentCapabilities()
        model_id = body.model or settings.LLM_MODEL
        model_base_url = None
        model_api_key = None
        attachments = []
        skill_names = []
        thread_id = ""
        cache_scope = None
        allowed_kb_ids = []
        memory_context = ""
        requested_mcp = None
        dynamic_tools = []
        orchestration_agents = {}
        cache_tool_intent = True

        try:
            user_error = await guard.acquire_user()
            if user_error is not None:
                run_status = "error"
                yield _sse_frame("error", user_error)
                return
            if guard.cancelled:
                run_status = "canceled"
                return

            # Session lookup and every potentially slow preparation step share
            # the same absolute deadline, heartbeat, disconnect, and lease race.
            async for wait_frame in guard.iter_operation(prepare_stream_preflight()):
                yield wait_frame
            if guard.cancelled:
                cancelled = True
                run_status = "canceled"
                return
            prepared = guard.operation_result
            sess = prepared["sess"]
            resolved_agent = prepared["resolved_agent"]
            model_id = prepared["model_id"]
            model_base_url = prepared["model_base_url"]
            model_api_key = prepared["model_api_key"]
            attachments = prepared["attachments"]
            skill_names = prepared["skill_names"]
            thread_id = prepared["thread_id"]
            cache_scope = prepared["cache_scope"]
            allowed_kb_ids = prepared["allowed_kb_ids"]
            memory_context = prepared["memory_context"]
            requested_mcp = prepared["requested_mcp"]
            dynamic_tools = prepared["dynamic_tools"]
            orchestration_agents = prepared["orchestration_agents"]
            cache_tool_intent = prepared["cache_tool_intent"]

            # Cache is independent from GUARD_ENABLED and bypasses stream/runtime
            # admission only after the principal lease has been acquired.
            async for wait_frame in guard.iter_operation(
                semantic_cache.lookup(
                    body.message,
                    scope=cache_scope,
                    tool_intent=cache_tool_intent,
                    has_attachments=bool(attachments),
                )
            ):
                yield wait_frame
            if guard.cancelled:
                cancelled = True
                run_status = "canceled"
                return
            semantic_hit = guard.operation_result
            if semantic_hit is not None:
                async for wait_frame in guard.iter_operation(
                    persist_user_message(sess)
                ):
                    yield wait_frame
                if guard.cancelled:
                    cancelled = True
                    run_status = "canceled"
                    return
                cache_hit = True
                cache_similarity = semantic_hit.similarity
                reply_parts.append(semantic_hit.answer)
                guard.mark_outcome_delivered()
                cache_duration_ms = int((time.monotonic() - stream_start) * 1000)
                cache_metadata = {
                    "cache_hit": True,
                    "semantic_cache_similarity": round(
                        semantic_hit.similarity,
                        6,
                    ),
                }
                yield _sse_frame(
                    "session_start",
                    {
                        "session_id": str(session_id),
                        "thread_id": thread_id,
                        **cache_metadata,
                    },
                )
                yield _sse_frame(
                    "message_start",
                    {"id": "msg_cache", "role": "assistant", **cache_metadata},
                )
                yield _sse_frame(
                    "message_delta",
                    {
                        "id": "msg_cache",
                        "content": semantic_hit.answer,
                        **cache_metadata,
                    },
                )
                yield _sse_frame(
                    "message_end",
                    {
                        "id": "msg_cache",
                        "token_usage": {"input": 0, "output": 0, "total": 0},
                        **cache_metadata,
                    },
                )
                yield _sse_frame(
                    "run_end",
                    {
                        "status": "success",
                        "duration_ms": cache_duration_ms,
                        "tool_calls": 0,
                        **cache_metadata,
                    },
                )
                return

            async for admission_frame in guard.acquire_stream():
                yield admission_frame
            if not guard.stream_admitted:
                run_status = "canceled" if guard.cancelled else "error"
                return
            async for wait_frame in guard.iter_operation(persist_user_message(sess)):
                yield wait_frame
            if guard.cancelled:
                cancelled = True
                run_status = "canceled"
                return

            metrics.session_open(str(session_id))
            session_opened = True
            # The runtime advances in child tasks, so set ContextVar before the
            # first __anext__ task copies this request context.
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
                    allowed_skill_names=(
                        resolved_agent.allowed_skill_names
                        if resolved_agent.active
                        else None
                    ),
                    allowed_tool_names=(
                        resolved_agent.allowed_tool_names
                        if resolved_agent.active
                        else None
                    ),
                    allowed_capability_ids=requested_mcp,
                    memory_enabled=resolved_agent.memory_enabled,
                    orchestration_agents=orchestration_agents,
                    session_id=session_id,
                    message_id=user_message_row.id,
                    request_id=request_id,
                    outcome_deadline_monotonic=outcome_deadline_monotonic,
                )
            )
            context_set = True

            async for wait_frame in guard.iter_operation(
                _current_degrade_payload()
            ):
                yield wait_frame
            if guard.cancelled:
                cancelled = True
                run_status = "canceled"
                return
            degrade_payload = guard.operation_result
            if degrade_payload is not None:
                yield _sse_frame("degrade", degrade_payload)

            runtime_stream = harness_app.runtime.astream(
                thread_id,
                body.message,
                tenant_id,
                user_id,
                skill_names,
                session_id=str(session_id),
                memory_context=memory_context,
                request_id=request_id,
                message_id=user_message_row.id,
                approval_mode=body.approval_mode,
            )
            async for frame in guard.iter_runtime(runtime_stream):
                if "event: error" in frame:
                    run_status = "error"
                    try:
                        data = next(
                            (
                                line[6:]
                                for line in frame.splitlines()
                                if line.startswith("data: ")
                            ),
                            "{}",
                        )
                        error_code = int(json.loads(data).get("code") or 0)
                        if error_code in {42900, 42901, 50300, 50400, 50401, 50402}:
                            guard.rejection_code = error_code
                    except (ValueError, json.JSONDecodeError):
                        pass
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
                        routed_agent_name = (
                            json.loads(data).get("target_agent") or routed_agent_name
                        )
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
                        status = str(payload.get("status") or "error")
                        run_status = "ok" if status == "success" else status
                        tool_calls_count = int(payload.get("tool_calls") or 0)
                    except json.JSONDecodeError:
                        pass
                delta = _extract_stream_delta(frame)
                if delta:
                    reply_parts.append(delta)
                yield frame
            if guard.cancelled:
                cancelled = True
                run_status = "canceled"
        except (asyncio.CancelledError, GeneratorExit):
            await guard.mark_cancelled()
            cancelled = True
            run_status = "canceled"
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("SSE 流异常")
            run_status = "error"
            mapped = _guard_error_payload(exc)
            if mapped is None:
                yield _sse_frame("error", {"code": 50000, "message": "stream error"})
            else:
                payload, reason = mapped
                guard.rejection_code = int(payload["code"])
                if not bool(getattr(exc, "rejection_recorded", False)):
                    metrics.record_guard_rejected(reason)
                    exc.rejection_recorded = True  # type: ignore[attr-defined]
                yield _sse_frame("error", payload)
        finally:
            await guard.close()
            cancelled = cancelled or guard.cancelled
            if context_set:
                reset_tool_context()
            if session_opened:
                metrics.session_close(str(session_id))
            duration_ms = int((time.monotonic() - stream_start) * 1000)
            reply = "".join(reply_parts).strip()
            if trace_id and user_message_row is not None:
                await repo.link_messages_to_trace(db, request_id=request_id, trace_id=trace_id)
            if (
                reply
                and not cancelled
                and run_status == "ok"
                and user_message_row is not None
            ):
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
            if (
                reply
                and not cancelled
                and not cache_hit
                and run_status == "ok"
                and resolved_agent.memory_enabled
            ):
                _schedule_memory_extract(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    user_msg=body.message,
                    assistant_msg=reply,
                    session_id=session_id,
                    session_title=sess.title,
                )
            if reply and not cancelled and not cache_hit and run_status == "ok":
                _schedule_semantic_cache_store(
                    body.message,
                    reply,
                    scope=cache_scope,
                    tool_intent=cache_tool_intent,
                    tool_used=tool_calls_count > 0,
                    metadata={
                        "model": model_id,
                        "agent_id": (
                            str(resolved_agent.agent_id)
                            if resolved_agent.agent_id
                            else None
                        ),
                    },
                )
            await audit.audit(
                tenant_id,
                "chat.stream",
                user_id=user_id,
                session_id=session_id,
                detail={
                    "model": model_id,
                    "agent_id": (
                        str(resolved_agent.agent_id)
                        if resolved_agent.agent_id
                        else None
                    ),
                    "cancelled": cancelled,
                    "rejection_code": guard.rejection_code,
                    "cache_hit": cache_hit,
                    "semantic_cache_similarity": (
                        round(cache_similarity, 6)
                        if cache_similarity is not None
                        else None
                    ),
                },
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
        _observe_sse_first_outcome(
            event_stream(),
            request_started_monotonic=request_started_at,
        ),
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


@router.get("/{session_id}/pending-approval", response_model=ApiResponse)
async def get_pending_approval(
    session_id: uuid.UUID,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """查询会话是否停在工具审批点，用于切换会话或刷新后的恢复。"""
    sess = await repo.get_session(db, session_id)
    if (
        sess is None
        or sess.tenant_id != auth.tenant.tenant_id
        or sess.status == "deleted"
    ):
        raise HTTPException(status_code=404, detail="session not found")
    if harness_app.runtime is None:
        raise HTTPException(status_code=503, detail="harness not ready")
    pending = await harness_app.runtime.pending_approval(
        sess.thread_id,
        auth.tenant.tenant_id,
    )
    return ApiResponse(data=pending)


@router.post("/{session_id}/resume")
async def resume(
    session_id: uuid.UUID,
    body: ResumeRequest,
    request: Request,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    if settings.CHAT_RUN_ENABLED:
        from ..storage import chat_runs
        from . import router_runs

        active = await chat_runs.get_active_run(
            db,
            session_id=session_id,
            tenant_id=auth.tenant.tenant_id,
            user_id=auth.user_id,
        )
        if active is None:
            raise HTTPException(status_code=409, detail="当前会话没有待恢复的运行")
        resumed = await router_runs.resume_run(
            active.id,
            ChatRunResumeRequest(
                decision="approve" if body.decision == "approve" else "reject",
                input=body.input,
            ),
            request,
            auth,
            db,
        )
        if not isinstance(resumed, ApiResponse):
            return resumed
        return await router_runs.subscribe_events(
            active.id,
            request,
            active.last_event_sequence,
            None,
            auth,
            db,
        )
    """HITL 恢复：向挂起的 interrupt 提交输入后继续执行（流式）。"""
    request_started_at = time.monotonic()
    outcome_deadline_monotonic = request_started_at + float(
        settings.GUARD_OUTCOME_DEADLINE_SECONDS
    )
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    tenant_id = auth.tenant.tenant_id
    user_id = auth.user_id
    client_ip = get_client_ip(request)

    async def prepare_resume_preflight():
        sess = await repo.get_session(db, session_id)
        if (
            sess is None
            or sess.tenant_id != tenant_id
            or sess.status == "deleted"
        ):
            raise HTTPException(status_code=404, detail="session not found")
        if harness_app.runtime is None:
            raise HTTPException(status_code=503, detail="harness not ready")
        pending = await harness_app.runtime.pending_approval(sess.thread_id, tenant_id)
        if harness_app.runtime.checkpointer is not None and pending is None:
            raise HTTPException(status_code=409, detail="当前会话没有待审批的工具调用")

        resolved_agent = await _resolve_request_agent(
            db,
            tenant_id=tenant_id,
            session=sess,
            body=body,
        )
        model_id, model_base_url, model_api_key, attachments = (
            _request_model_and_attachments(
                body=body,
                tenant_id=tenant_id,
                session_id=session_id,
                agent=resolved_agent,
            )
        )
        skill_names = _resolve_skill_names(body, resolved_agent)
        requested_kb_ids = await knowledge_service.resolve_ids_by_names(
            db,
            tenant_id=tenant_id,
            names=body.knowledge_bases or [],
        )
        if resolved_agent.active and resolved_agent.allowed_kb_ids:
            allowed_kb_ids = (
                resolved_agent.allowed_kb_ids
                if body.knowledge_bases is None
                else [
                    value
                    for value in requested_kb_ids
                    if value in set(resolved_agent.allowed_kb_ids)
                ]
            )
        else:
            allowed_kb_ids = requested_kb_ids
        requested_mcp = _resolve_mcp_ids(body, resolved_agent)
        dynamic_tools = await mcp_manager.resolve_tools(
            db,
            tenant_id=tenant_id,
            requested_capability_ids=requested_mcp,
        )
        orchestration_agents = await _resolve_orchestration_context(
            db,
            tenant_id=tenant_id,
        )
        return {
            "sess": sess,
            "pending": pending,
            "resolved_agent": resolved_agent,
            "model_id": model_id,
            "model_base_url": model_base_url,
            "model_api_key": model_api_key,
            "attachments": attachments,
            "skill_names": skill_names,
            "allowed_kb_ids": allowed_kb_ids,
            "requested_mcp": requested_mcp,
            "dynamic_tools": dynamic_tools,
            "orchestration_agents": orchestration_agents,
        }

    async def event_stream():
        guard = _SSEGuardLifecycle(
            request,
            auth,
            session_id,
            request_id,
            outcome_deadline_monotonic=outcome_deadline_monotonic,
        )
        stream_start = request_started_at
        run_status = "ok"
        context_set = False
        session_opened = False
        sess = None
        reply_parts: list[str] = []
        trace_id: str | None = None
        tool_calls_count = 0
        resolved_agent = ResolvedAgentCapabilities()
        try:
            user_error = await guard.acquire_user()
            if user_error is not None:
                run_status = "error"
                yield _sse_frame("error", user_error)
                return
            async for wait_frame in guard.iter_operation(
                prepare_resume_preflight()
            ):
                yield wait_frame
            if guard.cancelled:
                run_status = "canceled"
                return
            prepared = guard.operation_result
            sess = prepared["sess"]
            resolved_agent = prepared["resolved_agent"]
            async for admission_frame in guard.acquire_stream():
                yield admission_frame
            if not guard.stream_admitted:
                run_status = "canceled" if guard.cancelled else "error"
                return

            metrics.session_open(str(session_id))
            session_opened = True
            set_tool_context(
                ToolContext(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    allowed_kb_ids=prepared["allowed_kb_ids"],
                    dynamic_tools=prepared["dynamic_tools"],
                    model_id=prepared["model_id"],
                    model_base_url=prepared["model_base_url"],
                    model_api_key=prepared["model_api_key"],
                    attachments=prepared["attachments"],
                    agent_id=resolved_agent.agent_id,
                    agent_name=resolved_agent.agent_name,
                    agent_system_prompt=resolved_agent.system_prompt_block,
                    allowed_skill_names=(
                        resolved_agent.allowed_skill_names
                        if resolved_agent.active
                        else None
                    ),
                    allowed_tool_names=(
                        resolved_agent.allowed_tool_names
                        if resolved_agent.active
                        else None
                    ),
                    allowed_capability_ids=prepared["requested_mcp"],
                    memory_enabled=resolved_agent.memory_enabled,
                    orchestration_agents=prepared["orchestration_agents"],
                    session_id=session_id,
                    request_id=request_id,
                    outcome_deadline_monotonic=outcome_deadline_monotonic,
                )
            )
            context_set = True
            async for wait_frame in guard.iter_operation(
                _current_degrade_payload()
            ):
                yield wait_frame
            if guard.cancelled:
                run_status = "canceled"
                return
            degrade_payload = guard.operation_result
            if degrade_payload is not None:
                yield _sse_frame("degrade", degrade_payload)

            runtime_stream = harness_app.runtime.resume(
                sess.thread_id,
                body.input,
                body.decision,
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=str(session_id),
                request_id=request_id,
            )
            async for frame in guard.iter_runtime(runtime_stream):
                delta = _extract_stream_delta(frame)
                if delta:
                    reply_parts.append(delta)
                if "event: trace_start" in frame:
                    try:
                        data = next(
                            (line[6:] for line in frame.splitlines() if line.startswith("data: ")),
                            "{}",
                        )
                        trace_id = json.loads(data).get("trace_id") or trace_id
                    except json.JSONDecodeError:
                        pass
                if "event: error" in frame:
                    run_status = "error"
                    try:
                        data = next(
                            (
                                line[6:]
                                for line in frame.splitlines()
                                if line.startswith("data: ")
                            ),
                            "{}",
                        )
                        code = int(json.loads(data).get("code") or 0)
                        if code in {50300, 50400, 50401, 50402}:
                            guard.rejection_code = code
                    except (ValueError, json.JSONDecodeError):
                        pass
                elif "event: run_end" in frame:
                    try:
                        data = next(
                            (
                                line[6:]
                                for line in frame.splitlines()
                                if line.startswith("data: ")
                            ),
                            "{}",
                        )
                        status = str(json.loads(data).get("status") or "error")
                        run_status = "ok" if status == "success" else status
                        tool_calls_count = int(json.loads(data).get("tool_calls") or 0)
                    except json.JSONDecodeError:
                        run_status = "error"
                yield frame
            if guard.cancelled:
                run_status = "canceled"
        except (asyncio.CancelledError, GeneratorExit):
            await guard.mark_cancelled()
            run_status = "canceled"
            raise
        except Exception as exc:  # noqa: BLE001
            run_status = "error"
            mapped = _guard_error_payload(exc)
            if mapped is None:
                yield _sse_frame("error", {"code": 50002, "message": "resume failed"})
            else:
                payload, reason = mapped
                guard.rejection_code = int(payload["code"])
                if not bool(getattr(exc, "rejection_recorded", False)):
                    metrics.record_guard_rejected(reason)
                    exc.rejection_recorded = True  # type: ignore[attr-defined]
                yield _sse_frame("error", payload)
        finally:
            await guard.close()
            if context_set:
                reset_tool_context()
            if session_opened:
                metrics.session_close(str(session_id))
            duration_ms = int((time.monotonic() - stream_start) * 1000)
            reply = "".join(reply_parts).strip()
            if trace_id:
                await repo.link_messages_to_trace(
                    db,
                    request_id=request_id,
                    trace_id=trace_id,
                )
            if reply and run_status == "ok" and not guard.cancelled:
                await repo.add_message(
                    db,
                    session_id=session_id,
                    role="assistant",
                    content=reply,
                    trace_id=trace_id,
                    request_id=request_id,
                    step_index=2,
                    agent_id=resolved_agent.agent_id,
                )
            await audit.audit(
                tenant_id,
                "chat.resume",
                user_id=user_id,
                session_id=session_id,
                detail={
                    "decision": body.decision,
                    "cancelled": guard.cancelled,
                    "rejection_code": guard.rejection_code,
                    "tool_calls": tool_calls_count,
                },
                ip=client_ip,
            )
            metrics.record_request(
                tenant_id,
                ok=(run_status == "ok"),
                duration_ms=duration_ms,
                agent_id=(
                    str(resolved_agent.agent_id)
                    if resolved_agent.agent_id
                    else None
                ),
            )

    return StreamingResponse(
        _observe_sse_first_outcome(
            event_stream(),
            request_started_monotonic=request_started_at,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Request-Id": request_id,
        },
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
