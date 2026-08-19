"""pytest 共享 fixture：应用实例、工具注册中心、Skill 管理器。"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

# 测试环境强制使用非生产配置，避免读取真实 .env
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("API_KEYS", "test-key-1,test-key-2")
os.environ.setdefault("LLM_API_KEY", "")
os.environ.setdefault("LANGSMITH_TRACING", "false")


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
