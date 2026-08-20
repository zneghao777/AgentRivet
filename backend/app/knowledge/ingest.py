"""摄取编排：解析 → 分块 → 向量化 → 入库（支持 doc_id / 幂等重算）。

KB_MANAGEMENT_SPEC §3.4：
- 解析：GLM 异步解析优先（有 key 且文件类型受支持），否则回退本地解析。
- 入库：解析完成 → 分块 → 逐块向量化 → 写 KnowledgeChunk（含 doc_id）。
  同文档重算时先删旧 chunk 再写新（幂等）。
- 局部重算：编辑某个 chunk 文本 → 仅重算该 chunk 的 embedding（service.update_chunk）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..storage.models import DocumentMeta, KnowledgeBase, KnowledgeChunk
from .chunker import chunk_text
from .embedder import EmbeddingError, embed_texts
from .files import read_upload
from .parser.glm_parser import FILE_TYPES, tool_supports
from .parser.local_parser import LocalParseError, parse_local

logger = logging.getLogger(__name__)

ALLOWED_EXTS = settings.rag_allowed_exts_list
MAX_FILE_SIZE_BYTES = settings.RAG_MAX_FILE_SIZE_MB * 1024 * 1024
# GLM 支持解析的全部类型（含图片，图片自动走 OCR）
GLM_SUPPORTED_EXTS = set(FILE_TYPES)


class IngestError(Exception):
    """摄取异常（格式不支持 / 大小超限 / 解析失败等）。"""


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


def _now() -> datetime:
    return datetime.now(UTC)


async def _resolve_text(doc: DocumentMeta) -> tuple[str, str, str]:
    """解析文档文本，返回 (text, parse_source, parser_tool)。

    - 有 GLM key 且工具支持该类型 → GLM 异步解析（multipart 直传文件字节），
      图片（PNG/JPG/JPEG…）由 GLM OCR 识别文本。
    - GLM 失败 / 无 key → 本地解析降级（parse_source="local"）；
      本地无法解析（如图片）且 GLM 不可用时抛 IngestError 明确提示。
    """
    ext = doc.source_file.rsplit(".", 1)[-1].lower() if "." in doc.source_file else ""
    content = read_upload(doc.file_path or "") if doc.file_path else b""
    tool = settings.GLM_FILES_TOOL
    if settings.GLM_FILES_API_KEY and ext in GLM_SUPPORTED_EXTS and tool_supports(tool, ext):
        try:
            from .parser.glm_parser import GlmParseError, parse_file

            result = await parse_file(
                settings.GLM_FILES_API_KEY,
                content,
                doc.source_file,
                tool,
            )
            if not result.text:
                raise GlmParseError("GLM 解析结果为空")
            logger.info(
                "GLM 解析成功 doc=%s source=%s tool=%s",
                doc.id,
                doc.source_file,
                result.tool_type,
            )
            return result.text, "glm", result.tool_type
        except GlmParseError as exc:
            # 记录原因，降级本地解析
            logger.warning("GLM 解析失败，回退本地解析 doc=%s: %s", doc.id, exc)
            doc.error = f"GLM 解析失败已回退本地: {exc}"
    try:
        text = parse_local(doc.source_file, content)
    except LocalParseError as exc:
        if settings.GLM_FILES_API_KEY:
            raise IngestError(
                f"文件类型 .{ext} 无法本地解析，且 GLM 工具 {tool} 不支持该类型"
                "（图片 OCR 请配置 GLM_FILES_TOOL=prime 或 lite）"
            ) from exc
        raise IngestError(str(exc)) from exc
    return text, "local", ""


async def _embed_chunks(
    db: AsyncSession,
    *,
    kb_id,
    tenant_id: str,
    doc_id: int,
    source_file: str,
    chunks,
) -> list[KnowledgeChunk]:
    """分块向量化并构造入库行。"""
    try:
        vectors = await embed_texts([c.text for c in chunks])
    except EmbeddingError as exc:
        raise IngestError(str(exc)) from exc
    if len(vectors) != len(chunks):
        raise IngestError("向量化结果与分块数量不一致")
    return [
        KnowledgeChunk(
            kb_id=kb_id,
            tenant_id=tenant_id,
            doc_id=doc_id,
            source_file=source_file,
            chunk_index=index,
            chunk_text=chunk.text,
            embedding=vector,
            enabled=True,
            meta={
                "source_file": source_file,
                "chunk_index": index,
                "heading": chunk.heading,
            },
        )
        for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=False))
    ]


async def _refresh_stats(db: AsyncSession, kb_id, tenant_id: str) -> None:
    """重算知识库 doc_count / chunk_count。

    doc_count 以已解析成功的 DocumentMeta 为准；chunk_count 为全量分块数。
    """
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
            select(func.count(func.distinct(DocumentMeta.id))).where(
                DocumentMeta.kb_id == kb_id,
                DocumentMeta.tenant_id == tenant_id,
                DocumentMeta.parse_status == "parsed",
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
    await db.commit()


async def parse_and_ingest(db: AsyncSession, doc: DocumentMeta) -> int:
    """解析 + 分块 + 向量化 + 入库（幂等），返回写入的 chunk 数。

    doc 需已加载（kb_id / tenant_id / source_file / file_path）。
    成功 → parse_status=parsed；失败 → parse_status=failed + error（允许重试）。
    """
    doc.parse_status = "parsing"
    doc.error = None
    await db.commit()
    try:
        text, source, tool = await _resolve_text(doc)
    except (LocalParseError, OSError) as exc:
        doc.parse_status = "failed"
        doc.error = f"解析失败: {exc}"
        await db.commit()
        logger.error("解析失败 doc=%s: %s", doc.id, exc)
        raise IngestError(str(exc)) from exc

    if not text.strip():
        doc.parse_status = "failed"
        doc.error = "文件内容为空，无可摄取文本"
        await db.commit()
        raise IngestError("文件内容为空，无可摄取文本")

    doc.raw_text = text
    doc.parse_source = source
    doc.parser_tool = tool or None
    await db.commit()

    try:
        chunks = chunk_text(text)
        if not chunks:
            raise IngestError("分块结果为空，无法入库")
        # 幂等：先删旧 chunk（同一 doc_id）再写新
        await db.execute(
            delete(KnowledgeChunk).where(
                KnowledgeChunk.doc_id == doc.id,
                KnowledgeChunk.kb_id == doc.kb_id,
                KnowledgeChunk.tenant_id == doc.tenant_id,
            )
        )
        rows = await _embed_chunks(
            db,
            kb_id=doc.kb_id,
            tenant_id=doc.tenant_id,
            doc_id=doc.id,
            source_file=doc.source_file,
            chunks=chunks,
        )
        db.add_all(rows)
        await db.flush()
        doc.parse_status = "parsed"
        doc.updated_at = _now()
        await db.commit()
        # 统计需在 parse_status=parsed 提交后计算（doc_count 依赖该状态）
        await _refresh_stats(db, doc.kb_id, doc.tenant_id)
        logger.info(
            "解析完成 doc=%s file=%s chunks=%d source=%s",
            doc.id,
            doc.source_file,
            len(rows),
            source,
        )
        return len(rows)
    except IngestError:
        doc.parse_status = "failed"
        doc.error = doc.error or "摄取失败"
        await db.commit()
        raise
