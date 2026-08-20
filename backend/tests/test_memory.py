"""长期记忆测试（MEMORY_SPEC §12 验收）：服务写入/检索、卫生、用户级隔离、API、工具。

embedding 使用确定性 mock（基于字符哈希的非负向量），避免依赖真实 Embedding API。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.config import settings
from app.memory import extractor
from app.memory.hygiene import long_term_enabled, resolve_duplicate, should_be_pending
from app.memory.service import memory_service
from app.observability import audit
from app.storage.db import AsyncSessionLocal
from app.storage.models import MemoryEntry, MemorySettings

AUTH = {"X-API-Key": "test-key-1"}
AUTH_A = {**AUTH, "X-Tenant-Id": "tenant-a"}
AUTH_B = {**AUTH, "X-Tenant-Id": "tenant-b"}


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
    from app.knowledge import embedder as embedder_mod

    monkeypatch.setattr(embedder_mod, "embed_texts", _mock_embed_texts)
    monkeypatch.setattr(embedder_mod, "embed_query", _mock_embed_query)


@pytest.fixture(autouse=True)
def _no_llm_extract(monkeypatch):
    """禁用真实 LLM 提炼，直接返回固定候选（供服务级测试精确控制）。"""

    async def _fake_extract(*args, **kwargs):
        return []

    monkeypatch.setattr(extractor, "extract", _fake_extract)


@pytest.fixture(autouse=True)
async def _init_audit():
    audit.init_audit(lambda: AsyncSessionLocal())
    yield
    await asyncio.sleep(0.3)


def _tenant() -> str:
    return f"t-{uuid.uuid4().hex[:8]}"


async def _insert_memory(
    *, tenant_id: str, user_id: str, content: str, memory_type: str = "semantic", status: str = "active", confidence: float = 0.9
) -> int:
    """直接经 service 插入一条记忆（复用向量化 + 入库）。"""
    async with AsyncSessionLocal() as db:
        await memory_service._insert(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            content=content,
            memory_type=memory_type,
            confidence=confidence,
            status=status,
            session_id=None,
            source_summary="测试来源",
        )
        await db.flush()
        entry = (
            await db.execute(
                select(MemoryEntry).where(MemoryEntry.content == content).order_by(MemoryEntry.id.desc())
            )
        ).scalars().first()
        memory_id = entry.id
        await db.commit()
    return memory_id


# ---------- 单元：判重 / 低置信度 ----------


def test_should_be_pending_low_confidence():
    assert should_be_pending(0.3) is True
    assert should_be_pending(0.8) is False


def test_resolve_duplicate_add():
    """无相似记忆 → ADD。"""
    result = resolve_duplicate({"content": "用户偏好 Python"}, [])
    assert result.action == "ADD"


def test_resolve_duplicate_noop():
    """高相似（>=0.92）→ NOOP（判重）。"""
    hits = [
        _FakeHit(memory_id=1, content="用户生产库是 Postgres", similarity=0.95),
    ]
    result = resolve_duplicate({"content": "用户生产库是 Postgres 16"}, hits)
    assert result.action == "NOOP"
    assert result.memory_id == 1


def test_resolve_duplicate_conflict_zone():
    """相似度落入 0.70–0.92 → CONFLICT（需 LLM 裁决）。"""
    hits = [_FakeHit(memory_id=2, content="用户偏好长回答", similarity=0.80)]
    result = resolve_duplicate({"content": "用户偏好简洁回答"}, hits)
    assert result.action == "CONFLICT"


class _FakeHit:
    def __init__(self, *, memory_id, content, similarity, access_count=0):
        self.memory_id = memory_id
        self.content = content
        self.similarity = similarity
        self.access_count = access_count


# ---------- 服务：写入与检索 ----------


@pytest.mark.asyncio
async def test_extract_from_conversation_with_candidates(monkeypatch):
    """对话结束后台提炼写入记忆（extract-consolidate 全流程）。"""
    tenant = _tenant()
    user = "u1"

    async def _fake_extract(*args, **kwargs):
        return [
            {"content": "用户生产库是 Postgres 16", "memory_type": "semantic", "confidence": 0.95},
            {"content": "用户偏好简洁回复", "memory_type": "semantic", "confidence": 0.9},
        ]

    monkeypatch.setattr(extractor, "extract", _fake_extract)
    async with AsyncSessionLocal() as db:
        written = await memory_service.extract_from_conversation(
            db,
            tenant_id=tenant,
            user_id=user,
            user_msg="我用 Postgres 16 做生产库，回复简洁点",
            assistant_msg="好的，已了解。",
        )
        assert written >= 2
    async with AsyncSessionLocal() as db:
        entries = (
            await db.execute(
                select(MemoryEntry).where(
                    MemoryEntry.tenant_id == tenant, MemoryEntry.user_id == user
                )
            )
        ).scalars().all()
        assert len(entries) >= 2
        assert all(e.status == "active" for e in entries)


@pytest.mark.asyncio
async def test_retrieve_returns_active_only():
    """检索仅命中 active 记忆；disabled 不返回。"""
    tenant = _tenant()
    user = "u1"
    await _insert_memory(tenant_id=tenant, user_id=user, content="用户是后端工程师", status="active")
    await _insert_memory(tenant_id=tenant, user_id=user, content="用户旧项目停用了", status="disabled")

    async with AsyncSessionLocal() as db:
        hits = await memory_service.retrieve_relevant(
            db, tenant_id=tenant, user_id=user, query="用户职业是什么"
        )
    assert any(h.content == "用户是后端工程师" for h in hits)
    assert not any(h.content == "用户旧项目停用了" for h in hits)


@pytest.mark.asyncio
async def test_user_isolation():
    """用户级隔离：不同 user_id 记忆互不可见。"""
    tenant = _tenant()
    await _insert_memory(tenant_id=tenant, user_id="alice", content="Alice 的私有记忆")
    await _insert_memory(tenant_id=tenant, user_id="bob", content="Bob 的私有记忆")

    async with AsyncSessionLocal() as db:
        alice_hits = await memory_service.retrieve_relevant(
            db, tenant_id=tenant, user_id="alice", query="私有记忆"
        )
        bob_hits = await memory_service.retrieve_relevant(
            db, tenant_id=tenant, user_id="bob", query="私有记忆"
        )
    assert any(h.content == "Alice 的私有记忆" for h in alice_hits)
    assert any(h.content == "Bob 的私有记忆" for h in bob_hits)
    assert not any(h.content == "Bob 的私有记忆" for h in alice_hits)
    assert not any(h.content == "Alice 的私有记忆" for h in bob_hits)


@pytest.mark.asyncio
async def test_tenant_isolation():
    """租户级隔离：不同 tenant_id 记忆互不可见。"""
    await _insert_memory(tenant_id="tenant-x", user_id="u1", content="租户 X 的秘密")
    async with AsyncSessionLocal() as db:
        hits = await memory_service.retrieve_relevant(
            db, tenant_id="tenant-y", user_id="u1", query="秘密"
        )
    assert not any("租户 X" in h.content for h in hits)


# ---------- 卫生：删除 / 清空 / 设置 ----------


@pytest.mark.asyncio
async def test_delete_entry_physical():
    """单条物理删除后不再可检索。"""
    tenant = _tenant()
    mid = await _insert_memory(tenant_id=tenant, user_id="u1", content="待删除的记忆")
    async with AsyncSessionLocal() as db:
        ok = await memory_service.delete_entry(
            db, tenant_id=tenant, user_id="u1", memory_id=mid
        )
        assert ok is True
    async with AsyncSessionLocal() as db:
        remaining = await memory_service.retrieve_relevant(
            db, tenant_id=tenant, user_id="u1", query="待删除"
        )
    assert not any("待删除" in h.content for h in remaining)


@pytest.mark.asyncio
async def test_clear_entries():
    """清空当前用户全部记忆。"""
    tenant = _tenant()
    await _insert_memory(tenant_id=tenant, user_id="u1", content="记忆1")
    await _insert_memory(tenant_id=tenant, user_id="u1", content="记忆2")
    async with AsyncSessionLocal() as db:
        deleted = await memory_service.clear_entries(
            db, tenant_id=tenant, user_id="u1"
        )
        assert deleted >= 2
        overview = await memory_service.overview(db, tenant_id=tenant, user_id="u1")
        assert overview["total"] == 0


@pytest.mark.asyncio
async def test_settings_toggle():
    """关闭长期记忆后不再注入/写入。"""
    tenant = _tenant()
    async with AsyncSessionLocal() as db:
        await memory_service.update_settings(
            db, tenant_id=tenant, user_id="u1", long_term_enabled=False
        )
        enabled = await long_term_enabled(db, tenant_id=tenant, user_id="u1")
        assert enabled is False
        # 关闭后检索返回空
        hits = await memory_service.retrieve_relevant(
            db, tenant_id=tenant, user_id="u1", query="任何"
        )
        assert hits == []


# ---------- API ----------


@pytest.mark.asyncio
async def test_memory_api_list_and_overview(app_client):
    """GET /memory 列表 + /memory/overview 统计。"""
    tenant = _tenant()
    await _insert_memory(tenant_id=tenant, user_id=settings.ANONYMOUS_USER_ID, content="用户偏好敏捷开发", status="active")

    headers = {**AUTH, "X-Tenant-Id": tenant}
    resp = await app_client.get("/api/v1/memory", headers=headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] >= 1
    assert any(item["content"] == "用户偏好敏捷开发" for item in data["items"])

    ov = await app_client.get("/api/v1/memory/overview", headers=headers)
    assert ov.status_code == 200
    body = ov.json()["data"]
    assert body["total"] >= 1
    assert body["semantic_count"] >= 1


@pytest.mark.asyncio
async def test_memory_api_timeline(app_client):
    """GET /memory/timeline 按天分组。"""
    tenant = _tenant()
    await _insert_memory(tenant_id=tenant, user_id=settings.ANONYMOUS_USER_ID, content="今天的新记忆")
    headers = {**AUTH, "X-Tenant-Id": tenant}
    resp = await app_client.get("/api/v1/memory/timeline?days=30", headers=headers)
    assert resp.status_code == 200
    groups = resp.json()["data"]["groups"]
    assert len(groups) >= 1
    assert any("今天的新记忆" in item["content"] for g in groups for item in g["items"])


@pytest.mark.asyncio
async def test_memory_api_cross_tenant_404(app_client):
    """跨租户访问返回 404（不泄露存在性）。"""
    tenant = _tenant()
    mid = await _insert_memory(tenant_id=tenant, user_id=settings.ANONYMOUS_USER_ID, content="保密记忆")
    # 用另一租户删除该记忆 → 404
    resp = await app_client.delete(f"/api/v1/memory/{mid}", headers={**AUTH, "X-Tenant-Id": "other-tenant"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_memory_api_clear(app_client):
    """DELETE /memory 清空。"""
    tenant = _tenant()
    await _insert_memory(tenant_id=tenant, user_id=settings.ANONYMOUS_USER_ID, content="待清空记忆")
    headers = {**AUTH, "X-Tenant-Id": tenant}
    resp = await app_client.delete("/api/v1/memory", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["deleted"] >= 1


# ---------- 卫生：TTL 过期 ----------


@pytest.mark.asyncio
async def test_ttl_expiration():
    """超过 TTL 且未强化（access_count<threshold）的记忆自动过期。"""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import text

    from app.memory.hygiene import expire_stale

    tenant = _tenant()
    # 插入一条很久之前的记忆
    mid = await _insert_memory(tenant_id=tenant, user_id="u1", content="很久以前的旧记忆", status="active")
    async with AsyncSessionLocal() as db:
        # 把 created_at 改到 100 天前
        await db.execute(
            text("UPDATE memory_entries SET created_at = :dt WHERE id = :mid"),
            {"dt": datetime.now(timezone.utc) - timedelta(days=100), "mid": mid},
        )
        await db.commit()
        # TTL=30 天 → 应过期
        n = await expire_stale(db, tenant_id=tenant, user_id="u1", ttl_days=30)
        assert n >= 1
        # 过期后不再参与检索
        hits = await memory_service.retrieve_relevant(
            db, tenant_id=tenant, user_id="u1", query="旧记忆"
        )
        assert not any("旧记忆" in h.content for h in hits)


@pytest.mark.asyncio
async def test_ttl_strengthened_not_expired():
    """被强化（access_count>=threshold）的记忆不因 TTL 过期。"""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import text

    from app.memory.hygiene import expire_stale

    tenant = _tenant()
    mid = await _insert_memory(tenant_id=tenant, user_id="u1", content="经常被命中的记忆", status="active")
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                "UPDATE memory_entries SET created_at = :dt, access_count = :ac WHERE id = :mid"
            ),
            {
                "dt": datetime.now(timezone.utc) - timedelta(days=100),
                "ac": settings.MEMORY_STRENGTHEN_THRESHOLD,
                "mid": mid,
            },
        )
        await db.commit()
        n = await expire_stale(db, tenant_id=tenant, user_id="u1", ttl_days=30)
        assert n == 0  # 被强化 → 不过期
        # 仍可检索
        hits = await memory_service.retrieve_relevant(
            db, tenant_id=tenant, user_id="u1", query="经常被命中"
        )
        assert any("经常被命中" in h.content for h in hits)


# ---------- memory_search 工具 ----------


@pytest.mark.asyncio
async def test_memory_search_tool(clean_registry):
    """memory_search 工具注册 + 检索当前用户记忆。"""
    from app.harness.context import ToolContext, reset_tool_context, set_tool_context
    from app.tools.memory_search import memory_search
    from app.tools.registry import tool_registry

    tenant = _tenant()
    await _insert_memory(tenant_id=tenant, user_id="u1", content="用户喜欢用 TypeScript")

    tool_registry.register_builtin()
    assert tool_registry.get("memory_search") is not None

    set_tool_context(ToolContext(tenant_id=tenant, user_id="u1"))
    try:
        result = await memory_search.ainvoke({"query": "用户喜欢的语言", "top_k": 5})
        assert "TypeScript" in str(result)
    finally:
        reset_tool_context()


@pytest.mark.asyncio
async def test_memory_search_tool_missing_context():
    """无上下文时优雅返回提示。"""
    from app.harness.context import reset_tool_context
    from app.tools.memory_search import memory_search

    reset_tool_context()
    result = await memory_search.ainvoke({"query": "任何"})
    assert "上下文" in str(result)
