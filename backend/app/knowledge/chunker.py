"""分块器：标题层级切分 + 固定长度重叠兜底。

策略（SPEC §6）：
1. 按 Markdown 标题层级（# / ## / ###）切分，每块控制在 RAG_CHUNK_SIZE 内。
2. 超长段按固定长度 + RAG_CHUNK_OVERLAP 重叠切分。
3. 表格内容独立成块。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import settings

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$", re.MULTILINE)
_TABLE_BLOCK_RE = re.compile(
    r"((?:^\|.*\|[^\n]*\n)+)", re.MULTILINE
)


@dataclass
class Chunk:
    """一个分块：文本 + 元数据。"""

    text: str
    heading: str | None = None


@dataclass
class ChunkPreview:
    """分块预览（不落库）：供前端分块预览渲染（KB_MANAGEMENT_SPEC §3.4）。"""

    index: int
    heading: str | None
    text: str
    char_count: int


def _split_by_tables(text: str) -> list[str]:
    """将文本中的表格块独立切出，其余按段返回。"""
    parts: list[str] = []
    last = 0
    for match in _TABLE_BLOCK_RE.finditer(text):
        if match.start() > last:
            parts.append(text[last : match.start()])
        parts.append(match.group(1).strip())
        last = match.end()
    if last < len(text):
        parts.append(text[last:])
    return [p for p in parts if p.strip()]


def _fixed_length_chunk(text: str, chunk_size: int) -> list[str]:
    """固定长度 + 重叠兜底切分。"""
    size = chunk_size
    overlap = settings.RAG_CHUNK_OVERLAP
    step = max(1, size - overlap)
    if len(text) <= size:
        return [text] if text.strip() else []
    return [
        text[i : i + size]
        for i in range(0, len(text), step)
        if text[i : i + size].strip()
    ]


def chunk_text(text: str, chunk_size: int | None = None) -> list[Chunk]:
    """将纯文本按标题层级 + 固定长度切分为 Chunk 列表。

    chunk_size 可配置（默认 settings.RAG_CHUNK_SIZE，窗口可选 256/500/1000）。
    metadata（heading）由调用方结合 source_file 等补充。
    """
    size = chunk_size or settings.RAG_CHUNK_SIZE
    chunks: list[Chunk] = []
    # 先按表格块切分
    for block in _split_by_tables(text):
        if _TABLE_BLOCK_RE.match(block) and block.startswith("|"):
            # 表格独立成块
            chunks.append(Chunk(text=block.strip()))
            continue
        # 按标题层级切分
        matches = list(_HEADING_RE.finditer(block))
        if not matches:
            chunks.extend(
                Chunk(text=c) for c in _fixed_length_chunk(block.strip(), size)
            )
            continue
        positions = [(m.start(), m.group(1), m.group(2).strip()) for m in matches]
        for idx, (start, _level, heading) in enumerate(positions):
            end = positions[idx + 1][0] if idx + 1 < len(positions) else len(block)
            section = block[start:end].strip()
            if not section:
                continue
            if len(section) <= size:
                chunks.append(Chunk(text=section, heading=heading))
            else:
                # 超长小节：去掉标题行后按固定长度切分
                body = section
                for c in _fixed_length_chunk(body, size):
                    chunks.append(Chunk(text=c, heading=heading))
    return chunks


def preview_chunks(
    text: str, chunk_size: int | None = None
) -> list[dict]:
    """纯函数分块预览：不落库，返回 [{index, heading, text, char_count}]。"""
    return [
        {
            "index": index,
            "heading": chunk.heading,
            "text": chunk.text,
            "char_count": len(chunk.text),
        }
        for index, chunk in enumerate(chunk_text(text, chunk_size), start=1)
    ]
