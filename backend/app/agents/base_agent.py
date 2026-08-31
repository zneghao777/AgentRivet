"""基础 Agent：LLM 工厂 + Prompt 构建 + ReAct 节点逻辑。

LLM 创建工厂放在此，供主 Agent 与子 Agent 复用。
"""

from __future__ import annotations

import time
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI

from ..config import settings
from ..harness.context import get_tool_context
from ..harness.llm_guard import (
    CallChannel,
    GuardedChatModel,
    GuardedEndpoint,
    endpoint_circuit_breaker,
    get_llm_pool,
    thinking_override,
)
from ..skills.manager import skill_manager

SYSTEM_PROMPT_TEMPLATE = """你是企业级 Agent Harness 底座中的通用助手，代号「Harness Agent」。

# 能力边界
- 你本身不绑定任何业务。业务能力由已加载的 Skill 与可用工具提供。
- 严格遵守系统提示中列出的 Skill 指令；未加载的 Skill 一律不得臆想其功能。
- 当需要外部信息或执行操作时，调用对应工具；不要编造工具输出。
- 当已加载 Skill 引用 references/ 等相对资源文件时，调用 builtin_read_skill_file，
  传入该 Skill 的名称和相对路径；不要声称自己无法读取 Skill 资源。

# 已加载的 Skill（description）
{skill_descriptions}

# Skill 详细指令（首次触发时注入）
{skill_bodies}

# 长期记忆（MEMORY_SPEC §6.2 自动注入）
{memory_context}
"""


def create_llm(
    model_id: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    temperature: float | None = None,
    *,
    call_channel: CallChannel | None = None,
    max_tokens: int | None = None,
) -> BaseChatModel:
    """创建 OpenAI 兼容 LLM，并统一接入并发与端点防护。"""
    request_context = get_tool_context()
    if api_key is not None:
        resolved_api_key = api_key
    elif request_context.model_api_key is not None:
        resolved_api_key = request_context.model_api_key
    else:
        resolved_api_key = settings.LLM_API_KEY
    resolved_model = model_id or request_context.model_id or settings.LLM_MODEL
    resolved_base_url = base_url or request_context.model_base_url or settings.LLM_BASE_URL
    resolved_temperature = settings.LLM_TEMPERATURE if temperature is None else temperature
    resolved_channel = call_channel or request_context.call_channel
    degrade_level = request_context.resolved_degrade_level
    resolved_max_tokens = settings.LLM_MAX_TOKENS if max_tokens is None else max(1, max_tokens)
    if degrade_level >= 1:
        resolved_max_tokens = max(1, resolved_max_tokens // 2)

    call_timeout = float(settings.LLM_CALL_TIMEOUT_SECONDS)
    if (
        resolved_channel == "online"
        and request_context.outcome_deadline_monotonic is not None
    ):
        call_timeout = min(
            call_timeout,
            max(0.05, request_context.outcome_deadline_monotonic - time.monotonic()),
        )

    provider_kwargs: dict[str, Any] = {
        "base_url": resolved_base_url,
        # OpenAI SDK 要求凭据非空；本地无鉴权服务使用无敏感性的占位值。
        "api_key": resolved_api_key or "not-needed",
        "temperature": resolved_temperature,
        "max_tokens": resolved_max_tokens,
        # 429 必须由 Guard 逐次观测并执行 AIMD；SDK 内部重试会隐藏中间 429。
        "max_retries": 0,
        "timeout": call_timeout,
        # invoke() 仍通过 provider stream 聚合为完整 AIMessage，使 LangGraph
        # 能产生 on_chat_model_stream 并记录真实 first-token TTFT。
        "streaming": True,
    }
    if request_context.user_id:
        provider_kwargs["model_kwargs"] = {"user": request_context.user_id}
    extra_body = thinking_override(resolved_base_url, resolved_model, degrade_level)
    if extra_body:
        provider_kwargs["extra_body"] = extra_body

    inner = ChatOpenAI(model=resolved_model, **provider_kwargs)
    if not settings.GUARD_ENABLED:
        return inner

    fallback_endpoints = tuple(
        GuardedEndpoint(
            model=ChatOpenAI(model=fallback_model, **provider_kwargs),
            base_url=resolved_base_url,
            model_name=fallback_model,
        )
        for fallback_model in _fallback_models(resolved_model)
    )
    return GuardedChatModel(
        inner=inner,
        pool=get_llm_pool(resolved_channel),
        base_url=resolved_base_url,
        model_name=resolved_model,
        request_id=request_context.request_id,
        outcome_deadline_monotonic=(
            request_context.outcome_deadline_monotonic
            if resolved_channel == "online"
            else None
        ),
        fallback_endpoints=fallback_endpoints,
        circuit_breaker=endpoint_circuit_breaker,
    )


def _fallback_models(primary_model: str) -> list[str]:
    """解析有序 fallback 链，并去除空值、重复项与主模型。"""
    seen = {primary_model}
    models: list[str] = []
    for item in settings.LLM_FALLBACK_MODELS.split(","):
        model = item.strip()
        if not model or model in seen:
            continue
        seen.add(model)
        models.append(model)
    return models


def build_system_prompt(
    skill_names: list[str],
    memory_context: str | None = None,
    agent_system_prompt: str | None = None,
) -> str:
    """根据已加载的 Skill 构建 system prompt。

    渐进式：description 始终注入顶部；正文在会话首次触发时注入。
    memory_context：对话前检索到的用户长期记忆（MEMORY_SPEC §6.2），注入在最底部。
    """
    descriptions: list[str] = []
    bodies: list[str] = []

    for name in skill_names:
        skill = skill_manager.get(name)
        if not skill:
            continue
        if skill.enabled:
            descriptions.append(f"- {skill.name}: {skill.description}")
            # 正文按需注入：内置示例 skill 直接注入其正文
            if skill.body_md:
                bodies.append(f"### Skill: {skill.name}\n{skill.body_md}")

    memory_block = memory_context.strip() if memory_context else "（无已检索到的长期记忆）"
    default_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        skill_descriptions="\n".join(descriptions) or "（无已加载 Skill）",
        skill_bodies="\n\n".join(bodies) or "（无）",
        memory_context=memory_block,
    )
    if not agent_system_prompt:
        return default_prompt
    return (
        "# Agent 身份与任务边界\n"
        f"{agent_system_prompt.strip()}\n\n"
        "--- 默认 Harness 能力与安全边界（始终生效） ---\n"
        f"{default_prompt}"
    )


