"""实体 / 关系 / 事件抽取（接 LLM，GRAPH_RAG_SPEC §3.2 步骤 1~3）。

两阶段策略（成本与幻觉控制）：
1. 候选抽取：LLM 从合并后的 chunk 批量抽出 (实体, 关系, 事件) JSON。
2. 规则去噪：丢弃无信息量名词（"天地""道法" 等），并对实体名做清洗。

默认使用低成本抽取模型（glm-4-flash），不占用主 Agent 对话流。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .llm_client import (
    ExtractLLMError,
    parse_json_object,
)

logger = logging.getLogger(__name__)

# 低信息量名词黑名单（去除噪音，GRAPH_RAG_SPEC §3.2 步骤 1）
LOW_INFO_NOUNS = {
    "天地", "道法", "天地法则", "天道", "灵气", "功法", "境界", "时间",
    "空间", "世界", "大陆", "地方", "人", "东西", "事情", "情况", "方式",
    "方法", "原因", "结果", "问题", "时候", "所谓", "一般", "一个", "整个",
}

# 类型白名单提示词
ENTITY_TYPE_HINT = (
    "person(人物) | place(地点) | organization(组织/门派/宗门) | "
    "item(物品/法宝/功法) | cultivation(修行境界) | event(事件) | concept(概念)"
)

# 中文常用关系类型提示
RELATION_TYPE_HINT = (
    "属于 | 位于 | 修行 | 拥有 | 杀害 | 师徒 | 联盟 | 敌人 | 发现 | 来自 | "
    "拜师于 | 获得 | 结为道侣 | 离开 | 加入 | 创建 | 传授 | 击杀 | 寻找 | 留下 | 突破至"
)

_EXTRACT_PROMPT = """你是信息抽取引擎。请从【文本】中抽取实体、关系与事件，仅输出 JSON，不要输出任何解释。

### 实体类型
{entity_types}

### 关系类型（可自由扩展，保持简洁，3~8 字）
{relation_types}

### 输出 JSON 格式
{{
  "entities": [
    {{"name": "实体名", "type": "person", "description": "一句话描述（≤50字）"}}
  ],
  "relations": [
    {{"source": "起点实体名", "relation": "关系", "target": "终点实体名", "evidence": "原文证据句（≤40字）"}}
  ],
  "events": [
    {{"subject": "主体实体名", "predicate": "动作", "object": "对象", "summary": "一句话事件摘要（≤30字）"}}
  ]
}}

### 要求
1. 只抽取文本中明确出现、有信息量的实体，宁缺毋滥。
2. 丢弃泛化名词（如"天地""境界""功法"本身）作为独立实体。
3. 关系必须两端都指向已抽取的实体。
4. 同一实体的不同叫法（如"韩跑跑"与"韩立"）拆为不同 name，relation 里用最规范名。
5. entities 最多 {max_entities} 个。

