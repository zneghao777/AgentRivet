"""溯源闭环测试（KB_MANAGEMENT_SPEC §3.6 / §6）：

- Hit 携带 kb_id / chunk_id 稳定主键。
- hits_to_markdown 输出机器可读引用锚点 `引用: KB-{kb_id}/{chunk_id}`。
- 前端锚点解析正则可提取锚点集合（与 frontend/utils/trace.ts 约定一致）。
"""

from __future__ import annotations

import re
import uuid

from app.knowledge.retriever import Hit, hits_to_markdown

# 与 frontend/src/utils/trace.ts 保持一致的正则
ANCHOR_RE = re.compile(r"KB-([0-9a-fA-F-]{36})/(\d+)")


def _hit(*, chunk_id: int = 12, kb_id: str | None = None) -> Hit:
    return Hit(
        text="正式员工每年享有 12 天带薪年假。",
        source_file="manual.pdf",
        chunk_index=3,
        score=0.87,
        kb_id=uuid.UUID(kb_id) if kb_id else uuid.uuid4(),
        chunk_id=chunk_id,
    )


def test_hit_carries_trace_keys():
    """Hit 携带 kb_id / chunk_id。"""
    hit = _hit(chunk_id=42)
    assert isinstance(hit.kb_id, uuid.UUID)
    assert hit.chunk_id == 42


def test_hits_to_markdown_emits_reference_anchor():
    """markdown 输出 `引用: KB-{kb_id}/{chunk_id}` 锚点。"""
    hit = _hit(chunk_id=12)
    md = hits_to_markdown([hit])
    assert f"引用: KB-{hit.kb_id}/{hit.chunk_id}" in md
    assert "来源: manual.pdf" in md
    assert "相关度 0.87" in md


def test_parse_anchors_from_markdown():
    """解析函数能从 markdown 提取全部锚点（前端 trace.ts 约定）。"""
    hit_a = _hit(chunk_id=12)
    hit_b = _hit(chunk_id=99)
    md = hits_to_markdown([hit_a, hit_b])
    anchors = ANCHOR_RE.findall(md)
    assert len(anchors) == 2
    assert (str(hit_a.kb_id), "12") in anchors
    assert (str(hit_b.kb_id), "99") in anchors


def test_empty_hits_message():
    """空命中返回提示文案，无锚点。"""
    md = hits_to_markdown([])
    assert "未检索到" in md
    assert ANCHOR_RE.findall(md) == []
