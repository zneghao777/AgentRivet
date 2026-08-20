"""知识检索工具测试：注册、上下文注入、租户白名单过滤。"""

from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.harness.context import ToolContext, reset_tool_context, set_tool_context
from app.knowledge.ingest import IngestError
from app.knowledge.service import knowledge_service
from app.storage.db import AsyncSessionLocal
from app.tools.knowledge_search import knowledge_search
from app.tools.registry import tool_registry

DOC_MD = (
    "# 产品手册\n\n"
    "## 计费说明\n"
    "基础版每月 99 元，专业版每月 299 元，企业版需联系销售。\n\n"
    "## 技术支持\n"
    "工作时间提供 7x12 小时支持，紧急故障响应不超过 1 小时。\n"
)


def _unique(name: str) -> str:
    return f"{name}-{uuid.uuid4().hex[:6]}"


def _mock_vec(text: str, dim: int | None = None) -> list[float]:
    dim = dim or settings.EMBEDDING_DIM
    vec = [0.0] * dim
    for index, ch in enumerate(text):
        vec[(ord(ch) + index * 7) % dim] += 1.0
    return vec


async def _mock_embed_texts(texts: list[str]) -> list[list[float]]:
    return [_mock_vec(t) for t in texts]


async def _mock_embed_query(query: str) -> list[float]:
    return _mock_vec(query)


@pytest.fixture(autouse=True)
def _mock_embeddings(monkeypatch):
    from app.knowledge import ingest as ingest_mod
    from app.knowledge import retriever as retriever_mod

    monkeypatch.setattr(ingest_mod, "embed_texts", _mock_embed_texts)
    monkeypatch.setattr(retriever_mod, "embed_query", _mock_embed_query)


@pytest.fixture(autouse=True)
def _clean_context():
    """每个用例前后清理工具上下文。"""
    reset_tool_context()
    yield
    reset_tool_context()


@pytest.fixture(autouse=True)
def _no_background_parse(monkeypatch):
    """禁用后台解析调度，测试改为手动 _run_parse（避免并发双跑）。"""
    monkeypatch.setattr("app.knowledge.service._schedule_parse", lambda doc_id: None)


async def _prepare_kb(*, tenant_id: str, name: str | None = None) -> str:
    """直接经服务层创建知识库并摄取文档，返回 kb_id。

    上传为异步解析；测试中同步执行解析任务以消除调度时序依赖。
    """
    from app.knowledge.service import _run_parse

    async with AsyncSessionLocal() as db:
        kb = await knowledge_service.create(
            db, tenant_id=tenant_id, name=name or _unique("产品手册")
        )
        assert kb is not None
        doc = await knowledge_service.upload_document(
            db,
            tenant_id=tenant_id,
            kb_id=kb.id,
            filename="manual.md",
            content=DOC_MD.encode("utf-8"),
        )
        await _run_parse(doc.id)
        return str(kb.id)


def test_knowledge_search_tool_registered(clean_registry):
    """检索工具已注册进注册中心。"""
    tool_registry.register_builtin()
    tool = tool_registry.get("knowledge_search")
    assert tool is not None
    assert "知识库" in tool.description


@pytest.mark.asyncio
async def test_tool_missing_context_graceful():
    """无工具上下文时优雅返回提示，不抛异常。"""
    reset_tool_context()
    result = await knowledge_search.ainvoke({"query": "计费", "top_k": 3})
    assert "上下文" in str(result)


@pytest.mark.asyncio
async def test_tool_retrieves_with_context():
    """设置上下文后工具可检索当前租户全部知识库。"""
    await _prepare_kb(tenant_id="tool-tenant")
    set_tool_context(ToolContext(tenant_id="tool-tenant", user_id="u1"))

    result = await knowledge_search.ainvoke({"query": "产品手册 计费 技术支持", "top_k": 5})
    text = str(result)
    assert "manual.md" in text  # 来源标注
    assert "计费" in text or "技术支持" in text


@pytest.mark.asyncio
async def test_tool_tenant_whitelist():
    """工具内按租户白名单过滤：命中白名单库返回结果，白名单不含该库时未命中。"""
    kb_id = await _prepare_kb(tenant_id="wl-tenant", name=_unique("白名单库"))

    # 白名单包含该库 → 命中
    set_tool_context(
        ToolContext(
            tenant_id="wl-tenant",
            user_id="u1",
            allowed_kb_ids=[uuid.UUID(kb_id)],
        )
    )
    result = await knowledge_search.ainvoke({"query": "产品手册 计费", "top_k": 5})
    assert "manual.md" in str(result)

    # 白名单为空 → 检索该租户全部知识库 → 仍命中
    set_tool_context(ToolContext(tenant_id="wl-tenant", user_id="u1"))
    result = await knowledge_search.ainvoke({"query": "产品手册 计费", "top_k": 5})
    assert "manual.md" in str(result)

    # 白名单不含该库（指向随机 uuid）→ 未命中
    set_tool_context(
        ToolContext(
            tenant_id="wl-tenant",
            user_id="u1",
            allowed_kb_ids=[uuid.uuid4()],
        )
    )
    result = await knowledge_search.ainvoke({"query": "产品手册 计费", "top_k": 5})
    assert "未检索到" in str(result)


@pytest.mark.asyncio
async def test_disabled_kb_is_excluded_from_default_search():
    """停用后的知识库不参与默认检索，重新启用后恢复。"""
    tenant_id = f"toggle-tenant-{uuid.uuid4().hex[:6]}"
    kb_id = await _prepare_kb(tenant_id=tenant_id, name=_unique("开关库"))
    async with AsyncSessionLocal() as db:
        disabled = await knowledge_service.set_enabled(
            db,
            tenant_id=tenant_id,
            kb_id=uuid.UUID(kb_id),
            enabled=False,
        )
        assert disabled is not None

    set_tool_context(ToolContext(tenant_id=tenant_id, user_id="u1"))
    result = await knowledge_search.ainvoke({"query": "产品手册 计费", "top_k": 5})
    assert "未检索到" in str(result)

    async with AsyncSessionLocal() as db:
        await knowledge_service.set_enabled(
            db,
            tenant_id=tenant_id,
            kb_id=uuid.UUID(kb_id),
            enabled=True,
        )


@pytest.mark.asyncio
async def test_tool_kb_name_must_belong_to_tenant():
    """指定 knowledge_base 名称时必须属于当前租户，否则返回未命中。"""
    # 在 tenant-X 创建知识库
    name = _unique("他人库")
    await _prepare_kb(tenant_id="owner-tenant", name=name)
    set_tool_context(ToolContext(tenant_id="other-tenant", user_id="u1"))

    result = await knowledge_search.ainvoke(
        {"query": "产品手册 计费", "knowledge_base": name, "top_k": 5}
    )
    assert "未检索到" in str(result)


@pytest.mark.asyncio
async def test_ingest_rejects_bad_format():
    """非法文件格式在摄取层即被拒绝。"""
    from app.knowledge.ingest import validate_file

    with pytest.raises(IngestError):
        validate_file("evil.exe", b"MZ...")