### 文本
{text}"""

# 低置信度名词规则过滤（正则兜底，防 LLM 幻觉泛化名词）
_GENERIC_PATTERN = re.compile(r"^(?:天地|道法|天道|灵气|功法|境界|世间|人间|修真界|天地法则)$")


@dataclass
class ExtractedEntity:
    """抽取出的实体候选。"""

    name: str
    entity_type: str
    description: str | None = None


@dataclass
class ExtractedRelation:
    """抽取出的关系候选。"""

    source: str
    relation: str
    target: str
    evidence: str | None = None


@dataclass
class ExtractedEvent:
    """抽取出的事件候选。"""

    subject: str
    predicate: str
    object: str | None = None
    summary: str | None = None


@dataclass
class ExtractionResult:
    """一批 chunk 的抽取结果。"""

    entities: list[ExtractedEntity] = field(default_factory=list)
    relations: list[ExtractedRelation] = field(default_factory=list)
    events: list[ExtractedEvent] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.entities and not self.relations and not self.events


def _clean_name(name: str) -> str:
    """清洗实体名：去空白、去引号。"""
    return re.sub(r"\s+", "", (name or "")).strip(" \t\"'“”")


def _is_low_info(name: str) -> bool:
    """判定是否低信息量名词（丢弃）。"""
    name = _clean_name(name)
    if not name or len(name) > 20:
        return True
    if name in LOW_INFO_NOUNS:
        return True
    return bool(_GENERIC_PATTERN.match(name))


def _valid_relation_type(relation: str) -> str:
    """关系类型清洗：去空白，限长。"""
    rel = re.sub(r"\s+", "", (relation or ""))
    if len(rel) > 12:
        rel = rel[:12]
    return rel


def _build_prompt(chunk_texts: list[str], max_entities: int) -> str:
    joined = "\n\n---\n\n".join(chunk_texts)
    return _EXTRACT_PROMPT.format(
        entity_types=ENTITY_TYPE_HINT,
        relation_types=RELATION_TYPE_HINT,
        max_entities=max_entities,
        text=joined[:2400],  # 限制输入长度，防止模型输出被截断
    )


def _parse_result(data: dict) -> ExtractionResult:
    """将 LLM JSON 解析为结构化抽取结果（逐项容错）。"""
    result = ExtractionResult()
    entities: list[ExtractedEntity] = []

    for raw in data.get("entities") or []:
        if not isinstance(raw, dict):
            continue
        name = _clean_name(str(raw.get("name") or ""))
        etype = str(raw.get("type") or "concept").strip()
        desc = str(raw.get("description") or "").strip() or None
        if not name or _is_low_info(name):
            continue
        entities.append(ExtractedEntity(name=name, entity_type=etype, description=desc))

    # 实体名全集（关系/事件必须指向实体，防悬空）
    name_set = {e.name for e in entities}

    for raw in data.get("relations") or []:
        if not isinstance(raw, dict):
            continue
        source = _clean_name(str(raw.get("source") or ""))
        target = _clean_name(str(raw.get("target") or ""))
        rel = _valid_relation_type(str(raw.get("relation") or ""))
        evidence = str(raw.get("evidence") or "").strip() or None
        if not source or not target or not rel:
            continue
        if source not in name_set or target not in name_set:
            continue
        result.relations.append(
            ExtractedRelation(source=source, relation=rel, target=target, evidence=evidence)
        )

    for raw in data.get("events") or []:
        if not isinstance(raw, dict):
            continue
        subject = _clean_name(str(raw.get("subject") or ""))
        predicate = _valid_relation_type(str(raw.get("predicate") or ""))
        obj = _clean_name(str(raw.get("object") or "")) or None
        summary = str(raw.get("summary") or "").strip() or None
        if not subject or not predicate:
            continue
        if subject not in name_set:
            continue
        result.events.append(
            ExtractedEvent(subject=subject, predicate=predicate, object=obj, summary=summary)
        )

    result.entities = entities
    return result


async def extract_batch(
    chunk_texts: list[str],
    *,
    max_entities: int = 30,
    llm=None,
    max_retries: int = 2,
) -> ExtractionResult:
    """对一批合并 chunk 做实体/关系/事件抽取。

    对"解析失败 / 空结果"做有限次重试（LLM 抽取偶发空响应），
    仍失败返回空 ExtractionResult（不向上抛，避免构建中断）。
    """
    if not chunk_texts:
        return ExtractionResult()
    if llm is None:
        from .llm_client import chat_completion as llm

    prompt = _build_prompt(chunk_texts, max_entities)
    messages = [
        {"role": "system", "content": "你是信息抽取引擎，严格按用户要求输出 JSON。"},
        {"role": "user", "content": prompt},
    ]
    attempts = 1 + max(0, max_retries)
    for attempt in range(attempts):
        if attempt == 0:
            msgs = messages
            temp = 0.1
            max_tok = 4096
        else:
            # 重试：简化要求 + 提高随机性 + 更大输出预算
            msgs = [
                {
                    "role": "system",
                    "content": "你是信息抽取引擎。只输出 JSON，格式："
                    '{"entities":[{"name","type","description"}],'
                    '"relations":[{"source","relation","target","evidence"}],'
                    '"events":[{"subject","predicate","object","summary"}]}。'
                    "请保证 entities 至少返回 1 个明确实体。",
                },
                {
                    "role": "user",
                    "content": "请从以下文本抽取实体、关系与事件，尽量返回非空：\n"
                    + prompt[prompt.find("### 文本") :],
                },
            ]
            temp = 0.4
            max_tok = 4096
        try:
            content = await llm(
                msgs, json_mode=True, temperature=temp, max_tokens=max_tok
            )
        except ExtractLLMError as exc:
            logger.warning("抽取 LLM 调用失败（第%d次）: %s", attempt + 1, exc)
            continue
        try:
            data = parse_json_object(content)
        except ExtractLLMError as exc:
            logger.warning("抽取响应解析失败（第%d次）: %s", attempt + 1, exc)
            continue
        result = _parse_result(data)
        if not result.is_empty():
            return result
        logger.info("抽取结果为空（第%d次，重试）", attempt + 1)
    return ExtractionResult()


async def extract_entity_only(
    text: str,
    *,
    max_entities: int = 30,
    llm=None,
) -> list[ExtractedEntity]:
    """仅抽取实体（骨架模式 / 社区生成等场景复用）。"""
    result = await extract_batch([text], max_entities=max_entities, llm=llm)
    return result.entities
