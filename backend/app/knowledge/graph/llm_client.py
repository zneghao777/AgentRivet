"""图谱抽取用的 LLM 客户端（OpenAI 兼容 chat，默认走智谱 GLM 低成本模型）。

GRAPH_RAG_SPEC §1.2 / §7：
- 抽取不占用主 Agent 对话流，使用独立配置。
- 默认 GRAPH_EXTRACT_MODEL=glm-4-flash、GRAPH_EXTRACT_BASE_URL=智谱 v4。
- 未配置 GRAPH_EXTRACT_API_KEY 时回退主 LLM（LLM_BASE_URL / LLM_API_KEY），
  保证零配置也能跑通（代价是模型成本更高）。
"""

from __future__ import annotations

import json
import logging
import re

import httpx

from ...config import settings

logger = logging.getLogger(__name__)


class ExtractLLMError(Exception):
    """抽取 LLM 调用异常（网络 / 响应格式）。"""


def _chat_params() -> tuple[str, str, str]:
    """返回 (base_url, api_key, model) 的最终取值。"""
    if settings.GRAPH_EXTRACT_API_KEY:
        return (
            settings.GRAPH_EXTRACT_BASE_URL.rstrip("/"),
            settings.GRAPH_EXTRACT_API_KEY,
            settings.GRAPH_EXTRACT_MODEL,
        )
    # 回退主 LLM（DeepSeek / 本地服务等）
    return (
        settings.LLM_BASE_URL.rstrip("/"),
        settings.LLM_API_KEY,
        settings.LLM_MODEL,
    )


async def chat_completion(
    messages: list[dict],
    *,
    temperature: float = 0.2,
    max_tokens: int = 2048,
    json_mode: bool = False,
) -> str:
    """调用 OpenAI 兼容 chat/completions，返回 assistant content 字符串。

    json_mode=True 时在请求体加 response_format={"type":"json_object"}（GLM 支持）。
    """
    base_url, api_key, model = _chat_params()
    if not api_key:
        raise ExtractLLMError("未配置任何 LLM API Key（GRAPH_EXTRACT_API_KEY 或 LLM_API_KEY）")
    return await _chat_completion_inner(
        base_url, api_key, model, messages,
        temperature=temperature, max_tokens=max_tokens, json_mode=json_mode,
    )


async def _chat_completion_inner(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    *,
    temperature: float,
    max_tokens: int,
    json_mode: bool,
) -> str:
    """chat_completion 内部实现（拆分便于按批覆盖 max_tokens）。"""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    # 仅在 json_mode 时附加 response_format；部分服务（DeepSeek）不兼容时报错
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    content = await _post_chat(base_url, api_key, payload, headers, json_mode=json_mode)
    if isinstance(content, list):
        return "".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part)
            for part in content
        ).strip()
    return str(content or "").strip()


async def _post_chat(
    base_url: str, api_key: str, payload: dict, headers: dict, *, json_mode: bool
) -> str:
    """POST chat/completions；json_mode 失败时去掉 response_format 重试一次。"""
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{base_url}/chat/completions", json=payload, headers=headers
            )
    except httpx.HTTPError as exc:
        raise ExtractLLMError(f"抽取 LLM 网络错误: {exc}") from exc
    # json_mode 报错（如服务不支持 response_format）→ 去掉后重试
    if resp.status_code != 200 and json_mode:
        retry_payload = dict(payload)
        retry_payload.pop("response_format", None)
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions", json=retry_payload, headers=headers
                )
        except httpx.HTTPError as exc:
            raise ExtractLLMError(f"抽取 LLM 网络错误: {exc}") from exc
    if resp.status_code != 200:
        raise ExtractLLMError(
            f"抽取 LLM HTTP {resp.status_code}: {resp.text[:300]}"
        )
    try:
        body = resp.json()
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ExtractLLMError("抽取 LLM 响应格式异常") from exc
    return content


def parse_json_object(text: str) -> dict:
    """从 LLM 输出中提取 JSON 对象（容忍 markdown 代码块 / 前后杂讯 / 截断）。

    渐进式解析：
    1. 从最右的 } 逐字符向左回退，尝试完整解析。
    2. 全失败时对截断输出做"实体数组抢救"（抽出已完成解析的实体对象）。
    """
    if not text or not text.strip():
        raise ExtractLLMError("抽取 LLM 输出为空")
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start = cleaned.find("{")
    if start < 0:
        raise ExtractLLMError("抽取 LLM 输出中未找到 JSON 对象")
    # 1. 从最右的 } 逐字符向左回退，尝试解析（容忍截断 / 多余尾部）
    for end in range(len(cleaned) - 1, start, -1):
        if cleaned[end] != "}":
            continue
        fragment = cleaned[start : end + 1]
        try:
            obj = json.loads(fragment)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    # 2. 截断抢救：尝试从 "entities":[ 提取完整实体对象
    salvaged = _salvage_entities(cleaned)
    if salvaged is not None:
        return salvaged
    raise ExtractLLMError("抽取 LLM JSON 解析失败（无法找到闭合 JSON 对象）")


def _salvage_entities(text: str) -> dict | None:
    """从截断的 JSON 中抢救 entities 数组（容忍数组被截断）。

    在 "entities": [ 后扫描对象，收集能完整 json.loads 的对象。
    """
    marker = text.find('"entities"')
    if marker < 0:
        return None
    bracket = text.find("[", marker)
    if bracket < 0:
        return None
    entities: list[dict] = []
    i = bracket + 1
    while i < len(text):
        if text[i] == "{":
            # 用 raw_decode 尝试解析一个完整对象
            try:
                obj, next_i = json.JSONDecoder().raw_decode(text, i)
                if isinstance(obj, dict):
                    entities.append(obj)
                    i = next_i
                    continue
            except json.JSONDecodeError:
                pass
            # 对象不完整：尝试找这个对象自己的闭合 }
            depth = 1
            j = i + 1
            while j < len(text) and depth > 0:
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                j += 1
            if depth == 0:
                try:
                    obj = json.loads(text[i:j])
                    if isinstance(obj, dict):
                        entities.append(obj)
                except json.JSONDecodeError:
                    pass
            # 无论成败都前进，避免死循环
            i = max(j, i + 1)
        else:
            i += 1
    if entities:
        return {"entities": entities, "relations": [], "events": []}
    return None
