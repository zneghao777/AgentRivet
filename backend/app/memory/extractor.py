"""记忆提炼器：LLM 从对话中提炼值得长期记住的事实/偏好/事件（MEMORY_SPEC §6.1）。

输出 JSON 列表：[{content, memory_type, confidence, source_summary?}]
- confidence < MEMORY_CONFIDENCE_PENDING → 进入 pending（不参与检索，可人工确认）
- 提取失败 → 记录日志 + 指标，本轮不产生记忆，不影响对话主链路
"""

from __future__ import annotations

import json
import logging
import re

from ..config import settings

logger = logging.getLogger(__name__)

# 提取 prompt：引导 LLM 只提炼"跨会话稳定"的用户事实/偏好/事件，避免噪声
_EXTRACT_PROMPT = """你是一个记忆提炼器。从下面这段用户与 AI 助手的对话中，提炼出【值得长期记住】的
用户事实、偏好或重要事件。

规则：
1. 只提炼跨会话稳定、对后续对话有复用价值的信息（如用户身份、职业、技术栈、偏好、决策、解决过的问题）。
2. 不要提炼：一次性闲聊、临时状态、与用户无关的信息。
3. memory_type：
   - "semantic"：稳定的用户事实或偏好，如"用户的生产数据库是 Postgres 16"、"用户偏好简洁回复"。
   - "episodic"：带时间点的已解决事项/重要事件，如"用户上周解决了 XX 工单，方案是 YY"。
4. confidence（0-1）：你对该条记忆准确性的把握。把握低（可能幻觉）给 0.6 以下。
5. 每条记忆用一句话陈述，不超过 80 字。
6. 输出为 JSON 数组，不要输出其他文字。

对话：
{conversation}

输出（JSON 数组，元素字段：content, memory_type, confidence, source_summary）：
"""


def _build_conversation(user_msg: str, assistant_msg: str, session_title: str | None) -> str:
    """拼接待提炼的对话上下文（截断防止超长）。"""
    title = session_title or "无标题会话"
    lines = [
        f"会话标题：{title}",
        f"用户：{user_msg[:2000]}",
        f"助手：{assistant_msg[:2000]}",
    ]
    return "\n".join(lines)


def _parse_response(raw: str) -> list[dict]:
    """解析 LLM 输出为记忆候选列表（容忍 markdown 代码块包裹）。"""
    text = raw.strip()
    # 剥离可能的 ```json ... ``` 包裹
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    # 定位 JSON 数组
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("LLM 未返回 JSON 数组")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, list):
        raise ValueError("LLM 返回值不是数组")
    return data


def _normalize_candidates(raw_items: list[dict]) -> list[dict]:
    """清洗候选：过滤非法字段，落到 (content, memory_type, confidence, source_summary)。"""
    out: list[dict] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        mtype = item.get("memory_type")
        if mtype not in ("semantic", "episodic"):
            mtype = "semantic"
        try:
            confidence = float(item.get("confidence", 1.0))
        except (TypeError, ValueError):
            confidence = 1.0
        confidence = max(0.0, min(1.0, confidence))
        summary = str(item.get("source_summary", "")).strip() or None
        out.append(
            {
                "content": content,
                "memory_type": mtype,
                "confidence": confidence,
                "source_summary": summary,
            }
        )
    return out


async def extract(
    user_msg: str,
    assistant_msg: str,
    session_title: str | None = None,
) -> list[dict]:
    """从一轮对话中提炼记忆候选（LLM 调用）。

    返回清洗后的候选列表；任何异常返回 []（不影响对话主链路）。
    """
    if not user_msg and not assistant_msg:
        return []
    conversation = _build_conversation(user_msg, assistant_msg, session_title)
    prompt = _EXTRACT_PROMPT.format(conversation=conversation)

    try:
        from langchain_openai import ChatOpenAI

        llm = ChatOpenAI(
            model=settings.LLM_MODEL,
            base_url=settings.LLM_BASE_URL,
            api_key=settings.LLM_API_KEY,
            temperature=0.2,
            max_tokens=settings.LLM_MAX_TOKENS,
        )
        resp = await llm.ainvoke(prompt)
        raw = resp.content if isinstance(resp.content, str) else ""
        items = _parse_response(raw)
        return _normalize_candidates(items)[: settings.MEMORY_EXTRACT_MAX_ITEMS]
    except Exception as exc:  # noqa: BLE001
        logger.warning("记忆提炼失败: %s", exc)
        return []