def agent_node(state: dict) -> dict:
    """主 Agent 节点：调用 LLM（bind_tools）生成回复或工具调用。"""
    request_context = get_tool_context()
    llm = state.get("_llm") or create_llm(
        request_context.model_id,
        request_context.model_base_url,
        request_context.model_api_key,
    )
    messages = _messages_for_degrade(
        state.get("messages", []),
        request_context.resolved_degrade_level,
    )
    skill_names = state.get("skill_loaded", [])
    memory_context = state.get("memory_context")
    system_prompt = build_system_prompt(
        skill_names,
        memory_context,
        request_context.agent_system_prompt,
    )

    tools = state.get("_tools", [])
    llm_with_tools = llm.bind_tools(tools) if tools else llm

    prompt = [
        SystemMessage(content=system_prompt),
        *_with_attachments(messages, request_context.attachments or []),
    ]
    response: AIMessage = llm_with_tools.invoke(prompt)
    return {"messages": [response]}


_D2_RECENT_MESSAGE_BUDGET = 6


def _messages_for_degrade(messages: list[Any], degrade_level: int) -> list[Any]:
    """Retain D2 history without splitting tool exchanges or losing attachments."""
    if degrade_level < 2:
        return messages
    system_prefix: list[Any] = []
    for message in messages:
        if not isinstance(message, SystemMessage):
            break
        system_prefix.append(message)
    remaining = messages[len(system_prefix) :]
    groups = _history_message_groups(remaining)
    if not groups:
        return system_prefix

    selected: set[int] = set()
    retained_messages = 0
    for index in range(len(groups) - 1, -1, -1):
        if retained_messages >= _D2_RECENT_MESSAGE_BUDGET:
            break
        selected.add(index)
        retained_messages += len(groups[index])

    # Attachments are injected after trimming. Keep the latest user message even
    # when a long tool loop would otherwise move it outside the recent window.
    latest_user = next(
        (message for message in reversed(remaining) if isinstance(message, HumanMessage)),
        None,
    )
    if latest_user is not None:
        for index, group in enumerate(groups):
            if any(message is latest_user for message in group):
                selected.add(index)
                break

    trimmed = [message for index in sorted(selected) for message in groups[index]]
    return [*system_prefix, *trimmed]


def _history_message_groups(messages: list[Any]) -> list[list[Any]]:
    """Group an assistant tool request with its contiguous matching results.

    A standalone ``ToolMessage`` cannot be sent back to an OpenAI-compatible
    provider, so malformed source-history orphans are omitted during D2 trimming.
    """

    groups: list[list[Any]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if isinstance(message, ToolMessage):
            index += 1
            continue

        group = [message]
        index += 1
        if isinstance(message, AIMessage) and message.tool_calls:
            tool_call_ids = {
                str(call.get("id")) for call in message.tool_calls if call.get("id") is not None
            }
            while index < len(messages) and isinstance(messages[index], ToolMessage):
                tool_message = messages[index]
                if not tool_call_ids or str(tool_message.tool_call_id) in tool_call_ids:
                    group.append(tool_message)
                index += 1
        groups.append(group)
    return groups


def _with_attachments(messages: list, attachments: list[dict]) -> list:
    """把请求态附件注入最后一条用户消息，不污染会话 checkpoint。"""
    if not attachments:
        return messages
    last_user_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if isinstance(messages[index], HumanMessage)
        ),
        None,
    )
    if last_user_index is None:
        return messages

    original = messages[last_user_index]
    text = original.content if isinstance(original.content, str) else str(original.content)
    document_blocks = [
        f"\n\n--- 附件：{item['name']} ---\n{item['text']}\n--- 附件结束 ---"
        for item in attachments
        if item.get("kind") == "document"
    ]
    text_content = text + "".join(document_blocks)
    images = [item for item in attachments if item.get("kind") == "image"]
    content: str | list[dict]
    if images:
        content = [{"type": "text", "text": text_content}]
        content.extend(
            {
                "type": "image_url",
                "image_url": {"url": item["data_url"]},
            }
            for item in images
        )
    else:
        content = text_content

    enriched = HumanMessage(
        content=content,
        additional_kwargs=original.additional_kwargs,
        name=original.name,
        id=original.id,
    )
    result = list(messages)
    result[last_user_index] = enriched
    return result
