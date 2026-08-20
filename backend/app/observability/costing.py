"""LLM 成本计算（OBSERVABILITY_SPEC §6.4）。

成本必须按 `model_price_versions` 中带生效时间的价格版本计算，不能把供应商
当前价格回填到历史调用。未配置价格时回退到内置默认价目表并打标记。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# 内置默认价目表（美元 / 每百万 token）。仅作为未配置价格时的兜底。
DEFAULT_PRICES: dict[str, dict[str, float]] = {
    "claude-sonnet-4": {"input": 3.0, "cached_input": 0.3, "output": 15.0},
    "claude-opus-4": {"input": 15.0, "cached_input": 1.5, "output": 75.0},
    "claude-haiku-3": {"input": 0.25, "cached_input": 0.03, "output": 1.25},
    "gpt-4o": {"input": 2.5, "cached_input": 1.25, "output": 10.0},
    "gpt-4o-mini": {"input": 0.15, "cached_input": 0.075, "output": 0.6},
    "deepseek-v3": {"input": 0.27, "cached_input": 0.07, "output": 1.10},
    "deepseek-r1": {"input": 0.55, "cached_input": 0.14, "output": 2.19},
    "glm-4-plus": {"input": 0.5, "cached_input": 0.05, "output": 0.5},
    "default": {"input": 3.0, "cached_input": 0.3, "output": 15.0},
}

DEFAULT_CURRENCY = "USD"


def _price_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def compute_cost(
    model_name: str,
    token_usage: dict | None,
    *,
    input_price_per_million: float | None = None,
    cached_input_price_per_million: float | None = None,
    output_price_per_million: float | None = None,
    currency: str | None = None,
) -> dict[str, Any]:
    """按价格计算一次 LLM 调用的成本（§6.4）。

    返回 dict：{cost_input, cost_output, cost_total, currency, price_source}
    price_source: "configured"（价格表）/ "default"（内置兜底）/ None（无 token）
    """
    usage = token_usage or {}
    input_tokens = usage.get("input", usage.get("prompt", 0)) or 0
    cached_input = usage.get("cached_input", 0) or 0
    output_tokens = usage.get("output", usage.get("completion", 0)) or 0
    billable_input = max(0, input_tokens - cached_input)

    base = DEFAULT_PRICES.get(model_name) or DEFAULT_PRICES["default"]
    in_price = input_price_per_million if input_price_per_million is not None else base["input"]
    cached_price = (
        cached_input_price_per_million
        if cached_input_price_per_million is not None
        else base["cached_input"]
    )
    out_price = output_price_per_million if output_price_per_million is not None else base["output"]

    input_cost = billable_input * in_price / 1_000_000
    cached_cost = cached_input * cached_price / 1_000_000
    output_cost = output_tokens * out_price / 1_000_000
    total = input_cost + cached_cost + output_cost

    source = "configured" if input_price_per_million is not None else ("default" if input_tokens or output_tokens else None)
    return {
        "cost_input": round(input_cost, 8),
        "cost_output": round(output_cost, 8),
        "cost_total": round(total, 8),
        "currency": currency or DEFAULT_CURRENCY,
        "price_source": source,
    }


async def resolve_price(
    db,
    model_name: str,
    *,
    at: datetime | None = None,
) -> dict | None:
    """从 model_price_versions 查询 model_name 在指定时间生效的价格。"""
    from ..storage import repositories as repo

    at = at or datetime.now(UTC)
    try:
        version = await repo.get_price_at(db, model_name, at=at)
    except Exception:  # noqa: BLE001
        logger.warning("查询价格失败 model=%s: 回退默认价目表", model_name, exc_info=True)
        return None
    if version is None:
        return None
    return {
        "model_provider": version.model_provider,
        "model_name": version.model_name,
        "input_price_per_million": _price_float(version.input_price_per_million),
        "cached_input_price_per_million": _price_float(version.cached_input_price_per_million),
        "output_price_per_million": _price_float(version.output_price_per_million),
        "currency": version.currency or DEFAULT_CURRENCY,
        "version": version.version,
        "price_version_id": version.id,
    }
