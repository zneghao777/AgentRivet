"""知识库（RAG）API 测试：CRUD / 文档解析任务 / 分块治理 / 检索 / 租户隔离 / 审计。

embedding 使用确定性 mock（基于字符哈希的非负向量，余弦相似度恒 >= 0），
避免测试依赖真实 Embedding API。上传走异步后台解析（同事件循环调度），
测试通过轮询文档列表等待解析完成。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.config import settings
from app.observability import audit
from app.storage.db import AsyncSessionLocal
from app.storage.models import AuditLog

AUTH = {"X-API-Key": "test-key-1"}
AUTH_A = {**AUTH, "X-Tenant-Id": "tenant-a"}
AUTH_B = {**AUTH, "X-Tenant-Id": "tenant-b"}


def _unique(name: str) -> str:
    """生成运行间不冲突的知识库名称（DB 数据跨用例/跨运行保留）。"""
    return f"{name}-{uuid.uuid4().hex[:6]}"


DOC_MD = (
    "# 员工手册\n\n"
    "## 休假制度\n"
    "正式员工每年享有 12 天带薪年假，需提前 3 天申请。\n\n"
    "## 考勤规则\n"
    "工作日为周一至周五，弹性工作时间为 9:00-18:00。\n"
)


def _mock_vec(text: str, dim: int | None = None) -> list[float]:
    """确定性 mock 向量：字符哈希到桶，保证非负（余弦 >= 0）。"""
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
    """全局 mock embedding，避免真实 API 调用。"""
    from app.knowledge import ingest as ingest_mod
    from app.knowledge import retriever as retriever_mod

    monkeypatch.setattr(ingest_mod, "embed_texts", _mock_embed_texts)
    monkeypatch.setattr(retriever_mod, "embed_query", _mock_embed_query)
    # service.update_chunk / reembed 直接 import embedder.embed_texts，一并 mock
    monkeypatch.setattr("app.knowledge.service.embed_texts", _mock_embed_texts)


@pytest.fixture(autouse=True)
async def _init_audit():
    """初始化审计写入器（幂等），使审计记录落库（需在事件循环内创建后台任务）。"""
    audit.init_audit(lambda: AsyncSessionLocal())
    yield
    await _flush_audit()


async def _flush_audit() -> None:
    """等待审计后台队列写完。"""
    await asyncio.sleep(0.3)


async def _create_kb(client, *, tenant: dict[str, str], name: str) -> str:
    resp = await client.post(
        "/api/v1/knowledge-bases", json={"name": name}, headers=tenant
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    return body["data"]["kb_id"]


async def _upload_doc(client, kb_id: str, *, tenant: dict[str, str], filename: str = "guide.md") -> dict:
    """上传文档并轮询等待异步解析完成，返回文档 dict。"""
    resp = await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents/upload",
        files={"file": (filename, DOC_MD.encode("utf-8"), "text/markdown")},
        headers=tenant,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["parse_status"] == "pending"
    # 轮询文档列表等待后台解析完成
    for _ in range(40):
        await asyncio.sleep(0.05)
        resp = await client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=tenant)
        docs = resp.json()["data"]["documents"]
        match = next((d for d in docs if d["source_file"] == filename), None)
        if match and match["parse_status"] in ("parsed", "failed"):
            assert match["parse_status"] == "parsed", match.get("error")
            return match
    raise AssertionError("后台解析未在预期时间内完成")


@pytest.mark.asyncio
async def test_kb_create_list_get(app_client):
    """知识库 CRUD：创建 → 列表 → 详情。"""
    name = _unique("员工手册")
    kb_id = await _create_kb(app_client, tenant=AUTH_A, name=name)

    # 列表
    resp = await app_client.get("/api/v1/knowledge-bases", headers=AUTH_A)
    items = resp.json()["data"]
    assert any(item["kb_id"] == kb_id and item["name"] == name for item in items)

    # 详情 + 文档列表
    resp = await app_client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=AUTH_A)
    assert resp.status_code == 200
    detail = resp.json()["data"]
    assert detail["kb"]["name"] == name
    assert detail["documents"] == []


@pytest.mark.asyncio
async def test_kb_duplicate_name_conflict(app_client):
    """同名知识库返回 409。"""
    name = _unique("重名库")
    await _create_kb(app_client, tenant=AUTH_A, name=name)
    resp = await app_client.post(
        "/api/v1/knowledge-bases", json={"name": name}, headers=AUTH_A
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_kb_can_be_disabled_and_reenabled(app_client):
    """知识库开关保留数据，并控制管理状态。"""
    kb_id = await _create_kb(app_client, tenant=AUTH_A, name=_unique("开关库"))

    resp = await app_client.patch(
        f"/api/v1/knowledge-bases/{kb_id}/status",
        json={"enabled": False},
        headers=AUTH_A,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "inactive"

    items = (await app_client.get("/api/v1/knowledge-bases", headers=AUTH_A)).json()["data"]
    assert any(item["kb_id"] == kb_id and item["status"] == "inactive" for item in items)

    resp = await app_client.patch(
        f"/api/v1/knowledge-bases/{kb_id}/status",
        json={"enabled": True},
        headers=AUTH_A,
    )
    assert resp.json()["data"]["status"] == "active"


@pytest.mark.asyncio
async def test_kb_tenant_isolation(app_client):
    """跨租户访问知识库返回 404（不泄露存在性）。"""
    kb_id = await _create_kb(app_client, tenant=AUTH_A, name=_unique("私密库"))

    # 其他租户不可见
    resp = await app_client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=AUTH_B)
    assert resp.status_code == 404

    # 其他租户列表不含该库
    resp = await app_client.get("/api/v1/knowledge-bases", headers=AUTH_B)
    items = resp.json()["data"]
    assert all(item["kb_id"] != kb_id for item in items)

    # 其他租户删除返回 404
    resp = await app_client.delete(f"/api/v1/knowledge-bases/{kb_id}", headers=AUTH_B)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_upload_document_updates_stats(app_client):
    """上传 .md 文档后 doc_count / chunk_count 正确更新，parse_status=parsed。"""
    kb_id = await _create_kb(app_client, tenant=AUTH_A, name=_unique("统计手册"))
    doc = await _upload_doc(app_client, kb_id, tenant=AUTH_A)
    assert doc["parse_status"] == "parsed"
    assert doc["parse_source"] in ("glm", "local")
    assert doc["chunk_count"] >= 3  # 标题分块产生多个

    # 详情统计
    resp = await app_client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=AUTH_A)
    detail = resp.json()["data"]
    assert detail["kb"]["doc_count"] == 1
    assert detail["kb"]["chunk_count"] == doc["chunk_count"]
    assert len(detail["documents"]) == 1
    assert detail["documents"][0]["source_file"] == "guide.md"


@pytest.mark.asyncio
async def test_upload_document_replaces_old_chunks(app_client):
    """replace=true 幂等：重复上传不增加 doc_count。"""
    kb_id = await _create_kb(app_client, tenant=AUTH_A, name=_unique("幂等库"))

    for _ in range(2):
        await _upload_doc(app_client, kb_id, tenant=AUTH_A)

    resp = await app_client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=AUTH_A)
    detail = resp.json()["data"]
    assert detail["kb"]["doc_count"] == 1


@pytest.mark.asyncio
async def test_upload_invalid_extension(app_client):
    """非法扩展名返回 400。"""
    kb_id = await _create_kb(app_client, tenant=AUTH_A, name=_unique("校验库"))
    resp = await app_client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents/upload",
        files={"file": ("evil.exe", b"MZ...", "application/octet-stream")},
        headers=AUTH_A,
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_upload_returns_pending_then_parsed(app_client):
    """上传立即返回 pending，轮询列表出现 parse_status=parsed。"""
    kb_id = await _create_kb(app_client, tenant=AUTH_A, name=_unique("异步解析库"))
    await _upload_doc(app_client, kb_id, tenant=AUTH_A)
    # _upload_doc 内部已断言 pending → parsed 流转


@pytest.mark.asyncio
async def test_reparse_document(app_client):
    """重新解析接口返回 reparsing 状态并最终 parsed。"""
    kb_id = await _create_kb(app_client, tenant=AUTH_A, name=_unique("重解析库"))
    doc = await _upload_doc(app_client, kb_id, tenant=AUTH_A)

    resp = await app_client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents/{doc['doc_id']}/reparse",
        headers=AUTH_A,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["parse_status"] in ("reparsing", "parsed")

    # 等待重新解析完成
    for _ in range(40):
        await asyncio.sleep(0.05)
        resp = await app_client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=AUTH_A)
        match = next(
            (d for d in resp.json()["data"]["documents"] if d["doc_id"] == doc["doc_id"]), None
        )
        if match and match["parse_status"] in ("parsed", "failed"):
            assert match["parse_status"] == "parsed"
            return
    raise AssertionError("重新解析未完成")


@pytest.mark.asyncio
async def test_chunk_list_preview_update_toggle_reembed(app_client):
    """分块列表 / 预览 / 编辑(重算) / 启停 / 重新向量化全链路。"""
    kb_id = await _create_kb(app_client, tenant=AUTH_A, name=_unique("分块治理库"))
    doc = await _upload_doc(app_client, kb_id, tenant=AUTH_A)

    # 分块列表（按 doc 过滤）
    resp = await app_client.get(
        f"/api/v1/knowledge-bases/{kb_id}/chunks",
        params={"doc_id": doc["doc_id"], "page_size": 50},
        headers=AUTH_A,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == doc["chunk_count"]
    assert data["items"][0]["kb_id"] == kb_id
    chunk = data["items"][0]
    assert chunk["enabled"] is True

    # 关键词过滤
    resp = await app_client.get(
        f"/api/v1/knowledge-bases/{kb_id}/chunks",
        params={"q": "休假"},
        headers=AUTH_A,
    )
    filtered = resp.json()["data"]
    assert filtered["total"] >= 1
    assert any("休假" in item["text"] for item in filtered["items"])

    # 分块预览（不落库）
    resp = await app_client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents/{doc['doc_id']}/preview",
        json={"text": DOC_MD, "chunk_size": 500},
        headers=AUTH_A,
    )
    assert resp.status_code == 200
    previews = resp.json()["data"]
    assert len(previews) >= 1
    assert previews[0]["index"] == 1
    assert "char_count" in previews[0]

    # 编辑分块（自动重新向量化）
    new_text = chunk["text"] + "\n（已编辑补充内容）"
    resp = await app_client.put(
        f"/api/v1/knowledge-bases/{kb_id}/chunks/{chunk['chunk_id']}",
        json={"text": new_text},
        headers=AUTH_A,
    )
    assert resp.status_code == 200
    updated = resp.json()["data"]
    assert updated["text"] == new_text
    assert updated["char_count"] == len(new_text)

    # 停用分块（不重算）
    resp = await app_client.patch(
        f"/api/v1/knowledge-bases/{kb_id}/chunks/{chunk['chunk_id']}/enabled",
        json={"enabled": False},
        headers=AUTH_A,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["enabled"] is False

    # 重新向量化
    resp = await app_client.post(
        f"/api/v1/knowledge-bases/{kb_id}/chunks/{chunk['chunk_id']}/re-embed",
        headers=AUTH_A,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["enabled"] is False  # 重算不改 enabled


@pytest.mark.asyncio
async def test_test_search_returns_hits_with_trace_keys(app_client):
    """测试检索返回 top-k 片段，携带溯源主键 kb_id / chunk_id。"""
    kb_id = await _create_kb(app_client, tenant=AUTH_A, name=_unique("检索手册"))
    await _upload_doc(app_client, kb_id, tenant=AUTH_A)

    resp = await app_client.post(
        f"/api/v1/knowledge-bases/{kb_id}/test-search",
        json={"query": "员工手册 休假制度 考勤", "top_k": 5},
        headers=AUTH_A,
    )
    assert resp.status_code == 200
    hits = resp.json()["data"]
    assert len(hits) >= 1
    assert hits[0]["source_file"] == "guide.md"
    assert hits[0]["kb_id"] == kb_id
    assert hits[0]["chunk_id"] is not None
    assert "score" in hits[0]


@pytest.mark.asyncio
async def test_disabled_chunk_excluded_from_search(app_client):
    """停用的分块不被 test-search 检索到。"""
    kb_id = await _create_kb(app_client, tenant=AUTH_A, name=_unique("停用分块库"))
    doc = await _upload_doc(app_client, kb_id, tenant=AUTH_A)

    resp = await app_client.get(
        f"/api/v1/knowledge-bases/{kb_id}/chunks",
        params={"doc_id": doc["doc_id"], "page_size": 50},
        headers=AUTH_A,
    )
    chunk = resp.json()["data"]["items"][0]

    # 停用后检索，该分块不应出现在结果中
    resp = await app_client.patch(
        f"/api/v1/knowledge-bases/{kb_id}/chunks/{chunk['chunk_id']}/enabled",
        json={"enabled": False},
        headers=AUTH_A,
    )
    assert resp.status_code == 200
    resp = await app_client.post(
        f"/api/v1/knowledge-bases/{kb_id}/test-search",
        json={"query": chunk["text"][:20], "top_k": 10},
        headers=AUTH_A,
    )
    hits = resp.json()["data"]
    assert all(h["chunk_id"] != chunk["chunk_id"] for h in hits)


@pytest.mark.asyncio
async def test_chunk_tenant_isolation(app_client):
    """跨租户访问分块返回 404。"""
    kb_id = await _create_kb(app_client, tenant=AUTH_A, name=_unique("隔离分块库"))
    doc = await _upload_doc(app_client, kb_id, tenant=AUTH_A)

    resp = await app_client.get(f"/api/v1/knowledge-bases/{kb_id}/chunks", headers=AUTH_A)
    chunk_id = resp.json()["data"]["items"][0]["chunk_id"]

    resp = await app_client.get(
        f"/api/v1/knowledge-bases/{kb_id}/chunks",
        params={"doc_id": doc["doc_id"]},
        headers=AUTH_B,
    )
    assert resp.status_code == 404

    resp = await app_client.put(
        f"/api/v1/knowledge-bases/{kb_id}/chunks/{chunk_id}",
        json={"text": "越权修改"},
        headers=AUTH_B,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_document(app_client):
    """删除单个文档后统计归零。"""
    kb_id = await _create_kb(app_client, tenant=AUTH_A, name=_unique("删除文档库"))
    doc = await _upload_doc(app_client, kb_id, tenant=AUTH_A)

    resp = await app_client.delete(
        f"/api/v1/knowledge-bases/{kb_id}/documents/{doc['doc_id']}", headers=AUTH_A
    )
    assert resp.status_code == 200

    resp = await app_client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=AUTH_A)
    detail = resp.json()["data"]
    assert detail["kb"]["doc_count"] == 0
    assert detail["kb"]["chunk_count"] == 0
    assert detail["documents"] == []


@pytest.mark.asyncio
async def test_delete_knowledge_base(app_client):
    """删除知识库后不可访问。"""
    kb_id = await _create_kb(app_client, tenant=AUTH_A, name=_unique("待删库"))
    resp = await app_client.delete(f"/api/v1/knowledge-bases/{kb_id}", headers=AUTH_A)
    assert resp.status_code == 200

    resp = await app_client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=AUTH_A)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_knowledge_audit_records(app_client):
    """upload / search / chunk 治理均有审计记录。"""
    kb_id = await _create_kb(app_client, tenant=AUTH_A, name=_unique("审计库"))
    doc = await _upload_doc(app_client, kb_id, tenant=AUTH_A)
    await app_client.post(
        f"/api/v1/knowledge-bases/{kb_id}/test-search",
        json={"query": "休假制度", "top_k": 3},
        headers=AUTH_A,
    )
    resp = await app_client.get(f"/api/v1/knowledge-bases/{kb_id}/chunks", headers=AUTH_A)
    chunk_id = resp.json()["data"]["items"][0]["chunk_id"]
    await app_client.patch(
        f"/api/v1/knowledge-bases/{kb_id}/chunks/{chunk_id}/enabled",
        json={"enabled": False},
        headers=AUTH_A,
    )
    await _flush_audit()

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AuditLog.action).where(
                AuditLog.tenant_id == "tenant-a",
                AuditLog.action.like("knowledge.%"),
            )
        )
        actions = {row[0] for row in result.fetchall()}

    assert "knowledge.create" in actions
    assert "knowledge.upload" in actions
    assert "knowledge.test_search" in actions
    assert "knowledge.chunk.toggle" in actions
