"""pytest 共享 fixture：应用实例、工具注册中心、Skill 管理器。"""

from __future__ import annotations

import os

import pytest_asyncio

# 测试环境强制使用非生产配置，避免读取真实 .env
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("API_KEYS", "test-key-1,test-key-2")
os.environ.setdefault("LLM_API_KEY", "")
os.environ.setdefault("LANGSMITH_TRACING", "false")
# 测试默认关闭可观测性落库（OBSERVABILITY_SPEC §4.3），避免 span 写入干扰断言
os.environ.setdefault("OBSERVABILITY_ENABLED", "false")
# 测试禁用真实 GLM 解析（否则走真实网络/计费），统一回退本地解析
os.environ.setdefault("GLM_FILES_API_KEY", "")
# 测试禁用真实 Rerank 服务探测（AGENTIC_RAG_UPGRADE_SPEC §3.4 降级路径单测另行 mock）
os.environ.setdefault("RERANK_ENABLED", "false")
# 普通 API 测试不连接真实 LLM；专项 guard 用例显式构造或开启防护实例。
os.environ.setdefault("GUARD_ENABLED", "false")
# 全套 API 用例复用少量测试 Key，避免默认入口限流造成跨用例串扰；专项限流用例
# 会构造启用状态的独立应用验证配额和 429 协议。
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
# 旧 SSE/语义缓存测试显式固定兼容路径；ChatRun 专项用例单独开启新路径。
os.environ.setdefault("CHAT_RUN_ENABLED", "false")


@pytest_asyncio.fixture(scope="session", autouse=True)
async def init_schema():
    """会话级自动 fixture：建表并初始化工具/Skill/Harness（TestClient 不触发 lifespan）。"""
    from app.harness.app_runtime import harness_app
    from app.skills.manager import skill_manager
    from app.storage.db import AsyncSessionLocal, create_tables
    from app.tools.registry import tool_registry

    await create_tables()
    tool_registry.register_builtin()
    async with AsyncSessionLocal() as db:
        await skill_manager.discover(db)
    harness_app.build(tool_registry, checkpointer=None)
    yield


@pytest_asyncio.fixture
async def app_client():
    """提供 FastAPI TestClient（httpx.AsyncClient）。"""
    from httpx import ASGITransport, AsyncClient

    # 延迟导入以触发配置加载
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def db_session():
    """提供独立 DB session。"""
    from app.storage.db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        yield session


@pytest_asyncio.fixture
def clean_registry():
    """清理工具注册中心（测试隔离）。"""
    from app.tools.registry import tool_registry

    before = tool_registry.snapshot()
    yield
    tool_registry.clear()
    for info in before:
        tool = tool_registry.get(info.name)
        if tool is not None:
            tool_registry.register(tool, source=info.source)
