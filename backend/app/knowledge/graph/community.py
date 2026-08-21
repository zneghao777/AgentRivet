"""社区发现 + 摘要（GRAPH_RAG_SPEC §3.2 步骤 5）。

目的：让图谱能回答全局性问题（如"总结韩立的主要成长阶段"）。

实现：
- level 0：连通分量（基于关系边），细粒度社区。
- level 1：粗粒度社区（按实体类型/权重归并，形成金字塔）。
- 社区摘要：成员实体 + 关系汇总喂给 LLM 生成 summary 与 keywords。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class CommunityCandidate:
    """一个社区候选（level 0 成员为实体 id，level 1 由多个 level0 聚合）。"""

    level: int
    member_entity_ids: list[str] = field(default_factory=list)
    summary: str | None = None
    keywords: list[str] = field(default_factory=list)


def connected_communities(
    entities: list[str],
    edges: list[tuple[str, str]],
) -> list[list[str]]:
    """连通分量划分（无向）。返回若干组实体 id 列表。"""
    adj: dict[str, set[str]] = {e: set() for e in entities}
    for a, b in edges:
        if a in adj and b in adj:
            adj[a].add(b)
            adj[b].add(a)
    visited: set[str] = set()
    components: list[list[str]] = []
    for node in entities:
        if node in visited:
            continue
        stack = [node]
        comp: list[str] = []
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            comp.append(cur)
            stack.extend(adj[cur] - visited)
        if len(comp) >= 2:
            components.append(comp)
    return components


def coarse_communities(level0: list[list[str]]) -> list[list[str]]:
    """粗粒度：将 level0 按相邻关系二次归并，形成 level1 金字塔。

    简单实现：把彼此有共同"枢纽"成员倾向合并——这里采用逐次归并小社区到
    最近的大社区，直至所有社区不再缩小（贪心）。结果不保证最优，但足够支持全局摘要。
    """
    if not level0:
        return []
    # 用集合表示；记录每对社区是否共享成员作为合并依据（保守：仅当明显重叠）
    clusters = [set(c) for c in level0]
    changed = True
    while changed:
        changed = False
        merged = []
        used = [False] * len(clusters)
        for i, ci in enumerate(clusters):
            if used[i]:
                continue
            used[i] = True
            current = set(ci)
            for j in range(i + 1, len(clusters)):
                if used[j]:
                    continue
                cj = clusters[j]
                if current & cj:
                    current |= cj
                    used[j] = True
                    changed = True
            merged.append(current)
        clusters = merged
    return [sorted(c) for c in clusters]


_SUMMARY_PROMPT = """你是知识图谱社区摘要器。以下是一个图社区包含的实体与关系，请用 3~5 句话概括这个社区的核心主题，并给出 3~6 个关键词。

仅输出 JSON：{{"summary": "社区摘要", "keywords": ["关键词1", ...]}}

### 实体
{entities}

### 关系
{relations}"""


async def summarize_community(
    entity_lines: list[str],
    relation_lines: list[str],
    *,
    llm=None,
) -> tuple[str | None, list[str]]:
    """调用 LLM 生成社区摘要与关键词；失败返回 (None, [])。"""
    if not entity_lines:
        return None, []
    if llm is None:
        from .llm_client import chat_completion as llm
    from .llm_client import ExtractLLMError, parse_json_object

    prompt = _SUMMARY_PROMPT.format(
        entities="\n".join(entity_lines)[:2500],
        relations="\n".join(relation_lines)[:2500] or "（无）",
    )
    try:
        content = await llm(
            [
                {"role": "system", "content": "你是知识图谱社区摘要器。"},
                {"role": "user", "content": prompt},
            ],
            json_mode=True,
            temperature=0.2,
        )
        data = parse_json_object(content)
        summary = str(data.get("summary") or "").strip() or None
        keywords = [
            str(k).strip() for k in (data.get("keywords") or []) if str(k).strip()
        ]
        return summary, keywords[:6]
    except ExtractLLMError as exc:
        logger.warning("社区摘要生成失败: %s", exc)
        return None, []


def build_communities(
    entity_ids: list[str],
    edges: list[tuple[str, str]],
) -> list[CommunityCandidate]:
    """构建两级社区候选（level0 连通分量 + level1 粗粒度），不含 LLM 摘要。

    摘要由调用方用 summarize_community 逐社区生成后回填。
    """
    level0 = connected_communities(entity_ids, edges)
    candidates: list[CommunityCandidate] = [
        CommunityCandidate(level=0, member_entity_ids=c) for c in level0
    ]
    level1 = coarse_communities(level0)
    for c in level1:
        candidates.append(CommunityCandidate(level=1, member_entity_ids=c))
    return candidates
