"""RAG 评测：HitRate / MRR 纯函数计算 + dataset 解析 + LLM-as-judge。

AGENTIC_RAG_UPGRADE_SPEC §1.4 / §3.9：
- 检索评测指标：hit_rate@k、mrr@k，纯 Python 计算，无外部依赖。
- parse_dataset：校验必填字段，非法用例跳过并计数。
- LLM-judge：judge_answer(query, contexts, answer) → {faithfulness, relevancy}（1-5），
  调用 create_llm()，结构化输出（JSON），失败该用例记 0 并标注。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 评测 variant → 检索配置映射（§3.7）
VARIANT_CONFIGS: dict[str, dict] = {
    "baseline": {"strategy": "vector", "rerank": False},
    "hybrid": {"strategy": "hybrid", "rerank": False},
    "hybrid+rerank": {"strategy": "hybrid", "rerank": True},
    "vector": {"strategy": "vector", "rerank": False},
    "keyword": {"strategy": "keyword", "rerank": False},
}

DEFAULT_VARIANTS: list[str] = ["baseline", "hybrid", "hybrid+rerank"]


@dataclass
class EvalCase:
    """单个评测用例（expected 为命中目标集合，字符串化以兼容 int/str）。"""

    query: str
    expected_chunk_ids: set[str] = field(default_factory=set)
    expected_doc_ids: set[str] = field(default_factory=set)
    reference_answer: str | None = None


def parse_dataset(dataset: list[dict]) -> tuple[list[EvalCase], int]:
    """校验必填字段（query + expected_chunk_ids/expected_doc_ids 至少一个）。

    非法用例跳过并计数；返回 (cases, skipped_count)。
    """
    cases: list[EvalCase] = []
    skipped = 0
    for raw in dataset:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        query = str(raw.get("query") or "").strip()
        chunk_ids = {str(c) for c in (raw.get("expected_chunk_ids") or []) if str(c).strip()}
        doc_ids = {str(d) for d in (raw.get("expected_doc_ids") or []) if str(d).strip()}
        if not query or (not chunk_ids and not doc_ids):
            skipped += 1
            continue
        reference = raw.get("reference_answer")
        cases.append(
            EvalCase(
                query=query,
                expected_chunk_ids=chunk_ids,
                expected_doc_ids=doc_ids,
                reference_answer=str(reference).strip() if reference else None,
            )
        )
    return cases, skipped


def hit_rate(results: list[list[str]], expected: list[set[str]]) -> float:
    """hit_rate@k = |{i : results[i] ∩ expected[i] ≠ ∅}| / n。

    results[i] 为用例 i 的 top-k 命中 chunk_id 列表；expected[i] 为命中目标集合。
    """
    if not results:
        return 0.0
    hits = sum(
        1
        for got, want in zip(results, expected, strict=False)
        if want and set(got) & want
    )
    return hits / len(results)


def mrr(results: list[list[str]], expected: list[set[str]]) -> float:
    """mrr@k = mean(1 / first_hit_rank_i)；无命中记 0。"""
    if not results:
        return 0.0
    total = 0.0
    for got, want in zip(results, expected, strict=False):
        first_rank = 0
        for rank, chunk_id in enumerate(got, start=1):
            if want and chunk_id in want:
                first_rank = rank
                break
        if first_rank:
            total += 1.0 / first_rank
    return total / len(results)


# ---------- LLM-as-judge（可选，端到端评测） ----------

_ANSWER_PROMPT = (
    "根据以下资料回答问题。若资料不足以回答，请直接说明。\n\n"
    "【问题】{query}\n\n【资料】\n{contexts}"
)

_JUDGE_PROMPT = """你是 RAG 评测评委。根据以下信息为回答打两个 1-5 分的分数：
- faithfulness：回答是否严格基于给定资料、无编造（资料充分支撑=5，明显编造=1）
- relevancy：回答是否切题并完整回应了问题（完全切题=5，答非所问=1）

【问题】{query}

【资料】
{contexts}

【回答】
{answer}

只输出 JSON，格式：{{"faithfulness": <1-5 的数字>, "relevancy": <1-5 的数字>}}"""


def _content_str(response) -> str:
    """LLM 响应 content 转 str（兼容 str 与多模态 list 内容）。"""
    content = getattr(response, "content", "")
    if isinstance(content, list):
        return "".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part)
            for part in content
        ).strip()
    return str(content or "").strip()


def _extract_json(text: str) -> dict:
    """从 LLM 输出中提取首个 JSON 对象（容忍 markdown 代码块包裹）。"""
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("输出中未找到 JSON 对象")
    return json.loads(cleaned[start : end + 1])


def _clamp_score(value) -> float:
    """分数钳制到 [1, 5]；非法值记 0（该用例标注失败）。"""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if 1.0 <= score <= 5.0:
        return round(score, 2)
    return 0.0


async def generate_answer(query: str, contexts: list[str], llm=None) -> str:
    """基于检索上下文生成参考回答（judge 无 reference_answer 时使用）。失败返回空串。"""
    if not contexts:
        return ""
    try:
        if llm is None:
            from ..agents.base_agent import create_llm

            llm = create_llm(call_channel="background")
        prompt = _ANSWER_PROMPT.format(
            query=query, contexts="\n---\n".join(contexts)[:4000]
        )
        response = await llm.ainvoke(prompt)
        return _content_str(response)
    except Exception as exc:  # noqa: BLE001
        logger.warning("评测参考回答生成失败: %s", exc)
        return ""


async def judge_answer(
    query: str, contexts: list[str], answer: str, llm=None
) -> dict:
    """LLM-as-judge：返回 {faithfulness, relevancy}（1-5 分）。

    失败该用例记 0 并标注 error 字段（AGENTIC_RAG_UPGRADE_SPEC §3.9）。
    """
    try:
        if llm is None:
            from ..agents.base_agent import create_llm

            llm = create_llm(call_channel="background")
        prompt = _JUDGE_PROMPT.format(
            query=query,
            contexts="\n---\n".join(contexts)[:4000] or "（无）",
            answer=(answer or "").strip() or "（无）",
        )
        response = await llm.ainvoke(prompt)
        data = _extract_json(_content_str(response))
        return {
            "faithfulness": _clamp_score(data.get("faithfulness")),
            "relevancy": _clamp_score(data.get("relevancy")),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM-judge 失败，该用例记 0: %s", exc)
        return {"faithfulness": 0.0, "relevancy": 0.0, "error": "judge_failed"}
