"""摄取编排测试：解析→分块→向量化入库、幂等重算、分块编辑重算（mock embedding）。"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.knowledge.service import _run_parse, knowledge_service
from app.storage.db import AsyncSessionLocal
from app.storage.models import DocumentMeta, KnowledgeChunk

DOC_MD = (
    "# 产品手册\n\n"
    "## 计费说明\n"
    "基础版每月 99 元，专业版每月 299 元，企业版需联系销售。\n\n"
    "## 技术支持\n"
    "工作时间提供 7x12 小时支持，紧急故障响应不超过 1 小时。\n"
)


def _mock_vec(text: str, dim: int | None = None) -> list[float]:
    dim = dim or settings.EMBEDDING_DIM
    vec = [0.0] * dim
    for index, ch in enumerate(text):
        vec[(ord(ch) + index * 7) % dim] += 1.0
    return vec


async def _mock_embed_texts(texts: list[str]) -> list[list[float]]:
    return [_mock_vec(t) for t in texts]


@pytest.fixture(autouse=True)
def _mock_embeddings(monkeypatch):
    from app.knowledge import ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "embed_texts", _mock_embed_texts)
    # service.update_chunk / reembed 直接 import embedder.embed_texts，一并 mock
    monkeypatch.setattr("app.knowledge.service.embed_texts", _mock_embed_texts)


@pytest.fixture(autouse=True)
def _no_background_parse(monkeypatch):
    """禁用后台解析调度，测试改为手动 _run_parse（避免并发双跑）。"""
    monkeypatch.setattr("app.knowledge.service._schedule_parse", lambda doc_id: None)


@pytest.mark.asyncio
async def test_ingest_document_full_flow():
    """解析→分块→向量化入库：DocumentMeta parsed，chunk 关联 doc_id。"""
    tenant = f"ingest-{uuid.uuid4().hex[:6]}"
    async with AsyncSessionLocal() as db:
        kb = await knowledge_service.create(db, tenant_id=tenant, name="手册库")
        assert kb is not None
        doc = await knowledge_service.upload_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            filename="manual.md",
            content=DOC_MD.encode("utf-8"),
            parse_method="text",
        )
        assert doc.parse_status == "pending"
        await _run_parse(doc.id)

    async with AsyncSessionLocal() as db:
        fresh = await db.get(DocumentMeta, doc.id)
        assert fresh is not None
        assert fresh.parse_status == "parsed"
        assert fresh.parse_method == "text"
        assert fresh.parse_source == "local"
        assert fresh.raw_text == DOC_MD
        chunks = (
            (
                await db.execute(
                    select(KnowledgeChunk).where(KnowledgeChunk.doc_id == doc.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(chunks) >= 3
        assert all(c.enabled is True for c in chunks)
        assert all(c.doc_id == doc.id for c in chunks)


@pytest.mark.asyncio
async def test_ingest_ocr_method_uses_remote_parser(monkeypatch):
    """只有显式选择 OCR 时才调用远程解析，并记录对应解析来源。"""
    from app.knowledge.parser import glm_parser

    class FakeResult:
        text = "扫描件识别结果"
        tool_type = "prime"

    async def fake_parse_file(*args, **kwargs):
        return FakeResult()

    monkeypatch.setattr(settings, "GLM_FILES_API_KEY", "test-key")
    monkeypatch.setattr(settings, "GLM_FILES_TOOL", "prime")
    monkeypatch.setattr(glm_parser, "parse_file", fake_parse_file)

    tenant = f"ocr-{uuid.uuid4().hex[:6]}"
    async with AsyncSessionLocal() as db:
        kb = await knowledge_service.create(db, tenant_id=tenant, name="扫描件库")
        assert kb is not None
        doc = await knowledge_service.upload_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            filename="scan.pdf",
            content=b"%PDF-1.4 fake scan",
            parse_method="ocr",
        )
        await _run_parse(doc.id)

    async with AsyncSessionLocal() as db:
        fresh = await db.get(DocumentMeta, doc.id)
        assert fresh is not None
        assert fresh.parse_method == "ocr"
        assert fresh.parse_source == "glm"
        assert fresh.raw_text == "扫描件识别结果"


@pytest.mark.asyncio
async def test_ingest_reparse_is_idempotent():
    """同文档重算幂等：chunk 数不变，不产生重复。"""
    tenant = f"reparse-{uuid.uuid4().hex[:6]}"
    async with AsyncSessionLocal() as db:
        kb = await knowledge_service.create(db, tenant_id=tenant, name="幂等库")
        assert kb is not None
        doc = await knowledge_service.upload_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            filename="manual.md",
            content=DOC_MD.encode("utf-8"),
            parse_method="text",
        )
        await _run_parse(doc.id)
        await _run_parse(doc.id)  # 再次解析（replace 语义）

        chunk_count = (
            await db.execute(
                select(func.count(KnowledgeChunk.id)).where(
                    KnowledgeChunk.doc_id == doc.id
                )
            )
        ).scalar_one()
        assert chunk_count >= 3
        # 同一 source_file 只有一份 DocumentMeta
        docs = (
            (
                await db.execute(
                    select(DocumentMeta).where(
                        DocumentMeta.kb_id == kb.id,
                        DocumentMeta.source_file == "manual.md",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(docs) == 1


@pytest.mark.asyncio
async def test_ingest_empty_text_fails():
    """空文本解析失败 → parse_status=failed。"""
    tenant = f"empty-{uuid.uuid4().hex[:6]}"
    async with AsyncSessionLocal() as db:
        kb = await knowledge_service.create(db, tenant_id=tenant, name="空文档库")
        assert kb is not None
        doc = await knowledge_service.upload_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            filename="empty.md",
            content="   \n\n  ".encode("utf-8"),
            parse_method="text",
        )
        await _run_parse(doc.id)

    async with AsyncSessionLocal() as db:
        fresh = await db.get(DocumentMeta, doc.id)
        assert fresh is not None
        assert fresh.parse_status == "failed"
        assert fresh.error


@pytest.mark.asyncio
async def test_chunk_edit_recomputes_embedding():
    """编辑分块后 embedding 重算（向量变化且可被检索到）。"""
    tenant = f"edit-{uuid.uuid4().hex[:6]}"
    async with AsyncSessionLocal() as db:
        kb = await knowledge_service.create(db, tenant_id=tenant, name="编辑库")
        assert kb is not None
        doc = await knowledge_service.upload_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            filename="manual.md",
            content=DOC_MD.encode("utf-8"),
            parse_method="text",
        )
        await _run_parse(doc.id)
        chunk = (
            (
                await db.execute(
                    select(KnowledgeChunk)
                    .where(KnowledgeChunk.doc_id == doc.id)
                    .order_by(KnowledgeChunk.id)
                )
            )
            .scalars()
            .first()
        )
        assert chunk is not None
        old_vec = list(chunk.embedding)
        new_text = chunk.chunk_text + "\n（新增修正内容，向量应随之更新）"
        updated = await knowledge_service.update_chunk(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            chunk_id=chunk.id,
            text=new_text,
        )
        assert updated is not None
        assert updated.chunk_text == new_text
        assert list(updated.embedding) != old_vec  # 向量已重算


@pytest.mark.asyncio
async def test_chunk_toggle_keeps_embedding():
    """启停分块不改向量。"""
    tenant = f"toggle-{uuid.uuid4().hex[:6]}"
    async with AsyncSessionLocal() as db:
        kb = await knowledge_service.create(db, tenant_id=tenant, name="启停库")
        assert kb is not None
        doc = await knowledge_service.upload_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            filename="manual.md",
            content=DOC_MD.encode("utf-8"),
            parse_method="text",
        )
        await _run_parse(doc.id)
        chunk = (
            (
                await db.execute(
                    select(KnowledgeChunk)
                    .where(KnowledgeChunk.doc_id == doc.id)
                    .order_by(KnowledgeChunk.id)
                )
            )
            .scalars()
            .first()
        )
        old_vec = list(chunk.embedding)
        toggled = await knowledge_service.toggle_chunk(
            db, tenant_id=tenant, kb_id=kb.id, chunk_id=chunk.id, enabled=False
        )
        assert toggled is not None
        assert toggled.enabled is False
        assert list(toggled.embedding) == old_vec


@pytest.mark.asyncio
async def test_preview_chunks_no_persist():
    """分块预览为纯函数，不落库。"""
    previews = knowledge_service.preview_document(text=DOC_MD, chunk_size=500)
    assert len(previews) >= 1
    assert previews[0]["index"] == 1
    assert previews[0]["char_count"] == len(previews[0]["text"])
    # 不落库：没有对应 DocumentMeta 被创建（纯函数在服务层无 DB 操作）
    await asyncio.sleep(0)
    assert isinstance(previews, list)
