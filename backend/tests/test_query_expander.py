"""Query 改写测试（AGENTIC_RAG_UPGRADE_SPEC §6 / G-RAG-3.5 验收）：

- 改写成功返回非空结果。
- 失败 / 空白结果 / 空白输入 → 原样返回。
- QUERY_EXPAND_TEMPLATE 非空时优先使用配置模板。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from app.config import settings
from app.knowledge.query_expander import expand_query


class _FakeLLM:
    """llm 替身：可配置 ainvoke 响应或异常。"""

    def __init__(self, content: str = "", exc: Exception | None = None):
        self.content = content
        self.exc = exc
        self.prompts: list[str] = []

    async def ainvoke(self, prompt, **kwargs):
        self.prompts.append(str(prompt))
        if self.exc:
            raise self.exc
        return AIMessage(content=self.content)


@pytest.mark.asyncio
async def test_expand_success():
    """改写成功 → 返回改写结果。"""
    llm = _FakeLLM(content="年假 申请 流程")
    out = await expand_query("我想休年假怎么弄", llm=llm)
    assert out == "年假 申请 流程"
    assert len(llm.prompts) == 1
    assert "我想休年假怎么弄" in llm.prompts[0]


@pytest.mark.asyncio
async def test_expand_failure_returns_original():
    """LLM 异常 → 原样返回（降级红线）。"""
    llm = _FakeLLM(exc=TimeoutError("llm timeout"))
    out = await expand_query("原始问题", llm=llm)
    assert out == "原始问题"


@pytest.mark.asyncio
async def test_expand_blank_result_returns_original():
    """LLM 返回空白 → 原样返回。"""
    llm = _FakeLLM(content="   ")
    out = await expand_query("原始问题", llm=llm)
    assert out == "原始问题"


@pytest.mark.asyncio
async def test_expand_blank_input_returns_as_is():
    """空白输入 → 原样返回且不调用 LLM。"""
    llm = _FakeLLM(content="不应该被调用")
    assert await expand_query("", llm=llm) == ""
    assert await expand_query("   ", llm=llm) == "   "
    assert llm.prompts == []


@pytest.mark.asyncio
async def test_custom_template_takes_priority(monkeypatch):
    """QUERY_EXPAND_TEMPLATE 非空时优先使用配置模板。"""
    monkeypatch.setattr(settings, "QUERY_EXPAND_TEMPLATE", "自定义改写模板：{query}")
    llm = _FakeLLM(content="改写结果")
    await expand_query("问题X", llm=llm)
    assert llm.prompts[0] == "自定义改写模板：问题X"


@pytest.mark.asyncio
async def test_builtin_template_used_by_default(monkeypatch):
    """模板为空时使用内置模板。"""
    monkeypatch.setattr(settings, "QUERY_EXPAND_TEMPLATE", "")
    llm = _FakeLLM(content="改写结果")
    await expand_query("问题Y", llm=llm)
    assert "关键词化表述" in llm.prompts[0]
    assert "问题Y" in llm.prompts[0]


@pytest.mark.asyncio
async def test_multimodal_content_handled():
    """content 为分段列表时合并为文本。"""
    llm = _FakeLLM(content="")
    llm.content = None  # 占位，下面用子类行为模拟

    class _ListLLM(_FakeLLM):
        async def ainvoke(self, prompt, **kwargs):
            return AIMessage(content=[{"type": "text", "text": "列表改写结果"}])

    out = await expand_query("问题Z", llm=_ListLLM())
    assert out == "列表改写结果"
