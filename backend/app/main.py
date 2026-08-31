"""FastAPI 入口：CORS、路由挂载、启动钩子。"""

from __future__ import annotations

import logging
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import (
    router_admin,
    router_agents,
    router_chat,
    router_knowledge,
    router_knowledge_graph,
    router_mcp,
    router_memory,
    router_observability,
    router_runs,
    router_sessions,
    router_skills,
    router_tools,
)
from .api.rate_limit import install_rate_limiting
from .config import settings
from .harness.app_runtime import harness_app
from .harness.llm_guard import LLMGuardError
from .memory.cleanup import start_ttl_scan, stop_ttl_scan
from .observability import audit
from .observability.aggregation import start_metric_worker, stop_metric_worker
from .observability.tracing import init_tracing
from .skills.manager import skill_manager
from .storage import repositories as repo
from .storage.checkpointer import get_checkpointer
from .storage.db import AsyncSessionLocal, create_tables
from .tools.registry import tool_registry

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动钩子：建表、初始化工具/Skill/Agent、编译图、初始化观测。"""
    # 配置校验
    _validate_config()
    if settings.GUARD_ENABLED and settings.DEGRADE_ENABLED:
        from .harness.llm_guard import start_degrade_monitor

        start_degrade_monitor()
    from .api.router_chat import start_semantic_cache_tasks

    start_semantic_cache_tasks()

    # 建表
    await create_tables()
    async with AsyncSessionLocal() as db:
        recovered_tasks = await repo.fail_running_agent_tasks(db)
        if recovered_tasks:
            logger.warning(
                "已将 %d 个进程重启前的 running AgentTask 标记为 failed", recovered_tasks
            )

    # KBX：恢复进程中断前已持久化的排队/超时任务。
    if settings.KB_INDEX_JOBS_ENABLED:
        from sqlalchemy import select

        from .knowledge.graph.builder import schedule_build
        from .knowledge.jobs import index_job_service
        from .knowledge.service import _schedule_parse
        from .storage.models import KnowledgeIndexJob

        async with AsyncSessionLocal() as db:
            await index_job_service.recover_stale(
                db, stale_after_seconds=settings.KB_INDEX_JOB_STALE_SECONDS
            )
            queued_jobs = list(
                (
                    await db.execute(
                        select(KnowledgeIndexJob).where(KnowledgeIndexJob.status == "queued")
                    )
                )
                .scalars()
                .all()
            )
        for job in queued_jobs:
            if job.job_type == "parse" and job.doc_id is not None:
                _schedule_parse(job.doc_id)
            elif job.job_type == "graph_build":
                schedule_build(
                    None,
                    job.kb_id,
                    job.tenant_id,
                    full=bool((job.meta or {}).get("full")),
                    doc_id=job.doc_id,
                    job_id=job.id,
                )

    # 工具注册
    tool_registry.register_builtin()

    # Skill 发现
    async with AsyncSessionLocal() as db:
        await skill_manager.discover(db)

    # 逐个恢复已启用 MCP；单个远程服务失败不得阻断应用启动。
    if settings.MCP_ENABLED:
        from sqlalchemy import select

        from .mcp.adapter import McpAdapterError
        from .mcp.manager import mcp_manager
        from .storage.models import McpServer

        async with AsyncSessionLocal() as db:
            enabled_mcp_servers = list(
                (await db.execute(select(McpServer).where(McpServer.enabled.is_(True))))
                .scalars()
                .all()
            )
            for mcp_server in enabled_mcp_servers:
                try:
                    await mcp_manager.discover(db, mcp_server)
                except McpAdapterError as exc:
                    logger.warning("MCP Server %s 恢复失败: %s", mcp_server.name, exc)

    # Checkpointer + 图编译
    try:
        checkpointer = await get_checkpointer()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Checkpointer 初始化失败（%s），使用无持久化模式", exc)
        checkpointer = None
    harness_app.build(tool_registry, checkpointer=checkpointer)

    # ChatRun 执行生命周期独立于任何 HTTP Subscriber。
    if settings.CHAT_RUN_ENABLED:
        from .harness.chat_run_maintenance import start_chat_run_maintenance
        from .harness.chat_run_outbox import start_chat_run_outbox
        from .harness.run_coordinator import run_coordinator

        await run_coordinator.start()
        start_chat_run_outbox()
        start_chat_run_maintenance()

    # 观测（OBSERVABILITY_SPEC）
    init_tracing(lambda: AsyncSessionLocal())
    audit.init_audit(lambda: AsyncSessionLocal())
    # 结构化日志 → observability_logs（§8.1）：WARN+ 自动注入 TraceContext
    from .observability import log_export

    log_export.install_log_handler()

    # RabbitMQ 拓扑由代码幂等声明；不可用只记录并保留本地降级路径。
    if settings.MQ_ENABLED:
        from .mq.connection import mq_connection
        from .mq.monitor import start_mq_monitor

        try:
            await mq_connection.connect()
        except Exception as exc:  # noqa: BLE001
            logger.warning("RabbitMQ 启动连接失败，API 使用本地降级路径: %s", exc)
        start_mq_monitor()

    logger.info(
        "Harness 就绪：app=%s env=%s tools=%d skills=%d multi_agent=%s",
        settings.APP_NAME,
        settings.APP_ENV,
        len(tool_registry.snapshot()),
        len(skill_manager.all()),
        settings.MULTI_AGENT_ENABLED,
    )

    # 长期记忆 TTL 过期扫描（后台常驻，MEMORY_SPEC §6.3.1）
    start_ttl_scan()
    from .harness.guard_metrics_bridge import start_guard_metrics_bridge

    start_guard_metrics_bridge(collect_remote=True)
    # 可观测性：指标聚合 + span 保留清理（OBSERVABILITY_SPEC §2.4/§2.7）
    if settings.OBSERVABILITY_ENABLED:
        start_metric_worker()
        from .observability.alerts import start_alert_worker

        start_alert_worker()
    yield
    from .observability.alerts import stop_alert_worker

    await stop_alert_worker()
    await stop_metric_worker()
    await stop_ttl_scan()
    if settings.CHAT_RUN_ENABLED:
        from .harness.chat_run_maintenance import stop_chat_run_maintenance
        from .harness.chat_run_outbox import stop_chat_run_outbox
        from .harness.run_coordinator import run_coordinator

        await stop_chat_run_maintenance()
        await stop_chat_run_outbox()
        await run_coordinator.stop()
    from .api.router_chat import shutdown_semantic_cache_tasks
    from .harness.guard_metrics_bridge import stop_guard_metrics_bridge
    from .harness.llm_guard import close_degrade_monitor
    from .harness.semantic_cache import semantic_cache
    from .harness.stream_gate import user_stream_lock

    close_degrade_monitor()
    await stop_guard_metrics_bridge()
    await shutdown_semantic_cache_tasks()
    await semantic_cache.close()
    await user_stream_lock.close()
    # 采集器 flush（§5.5：进程关闭最多等 5 秒）
    from .observability import tracing

    await tracing.shutdown_tracing()
    if settings.MQ_ENABLED:
        from .mq.monitor import stop_mq_monitor
        from .mq.publisher import shutdown_publisher

        await stop_mq_monitor()
        await shutdown_publisher()


def _validate_config() -> None:
    """启动时校验关键配置。"""
    if settings.LLM_API_KEY == "":
        logger.warning("LLM_API_KEY 未配置，接口调用将返回错误事件（允许启动）")
    if not settings.api_keys_list:
        logger.warning("API_KEYS 未配置；prod 环境将拒绝所有请求，dev 环境使用默认 key")
    if settings.APP_ENV == "prod":
        missing = []
        if not settings.LLM_API_KEY:
            missing.append("LLM_API_KEY")
        if not settings.api_keys_list:
            missing.append("API_KEYS")
        if missing:
            raise RuntimeError(f"prod 环境缺少关键配置: {', '.join(missing)}")


app = FastAPI(
    title=settings.APP_NAME,
    version="0.1.0",
    docs_url="/docs",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)


@app.exception_handler(LLMGuardError)
async def llm_guard_error_handler(_request: Request, exc: LLMGuardError) -> JSONResponse:
    """Expose the same stable guard contract for non-streaming model calls."""
    if not exc.rejection_recorded:
        from .observability.metrics import metrics

        metrics.record_guard_rejected(exc.reason)
        exc.rejection_recorded = True
    retry_after_ms = exc.retry_after_ms if exc.retry_after_ms is not None else 5_000
    payload: dict[str, int | str] = {
        "code": exc.code,
        "message": str(exc),
    }
    headers: dict[str, str] = {}
    if retry_after_ms is not None:
        payload["retry_after_ms"] = retry_after_ms
        headers["Retry-After"] = str(max(1, math.ceil(retry_after_ms / 1000)))
    return JSONResponse(
        status_code=504 if exc.code in {50400, 50401, 50402} else 503,
        content=payload,
        headers=headers,
    )


# L1 入口限流先安装，随后安装的 CORS 位于外层，确保 429 也携带跨域头。
install_rate_limiting(app, settings)

# CORS（生产需限制白名单）
ALLOWED_ORIGINS = ["*"] if settings.APP_ENV != "prod" else []
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

API = settings.API_PREFIX
app.include_router(router_sessions.router, prefix=API)
app.include_router(router_agents.router, prefix=API)
app.include_router(router_chat.router, prefix=API)
app.include_router(router_runs.router, prefix=API)
app.include_router(router_skills.router, prefix=API)
app.include_router(router_tools.router, prefix=API)
app.include_router(router_knowledge.router, prefix=API)
app.include_router(router_knowledge_graph.router, prefix=f"{API}/kbs")
app.include_router(router_knowledge_graph.router, prefix=f"{API}/knowledge-bases")
app.include_router(router_mcp.router, prefix=API)
app.include_router(router_mcp.capability_router, prefix=API)
app.include_router(router_memory.router, prefix=API)
app.include_router(router_admin.router, prefix=API)
app.include_router(router_observability.router, prefix=API)


@app.get("/")
async def root():
    return {"app": settings.APP_NAME, "env": settings.APP_ENV, "docs": "/docs"}
