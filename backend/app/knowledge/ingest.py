"""摄取编排：解析文件 → chunker 分块 → embed → 批量入库 → 更新统计。

支持格式：.md / .txt（直接读文本）；.pdf（pypdf 纯文本提取）；.docx（python-docx 提取段落）。
"""

from __future__ import annotations

import io
import logging
import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..storage.models import KnowledgeBase, KnowledgeChunk
from .chunker import chunk_text
from .embedding import EmbeddingError, embed_texts

logger = logging.getLogger(__name__)

ALLOWED_EXTS = settings.rag_allowed_exts_list
MAX_FILE_SIZE_BYTES = settings.RAG_MAX_FILE_SIZE_MB * 1024 * 1024


class IngestError(Exception):
    """摄取异常（格式不支持 / 大小超限等）。"""


def validate_file(filename: str, content: bytes) -> str:
    """校验扩展名白名单与大小上限，返回小写扩展名。"""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if f".{ext}" not in ALLOWED_EXTS:
        raise IngestError(
            f"不支持的文件格式 .{ext}（允许: {', '.join(ALLOWED_EXTS)}）"
        )
    if len(content) > MAX_FILE_SIZE_BYTES:
        raise IngestError(
            f"文件大小超过上限 {settings.RAG_MAX_FILE_SIZE_MB}MB"
        )
    return ext


def extract_text(filename: str, content: bytes) -> str:
    """按扩展名解析文件为纯文本。"""
    ext = filename.rsplit(".", 1)[-1].lower()
    try:
        if ext in ("md", "txt"):
            return content.decode("utf-8", errors="replace")
        if ext == "pdf":
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(content))
            pages = [page.extract_text() or "" for page in reader.pages]
            return "\n".join(pages)
        if ext == "docx":
            import docx

            doc = docx.Document(io.BytesIO(content))
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            return "\n".join(paragraphs)
        raise IngestError(f"不支持的文件格式 .{ext}")
    except IngestError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise IngestError(f"文件解析失败: {exc}") from exc


async def ingest_document(
    db: AsyncSession,
    *,
    kb_id: uuid.UUID,
    tenant_id: str,
    filename: str,
    content: bytes,
    replace: bool = True,
) -> tuple[int, int]:
    """摄取单个文档，返回 (doc_id, chunk_count)。

    replace=True（默认）：先删除该文件旧 chunks 再写入，保证幂等（SPEC §6）。
    """
    validate_file(filename, content)
    text = extract_text(filename, content)
    if not text.strip():
        raise IngestError("文件内容为空，无可摄取文本")

    chunks = chunk_text(text)
    if not chunks:
        raise IngestError("分块结果为空，无法入库")

    # 幂等：替换旧 chunks
    if replace:
        await db.execute(
            delete(KnowledgeChunk).where(
                KnowledgeChunk.kb_id == kb_id,
                KnowledgeChunk.tenant_id == tenant_id,
                KnowledgeChunk.source_file == filename,
            )
        )

    try:
        vectors = await embed_texts([c.text for c in chunks])
    except EmbeddingError as exc:
        raise IngestError(str(exc)) from exc
    if len(vectors) != len(chunks):
        raise IngestError("向量化结果与分块数量不一致")

    rows = [
        KnowledgeChunk(
            kb_id=kb_id,
            tenant_id=tenant_id,
            source_file=filename,
            chunk_index=index,
            chunk_text=chunk.text,
            embedding=vector,
            meta={
                "source_file": filename,
                "chunk_index": index,
                "heading": chunk.heading,
            },
        )
        for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=False))
    ]
    db.add_all(rows)
    await db.flush()

    # 更新知识库统计（事务内，doc_count 按 source_file 去重）
    await _refresh_stats(db, kb_id, tenant_id)
    await db.commit()
    logger.info(
        "摄取完成 kb=%s file=%s chunks=%d", kb_id, filename, len(rows)
    )
    return rows[0].id, len(rows)


async def _refresh_stats(
    db: AsyncSession, kb_id: uuid.UUID, tenant_id: str
) -> None:
    """重算知识库的 doc_count / chunk_count。"""
    chunk_count = (
        await db.execute(
            select(func.count(KnowledgeChunk.id)).where(
                KnowledgeChunk.kb_id == kb_id,
                KnowledgeChunk.tenant_id == tenant_id,
            )
        )
    ).scalar_one()
    doc_count = (
        await db.execute(
            select(func.count(func.distinct(KnowledgeChunk.source_file))).where(
                KnowledgeChunk.kb_id == kb_id,
                KnowledgeChunk.tenant_id == tenant_id,
            )
        )
    ).scalar_one()
    await db.execute(
        update(KnowledgeBase)
        .where(
            KnowledgeBase.id == kb_id,
            KnowledgeBase.tenant_id == tenant_id,
        )
        .values(doc_count=doc_count, chunk_count=chunk_count)
    )


async def delete_document_chunks(
    db: AsyncSession,
    *,
    kb_id: uuid.UUID,
    tenant_id: str,
    source_file: str,
) -> None:
    """删除单个文档的所有 chunks 并刷新统计。"""
    await db.execute(
        delete(KnowledgeChunk).where(
            KnowledgeChunk.kb_id == kb_id,
            KnowledgeChunk.tenant_id == tenant_id,
            KnowledgeChunk.source_file == source_file,
        )
    )
    await _refresh_stats(db, kb_id, tenant_id)
    await db.commit()
