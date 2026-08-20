"""本地解析降级单元测试：txt/md/pdf/docx 及异常路径。"""

from __future__ import annotations

import pytest

from app.knowledge.parser.local_parser import LocalParseError, parse_local


def test_parse_txt():
    assert parse_local("a.txt", "你好".encode("utf-8")) == "你好"


def test_parse_md():
    text = "# 标题\n内容"
    assert parse_local("a.md", text.encode("utf-8")) == text


def test_parse_pdf():
    """构造最小 PDF（含一行文本），用 pypdf 提取。"""
    # 最小可用 PDF（横向 A4，单行文本）
    pdf = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
        b"4 0 obj<</Length 62>>stream\n"
        b"BT /F1 18 Tf 72 720 Td (Hello PDF) Tj ET\n"
        b"endstream endobj\n"
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
        b"xref\n"
        b"0 6\n"
        b"0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"0000000058 00000 n \n"
        b"0000000115 00000 n \n"
        b"0000000241 00000 n \n"
        b"0000000372 00000 n \n"
        b"trailer<</Size 6/Root 1 0 R>>\n"
        b"startxref\n"
        b"433\n"
        b"%%EOF\n"
    )
    text = parse_local("a.pdf", pdf)
    assert "Hello PDF" in text


def test_parse_unsupported_raises():
    with pytest.raises(LocalParseError, match="不支持"):
        parse_local("a.exe", b"MZ...")


def test_parse_corrupt_pdf_raises():
    with pytest.raises(LocalParseError):
        parse_local("bad.pdf", b"not a real pdf")
