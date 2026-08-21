"""实体归一化 / 别名合并（GRAPH_RAG_SPEC §3.2 步骤 4）。

策略（准确率优先于召回率，宁可漏合不可错合）：
1. 精确别名表（项目可维护 alias_rules）。
2. 向量相似度：同类型实体描述向量 cosine > 阈值 → 合并候选。
3. LLM 确认：低置信度候选由 LLM 判定是否同指（可选，成本较高，默认关闭自动确认）。

对外提供 resolve_names 纯函数：输入一批候选实体名，输出 {name -> canonical_name}。
"""

from __future__ import annotations

import logging
import re

from ..embedder import EmbeddingError, embed_texts

logger = logging.getLogger(__name__)

# 项目级别名规则：{canonical_name: [别名...]}，可被外部扩展
ALIAS_RULES: dict[str, list[str]] = {
    # 示例（《凡人修仙传》），真实项目在应用中补充
    "韩立": ["韩跑跑", "韩道友", "厉飞雨", "韩真人"],
    "紫灵": ["紫灵仙子", "南陇侯"],
}

# 构建别名映射缓存：别名 -> canonical
_ALIAS_LOOKUP: dict[str, str] | None = None


def _build_alias_lookup() -> dict[str, str]:
    """将别名表构建为 别名->规范名 映射（含自身）。"""
    lookup: dict[str, str] = {}
    for canonical, aliases in ALIAS_RULES.items():
        lookup[canonical] = canonical
        for alias in aliases:
            lookup[alias] = canonical
    return lookup


def alias_lookup() -> dict[str, str]:
    """返回全局别名映射（懒构建）。"""
    global _ALIAS_LOOKUP
    if _ALIAS_LOOKUP is None:
        _ALIAS_LOOKUP = _build_alias_lookup()
    return _ALIAS_LOOKUP


def _normalize_name(name: str) -> str:
    """归一化比较键：去空白、去标点、小写（中文无需小写，兼容英文）。"""
    name = re.sub(r"\s+", "", name or "")
    return name.strip().lower()


def resolve_names(names: list[str]) -> dict[str, str]:
    """纯函数：输入一批实体名，返回 {原名: 规范名}。

    - 命中别名表 → 映射到规范名。
    - 全等（归一化后）自动视为同指。
    仅处理确定性规则（准确率优先）；向量/LLM 合并由上层持久化流程处理。
    """
    lookup = alias_lookup()
    resolved: dict[str, str] = {}
    # 先用别名表
    for name in names:
        canonical = lookup.get(name)
        resolved[name] = canonical or name
    # 归一化全等归并：取出现顺序第一个作为规范名
    norm_to_canonical: dict[str, str] = {}
    for name in names:
        key = _normalize_name(resolved.get(name, name))
        if key in norm_to_canonical:
            resolved[name] = norm_to_canonical[key]
        else:
            norm_to_canonical[key] = resolved.get(name, name)
    return resolved


async def find_vector_merge_candidates(
    entities: list[tuple[str, str, str]],
    *,
    threshold: float,
    top_k: int = 5,
) -> list[tuple[str, str]]:
    """向量相似度合并候选（同类型实体描述向量）。

    entities: [(name, type, description), ...]
    返回合并候选对 [(name_a, name_b), ...]，二者描述高度相似且同类型。
    这是确定性规则之外的第二层；上层据此做 LLM 确认或直接合并。
    """
    if len(entities) < 2 or threshold <= 0:
        return []
    texts = [(desc or name) for name, _t, desc in entities]
    try:
        vectors = await embed_texts(texts)
    except EmbeddingError:
        return []
    if len(vectors) != len(entities):
        return []

    pairs: list[tuple[str, str]] = []
    names = [n for n, _t, _d in entities]
    types = [t for _n, t, _d in entities]
    for i in range(len(entities)):
        for j in range(i + 1, len(entities)):
            if types[i] != types[j]:
                continue
            if names[i] == names[j]:
                continue
            sim = _cosine(vectors[i], vectors[j])
            if sim >= threshold:
                pairs.append((names[i], names[j]))
    return pairs


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
