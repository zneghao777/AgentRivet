"""与 HTTP Subscriber 解耦的 ChatRun 后台执行协调器。"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from ..agents.models import OrchestrationAgent, ResolvedAgentCapabilities
from ..config import settings
from ..observability import audit
from ..observability.metrics import metrics
from ..storage import chat_runs
from ..storage.db import AsyncSessionLocal
from ..storage.models import ChatRun
from .context import ToolContext, pop_tool_context, push_tool_context
from .run_events import ChatRunEventSink, run_event_notifier

logger = logging.getLogger(__name__)


class LeaseLostError(RuntimeError):
    pass


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def _resolved_agent_from_snapshot(payload: dict[str, Any]) -> ResolvedAgentCapabilities:
    snapshot = payload.get("agent_snapshot") or {}
    return ResolvedAgentCapabilities(
        agent_id=_uuid_or_none(snapshot.get("agent_id")),
        agent_name=snapshot.get("agent_name"),
        status=snapshot.get("status"),
        system_prompt_block=snapshot.get("system_prompt_block"),
        allowed_skill_names=list(snapshot.get("allowed_skill_names") or []),
        allowed_tool_names=set(snapshot.get("allowed_tool_names") or []) or None,
        allowed_capability_ids={
            parsed
            for value in snapshot.get("allowed_capability_ids") or []
            if (parsed := _uuid_or_none(value)) is not None
        },
        allowed_kb_ids=[
            parsed
            for value in snapshot.get("allowed_kb_ids") or []
            if (parsed := _uuid_or_none(value)) is not None
        ]
        or None,
        model=snapshot.get("model"),
        model_base_url=snapshot.get("model_base_url"),
        memory_enabled=bool(snapshot.get("memory_enabled", True)),
    )


def _orchestration_from_snapshot(payload: dict[str, Any]) -> dict[str, OrchestrationAgent]:
    result: dict[str, OrchestrationAgent] = {}
    for item in payload.get("orchestration_agents") or []:
        agent_id = _uuid_or_none(item.get("agent_id"))
        if agent_id is None or not item.get("name"):
            continue
        result[str(item["name"])] = OrchestrationAgent(
            agent_id=agent_id,
            tenant_id=str(item.get("tenant_id") or "default"),
            name=str(item["name"]),
            description=str(item.get("description") or ""),
            scope=str(item.get("scope") or "internal"),
            system_prompt=item.get("system_prompt"),
            model=item.get("model"),
            model_base_url=item.get("model_base_url"),
            memory_enabled=bool(item.get("memory_enabled", True)),
            allowed_skill_names=list(item.get("allowed_skill_names") or []),
            allowed_tool_names=set(item.get("allowed_tool_names") or []) or None,
            allowed_capability_ids={
                parsed
                for value in item.get("allowed_capability_ids") or []
                if (parsed := _uuid_or_none(value)) is not None
            },
            allowed_kb_ids=[
                parsed
                for value in item.get("allowed_kb_ids") or []
                if (parsed := _uuid_or_none(value)) is not None
            ]
            or None,
            capability_summary=str(item.get("capability_summary") or ""),
        )
    return result


class RunCoordinator:
    """领取、执行、暂停、取消和完成 ChatRun。"""

    def __init__(self) -> None:
        self.worker_id = f"{settings.APP_NAME}-api-{uuid.uuid4().hex[:12]}"
        self._queue: asyncio.Queue[uuid.UUID] = asyncio.Queue()
        self._queued: set[uuid.UUID] = set()
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._lease_lost: set[uuid.UUID] = set()
        self._scheduler_task: asyncio.Task[None] | None = None
        self._scan_task: asyncio.Task[None] | None = None
        self._stopping = False

    @property
    def local_execution_enabled(self) -> bool:
        return not (
            settings.CHAT_RUN_DURABLE_ENABLED and settings.MQ_ENABLED
        )

    async def start(self) -> None:
        if not settings.CHAT_RUN_ENABLED or self._scheduler_task is not None:
            return
        self._stopping = False
        async with AsyncSessionLocal() as db:
            requeued, failed = await chat_runs.recover_expired_leases(
                db,
                max_attempts=settings.CHAT_RUN_MAX_ATTEMPTS,
                write_outbox=(
                    settings.CHAT_RUN_DURABLE_ENABLED and settings.MQ_ENABLED
                ),
            )
        metrics.record_chat_run_recovery("requeued", requeued)
        metrics.record_chat_run_recovery("failed", failed)
        if requeued or failed:
            logger.warning(
                "ChatRun 启动恢复 requeued=%d failed=%d", requeued, failed
            )
        if self.local_execution_enabled:
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())
            self._scan_task = asyncio.create_task(self._scan_loop())
            await self._enqueue_persisted()

    async def stop(self) -> None:
        self._stopping = True
        for task in (self._scheduler_task, self._scan_task, *self._tasks.values()):
            if task is not None and not task.done():
                task.cancel()
        pending = [
            task
            for task in (self._scheduler_task, self._scan_task, *self._tasks.values())
            if task is not None
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._scheduler_task = None
        self._scan_task = None
        self._tasks.clear()
        self._queued.clear()

    async def enqueue(self, run_id: uuid.UUID) -> None:
        if not settings.CHAT_RUN_ENABLED or not self.local_execution_enabled:
            return
        if run_id in self._queued or run_id in self._tasks:
            return
        self._queued.add(run_id)
        await self._queue.put(run_id)

    async def execute(self, run_id: uuid.UUID) -> None:
        """由本地协调器或独立 Worker 执行一次领取尝试。"""
        async with AsyncSessionLocal() as db:
            run = await chat_runs.claim_run(
                db,
                run_id=run_id,
                worker_id=self.worker_id,
                lease_seconds=settings.CHAT_RUN_WORKER_LEASE_SECONDS,
                max_active_per_user=settings.CHAT_RUN_MAX_ACTIVE_PER_USER,
                max_active_per_tenant=settings.CHAT_RUN_MAX_ACTIVE_PER_TENANT,
                max_attempts=settings.CHAT_RUN_MAX_ATTEMPTS,
            )
        if run is None:
            return
        if run.attempt > 1:
            await audit.audit(
                run.tenant_id,
                "chat.run.recovered",
                user_id=run.user_id,
                session_id=run.session_id,
                request_id=run.request_id,
                resource_id=str(run.id),
                detail={
                    "run_id": str(run.id),
                    "status": run.status,
                    "attempt": run.attempt,
                },
            )
            async with AsyncSessionLocal() as db:
                unsafe_retry = await chat_runs.has_tool_events(db, run_id=run.id)
            if unsafe_retry:
                await self._fail_run(
                    run,
                    "CHAT_RUN_UNSAFE_RETRY",
                    "检测到已执行工具，无法证明外部副作用可安全重试",
                    time.monotonic(),
                )
                return
        metrics.record_chat_run_status("queued", -1)
        metrics.record_chat_run_status("running", 1)
        metrics.record_chat_run_queue_wait(
            max(0.0, (utcnow() - run.created_at).total_seconds() * 1000)
        )
        await run_event_notifier.publish(run_id)
        await audit.audit(
            run.tenant_id,
            "chat.run.start",
            user_id=run.user_id,
            session_id=run.session_id,
            request_id=run.request_id,
            resource_id=str(run.id),
            detail={"run_id": str(run.id), "status": run.status, "attempt": run.attempt},
        )
        started = time.monotonic()
        heartbeat = asyncio.create_task(
            self._heartbeat(run_id, asyncio.current_task())
        )
        try:
            async with asyncio.timeout(settings.CHAT_RUN_TIMEOUT_SECONDS):
                await self._run_graph(run)
        except asyncio.CancelledError:
            if run_id in self._lease_lost:
                self._lease_lost.discard(run_id)
                logger.warning("ChatRun 租约丢失，停止推进 run=%s", run_id)
                return
            from . import llm_guard

            llm_guard.cancel_request(run.request_id)
            async with AsyncSessionLocal() as db:
                await chat_runs.finish_canceled(
                    db,
                    run_id=run_id,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            await run_event_notifier.publish(run_id)
            await audit.audit(
                run.tenant_id,
                "chat.run.canceled",
                user_id=run.user_id,
                session_id=run.session_id,
                request_id=run.request_id,
                resource_id=str(run.id),
                detail={"run_id": str(run.id), "status": "canceled", "attempt": run.attempt},
            )
        except TimeoutError:
            await self._fail_run(run, "CHAT_RUN_TIMEOUT", "运行超过最大执行时间", started)
        except Exception as exc:  # noqa: BLE001
            logger.exception("ChatRun 执行失败 run=%s", run_id)
            await self._fail_run(run, "CHAT_RUN_EXECUTION_FAILED", str(exc), started)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def request_cancel(self, run_id: uuid.UUID) -> None:
        async with AsyncSessionLocal() as db:
            run, changed = await chat_runs.request_cancel(db, run_id=run_id)
        if changed:
            from . import llm_guard

            llm_guard.cancel_request(run.request_id)
            task = self._tasks.get(run_id)
            if run.status == "running" and task is not None and not task.done():
                task.cancel()
            await run_event_notifier.publish(run_id)

    async def resume(self, run_id: uuid.UUID, command: dict[str, Any]) -> None:
        async with AsyncSessionLocal() as db:
            await chat_runs.queue_resume(
                db,
                run_id=run_id,
                command=command,
                write_outbox=(
                    settings.CHAT_RUN_DURABLE_ENABLED and settings.MQ_ENABLED
                ),
            )
        await run_event_notifier.publish(run_id)
        await self.enqueue(run_id)

    async def _scheduler_loop(self) -> None:
        while True:
            run_id = await self._queue.get()
            self._queued.discard(run_id)
            if run_id in self._tasks:
                self._queue.task_done()
                continue
            task = asyncio.create_task(self.execute(run_id))
            self._tasks[run_id] = task

            def done(_task: asyncio.Task[None], current_run_id: uuid.UUID = run_id) -> None:
                self._tasks.pop(current_run_id, None)
                self._queue.task_done()

            task.add_done_callback(done)

    async def _scan_loop(self) -> None:
        interval = max(0.25, settings.CHAT_RUN_EVENT_POLL_INTERVAL_MS / 1000)
        while True:
            await asyncio.sleep(interval)
            await self._enqueue_persisted()

    async def _enqueue_persisted(self) -> None:
        async with AsyncSessionLocal() as db:
            run_ids = await chat_runs.list_queued_run_ids(db)
        for run_id in run_ids:
            await self.enqueue(run_id)

    async def _heartbeat(
        self,
        run_id: uuid.UUID,
        execution_task: asyncio.Task[Any] | None,
    ) -> None:
        while True:
            await asyncio.sleep(settings.CHAT_RUN_WORKER_HEARTBEAT_SECONDS)
            async with AsyncSessionLocal() as db:
                renewed = await chat_runs.renew_lease(
                    db,
                    run_id=run_id,
                    worker_id=self.worker_id,
                    lease_seconds=settings.CHAT_RUN_WORKER_LEASE_SECONDS,
                )
            if not renewed:
                async with AsyncSessionLocal() as db:
                    current = await db.get(ChatRun, run_id)
                if current is None or current.cancel_requested_at is None:
                    self._lease_lost.add(run_id)
                if execution_task is not None:
                    execution_task.cancel()
                return

    async def _run_graph(self, run) -> None:
        from ..api.router_chat import _retrieve_memory_context, _schedule_memory_extract
        from ..chat.attachments import prepare_attachments
        from ..harness.app_runtime import harness_app
        from ..knowledge.service import knowledge_service
        from ..mcp.manager import mcp_manager
        from ..storage import repositories as repo

        if harness_app.runtime is None:
            raise RuntimeError("harness not ready")
        payload = dict(run.request_payload or {})
        resolved_agent = _resolved_agent_from_snapshot(payload)
        orchestration_agents = _orchestration_from_snapshot(payload)
        requested_capability_ids = {
            parsed
            for value in payload.get("mcp_capability_ids") or []
            if (parsed := _uuid_or_none(value)) is not None
        }
        async with AsyncSessionLocal() as db:
            session = await repo.get_session(db, run.session_id)
            if (
                session is None
                or session.status == "deleted"
                or session.tenant_id != run.tenant_id
            ):
                raise RuntimeError("session not found")
            # Run.user_id 在匿名模式下是 API Key 的配额/鉴权主体。
            # 业务上下文必须沿用会话用户，与旧聊天接口及 /memory 的归属一致。
            user_id = session.user_id
            dynamic_tools = await mcp_manager.resolve_tools(
                db,
                tenant_id=run.tenant_id,
                requested_capability_ids=requested_capability_ids or None,
            )
            # Run 快照只保存能力 ID；每次执行（含审批恢复）重新装载 Worker 工具。
            # 使用各 Agent 的授权集合，空集合不得回退为租户全部工具。
            for agent in orchestration_agents.values():
                if agent.allowed_capability_ids:
                    agent.dynamic_tools = await mcp_manager.resolve_tools(
                        db,
                        tenant_id=run.tenant_id,
                        requested_capability_ids=agent.allowed_capability_ids,
                    )
            memory_context = ""
            if resolved_agent.memory_enabled:
                memory_context = await _retrieve_memory_context(
                    db,
                    tenant_id=run.tenant_id,
                    user_id=user_id,
                    query=str(payload.get("message") or ""),
                )
            # 名称快照用于审计；执行时只解析创建阶段已允许的知识库名称。
            allowed_kb_ids = [
                parsed
                for value in payload.get("knowledge_base_ids") or []
                if (parsed := _uuid_or_none(value)) is not None
            ]
            if not allowed_kb_ids and payload.get("knowledge_bases"):
                allowed_kb_ids = await knowledge_service.resolve_ids_by_names(
                    db,
                    tenant_id=run.tenant_id,
                    names=list(payload.get("knowledge_bases") or []),
                )
        attachments = prepare_attachments(
            run.tenant_id,
            run.session_id,
            list(payload.get("attachment_ids") or []),
        )
        context = ToolContext(
            tenant_id=run.tenant_id,
            user_id=user_id,
            allowed_kb_ids=allowed_kb_ids,
            dynamic_tools=dynamic_tools,
            model_id=payload.get("model") or settings.LLM_MODEL,
            model_base_url=payload.get("model_base_url"),
            model_api_key=None,
            attachments=attachments,
            agent_id=resolved_agent.agent_id,
            agent_name=resolved_agent.agent_name,
            agent_system_prompt=resolved_agent.system_prompt_block,
            allowed_skill_names=resolved_agent.allowed_skill_names,
            allowed_tool_names=resolved_agent.allowed_tool_names,
            allowed_capability_ids=requested_capability_ids or None,
            memory_enabled=resolved_agent.memory_enabled,
            orchestration_agents=orchestration_agents,
            session_id=run.session_id,
            message_id=run.user_message_id,
            request_id=run.request_id,
            chat_run_id=run.id,
            degrade_level=int(payload.get("degrade_level") or 0),
        )
        token = push_tool_context(context)
        sink = ChatRunEventSink(run.id)
        reply_parts: list[str] = []
        token_usage: dict[str, int] | None = None
        trace_id: str | None = run.trace_id
        tool_calls = 0
        interrupted = False
        runtime_status = "error"
        started = time.monotonic()
        try:
            resume_command = payload.get("resume_command")
            if resume_command:
                stream = harness_app.runtime.resume(
                    session.thread_id,
                    str(resume_command.get("input") or ""),
                    str(resume_command.get("decision") or "reject"),
                    tenant_id=run.tenant_id,
                    user_id=user_id,
                    session_id=str(run.session_id),
                    request_id=run.request_id,
                    link_trace_id=run.trace_id,
                    chat_run_id=str(run.id),
                )
            else:
                stream = harness_app.runtime.astream(
                    session.thread_id,
                    str(payload.get("message") or ""),
                    run.tenant_id,
                    user_id,
                    list(payload.get("skills") or []),
                    session_id=str(run.session_id),
                    memory_context=memory_context,
                    request_id=run.request_id,
                    message_id=run.user_message_id,
                    approval_mode=str(payload.get("approval_mode") or "auto"),
                    chat_run_id=str(run.id),
                )
            async for frame in stream:
                parsed = await sink.consume_frame(frame)
                if parsed is None:
                    continue
                event = parsed.payload
                if parsed.event_type == "message_delta":
                    reply_parts.append(str(event.get("content") or ""))
                elif parsed.event_type == "message_end":
                    raw_usage = event.get("token_usage")
                    if isinstance(raw_usage, dict):
                        token_usage = {str(key): int(value) for key, value in raw_usage.items()}
                elif parsed.event_type == "trace_start":
                    trace_id = str(event.get("trace_id") or "") or trace_id
                    if trace_id:
                        async with AsyncSessionLocal() as db:
                            await chat_runs.set_trace_id(
                                db, run_id=run.id, trace_id=trace_id
                            )
                elif parsed.event_type == "interrupt_requested":
                    interrupted = True
                elif parsed.event_type == "run_end":
                    runtime_status = str(event.get("status") or "error")
                    tool_calls = int(event.get("tool_calls") or 0)
        finally:
            pop_tool_context(token)

        duration_ms = int((time.monotonic() - started) * 1000)
        if interrupted or runtime_status == "interrupted":
            async with AsyncSessionLocal() as db:
                await chat_runs.mark_pending_approval(
                    db, run_id=run.id, worker_id=self.worker_id
                )
            metrics.record_chat_run_status("pending_approval", 1)
            metrics.record_chat_run_status("running", -1)
            await run_event_notifier.publish(run.id)
            return
        reply = "".join(reply_parts).strip()
        if runtime_status not in {"ok", "success"}:
            raise RuntimeError(f"GraphRuntime 终态异常: {runtime_status}")
        async with AsyncSessionLocal() as db:
            completed = await chat_runs.finish_succeeded(
                db,
                run_id=run.id,
                worker_id=self.worker_id,
                content=reply,
                trace_id=trace_id,
                token_usage=token_usage,
                agent_id=resolved_agent.agent_id,
                duration_ms=duration_ms,
                tool_calls=tool_calls,
            )
            # 只在回复及成功终态提交后提炼；审批恢复仍使用原始用户请求。
            # 复用 MQ 发布与进程内降级，不阻塞回复，也不依赖 SSE 订阅者。
            if resolved_agent.memory_enabled:
                _schedule_memory_extract(
                    tenant_id=run.tenant_id,
                    user_id=user_id,
                    user_msg=str(payload.get("message") or ""),
                    assistant_msg=reply,
                    session_id=run.session_id,
                    session_title=session.title,
                )
            if trace_id:
                await repo.link_messages_to_trace(
                    db, request_id=run.request_id, trace_id=trace_id
                )
        metrics.record_chat_run_status("running", -1)
        metrics.record_chat_run_duration(duration_ms)
        await run_event_notifier.publish(run.id)
        await audit.audit(
            run.tenant_id,
            "chat.run.succeeded",
            user_id=run.user_id,
            session_id=run.session_id,
            trace_id=trace_id,
            request_id=run.request_id,
            resource_id=str(run.id),
            detail={"run_id": str(run.id), "status": completed.status, "attempt": run.attempt},
        )

    async def _fail_run(self, run, code: str, message: str, started: float) -> None:
        duration_ms = int((time.monotonic() - started) * 1000)
        async with AsyncSessionLocal() as db:
            await chat_runs.finish_failed(
                db,
                run_id=run.id,
                error_code=code,
                error_message=message,
                duration_ms=duration_ms,
            )
        metrics.record_chat_run_status("running", -1)
        metrics.record_chat_run_duration(duration_ms)
        await run_event_notifier.publish(run.id)
        await audit.audit(
            run.tenant_id,
            "chat.run.failed",
            user_id=run.user_id,
            session_id=run.session_id,
            request_id=run.request_id,
            resource_id=str(run.id),
            detail={
                "run_id": str(run.id),
                "status": "failed",
                "error_code": code,
                "attempt": run.attempt,
            },
        )


def utcnow():
    from datetime import UTC, datetime

    return datetime.now(UTC)


run_coordinator = RunCoordinator()
