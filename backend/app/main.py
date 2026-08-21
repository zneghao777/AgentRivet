"""FastAPI 入口：CORS、路由挂载、启动钩子。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .agents.demo_subagent import build_demo_subagent
from .agents.registry import agent_registry
from .api import (
    router_admin,
    router_chat,
    router_knowledge,
    router_knowledge_graph,
    router_mcp,
    router_memory,
    router_observability,
    router_sessions,
    router_skills,
    router_tools,
)
from .config import settings
from .harness.app_runtime import harness_app
from .memory.cleanup import start_ttl_scan, stop_ttl_scan
from .observability import audit
from .observability.aggregation import start_metric_worker, stop_metric_worker
from .observability.tracing import init_tracing
from .skills.manager import skill_manager
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

    # 建表
    await create_tables()

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

    # 子 Agent 注册
    agent_registry.register(build_demo_subagent())

    # Checkpointer + 图编译
    try:
        checkpointer = await get_checkpointer()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Checkpointer 初始化失败（%s），使用无持久化模式", exc)
        checkpointer = None
    harness_app.build(tool_registry, checkpointer=checkpointer)

    # 观测（OBSERVABILITY_SPEC）
    init_tracing(lambda: AsyncSessionLocal())
    audit.init_audit(lambda: AsyncSessionLocal())
    # 结构化日志 → observability_logs（§8.1）：WARN+ 自动注入 TraceContext
    from .observability import log_export

    log_export.install_log_handler()

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
    # 采集器 flush（§5.5：进程关闭最多等 5 秒）
    from .observability import tracing

    await tracing.shutdown_tracing()


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
app.include_router(router_chat.router, prefix=API)
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
