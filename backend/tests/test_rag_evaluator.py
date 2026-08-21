"""RAG 评测计算测试（AGENTIC_RAG_UPGRADE_SPEC §6 / G-RAG-4 验收）：

- hit_rate / mrr 纯函数：全中 / 全不中 / 部分命中 / 多命中取首。
- parse_dataset：非法用例跳过并计数。
- LLM-judge：结构化输出解析 / markdown 包裹容忍 / 失败记 0 / 分数越界钳制。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from app.knowledge import evaluator
from app.knowledge.evaluator import (
    hit_rate,
    judge_answer,
    mrr,
    parse_dataset,
)

# ---------- hit_rate / mrr 纯函数 ----------


def test_hit_rate_all_hit():
    results = [["1", "2"], ["3"]]
    expected = [{"1"}, {"3"}]
    assert hit_rate(results, expected) == 1.0


def test_hit_rate_none_hit():
    results = [["1"], ["2"]]
    expected = [{"9"}, {"8"}]
    assert hit_rate(results, expected) == 0.0


def test_hit_rate_partial():
    results = [["1"], ["9"], ["3"]]
    expected = [{"1"}, {"8"}, {"3"}]
    assert hit_rate(results, expected) == pytest.approx(2 / 3)


def test_hit_rate_empty():
    assert hit_rate([], []) == 0.0


def test_mrr_first_position():
    assert mrr([["1", "2"]], [{"1"}]) == 1.0


def test_mrr_second_position():
    assert mrr([["x", "1"]], [{"1"}]) == pytest.approx(0.5)


def test_mrr_no_hit():
    assert mrr([["x"]], [{"1"}]) == 0.0


def test_mrr_multiple_cases_averaged():
    results = [["1"], ["x", "2"]]
    expected = [{"1"}, {"2"}]
    assert mrr(results, expected) == pytest.approx((1.0 + 0.5) / 2)


def test_mrr_multi_match_takes_first_rank():
    """expected 多命中时取首个命中 rank。"""
    assert mrr([["a", "b"]], [{"a", "b"}]) == 1.0
    assert mrr([["a", "b"]], [{"b"}]) == pytest.approx(0.5)


# ---------- parse_dataset ----------


def test_parse_dataset_valid():
    cases, skipped = parse_dataset(
        [
            {"query": "q1", "expected_chunk_ids": [1, 2]},
            {"query": "q2", "expected_doc_ids": [7]},
            {"query": "q3", "expected_chunk_ids": ["5"], "reference_answer": "答案"},
        ]
    )
    assert skipped == 0
    assert len(cases) == 3
    assert cases[0].expected_chunk_ids == {"1", "2"}
    assert cases[1].expected_doc_ids == {"7"}
    assert cases[2].reference_answer == "答案"


def test_parse_dataset_skips_invalid():
    cases, skipped = parse_dataset(
        [
            {"query": "ok", "expected_chunk_ids": [1]},  # 合法
            {"query": "", "expected_chunk_ids": [1]},  # 缺 query
            {"expected_chunk_ids": [1]},  # 缺 query
            {"query": "no-target"},  # 无 expected
            {"query": "no-target", "expected_chunk_ids": []},  # 空 expected
            "not-a-dict",  # 非 dict
            None,
        ]
    )
    assert len(cases) == 1
    assert skipped == 6


def test_parse_dataset_empty():
    cases, skipped = parse_dataset([])
    assert cases == []
    assert skipped == 0


# ---------- LLM-judge ----------


class _JudgeLLM:
    def __init__(self, content: str = "", exc: Exception | None = None):
        self.content = content
        self.exc = exc

    async def ainvoke(self, prompt, **kwargs):
        if self.exc:
            raise self.exc
        return AIMessage(content=self.content)


@pytest.mark.asyncio
async def test_judge_answer_parses_json():
    llm = _JudgeLLM(content='{"faithfulness": 5, "relevancy": 4}')
    verdict = await judge_answer("q", ["c1"], "answer", llm=llm)
    assert verdict == {"faithfulness": 5.0, "relevancy": 4.0}


@pytest.mark.asyncio
async def test_judge_answer_tolerates_markdown_wrap():
    llm = _JudgeLLM(content='```json\n{"faithfulness": 3, "relevancy": 2}\n```')
    verdict = await judge_answer("q", ["c1"], "answer", llm=llm)
    assert verdict["faithfulness"] == 3.0
    assert verdict["relevancy"] == 2.0


@pytest.mark.asyncio
async def test_judge_answer_failure_scores_zero():
    llm = _JudgeLLM(exc=RuntimeError("llm down"))
    verdict = await judge_answer("q", ["c1"], "answer", llm=llm)
    assert verdict["faithfulness"] == 0.0
    assert verdict["relevancy"] == 0.0
    assert verdict.get("error") == "judge_failed"


@pytest.mark.asyncio
async def test_judge_answer_clamps_out_of_range():
    llm = _JudgeLLM(content='{"faithfulness": 9, "relevancy": 0.5}')
    verdict = await judge_answer("q", ["c1"], "answer", llm=llm)
    assert verdict["faithfulness"] == 0.0  # 越界记 0
    assert verdict["relevancy"] == 0.0


@pytest.mark.asyncio
async def test_generate_answer():
    from app.knowledge.evaluator import generate_answer

    llm = _JudgeLLM(content="基于资料的回答")
    out = await generate_answer("q", ["c1"], llm=llm)
    assert out == "基于资料的回答"

    # 空上下文 → 空串（不调用 LLM）
    assert await generate_answer("q", [], llm=llm) == ""


def test_variant_configs_cover_defaults():
    assert evaluator.DEFAULT_VARIANTS == ["baseline", "hybrid", "hybrid+rerank"]
    assert evaluator.VARIANT_CONFIGS["baseline"] == {"strategy": "vector", "rerank": False}
    assert evaluator.VARIANT_CONFIGS["hybrid+rerank"] == {"strategy": "hybrid", "rerank": True}
