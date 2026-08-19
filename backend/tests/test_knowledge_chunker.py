"""分块器单元测试（纯 Python，无 DB 依赖）。"""

from __future__ import annotations

from app.config import settings
from app.knowledge.chunker import chunk_text


def test_fixed_length_chunking():
    """超长文本按固定长度 + 重叠切分。"""
    text = "字" * (settings.RAG_CHUNK_SIZE * 3 + 10)
    chunks = chunk_text(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk.text) <= settings.RAG_CHUNK_SIZE + settings.RAG_CHUNK_OVERLAP


def test_heading_based_chunking():
    """Markdown 标题层级切分。"""
    md = (
        "# 第一章 概述\n"
        "这是第一章的内容，介绍产品背景。\n\n"
        "## 1.1 目标\n"
        "本节描述项目目标。\n\n"
        "## 1.2 范围\n"
        "本节描述项目范围。\n"
    )
    chunks = chunk_text(md)
    # 至少三个标题分块
    headings = [c.heading for c in chunks]
    assert "第一章 概述" in headings
    assert "1.1 目标" in headings
    assert "1.2 范围" in headings
    # 标题块内容包含标题行
    assert any("第一章 概述" in c.text for c in chunks)


def test_table_chunking():
    """表格内容独立成块。"""
    md = (
        "表格说明：\n"
        "| 列A | 列B |\n"
        "| --- | --- |\n"
        "| 1 | 2 |\n"
        "| 3 | 4 |\n"
    )
    chunks = chunk_text(md)
    assert any(c.text.startswith("|") for c in chunks)


def test_empty_text():
    """空文本返回空列表。"""
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_short_text_single_chunk():
    """短文本单个分块。"""
    chunks = chunk_text("你好，这是一段简短的测试文本。")
    assert len(chunks) == 1
    assert "简短" in chunks[0].text
