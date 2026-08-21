"""基础 Agent：LLM 工厂 + Prompt 构建 + ReAct 节点逻辑。

LLM 创建工厂放在此，供主 Agent 与子 Agent 复用。
"""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI

from ..config import settings
from ..harness.context import get_tool_context
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


def create_llm(model_id: str | None = None) -> BaseChatModel:
    """创建 OpenAI 兼容 LLM。"""
    return ChatOpenAI(
        model=model_id or settings.LLM_MODEL,
        base_url=settings.LLM_BASE_URL,
        api_key=settings.LLM_API_KEY,
        temperature=settings.LLM_TEMPERATURE,
        max_tokens=settings.LLM_MAX_TOKENS,
    )


def build_system_prompt(
    skill_names: list[str], memory_context: str | None = None
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
            descriptions.append(
                f"- {skill.name}: {skill.description}"
            )
            # 正文按需注入：内置示例 skill 直接注入其正文
            if skill.body_md:
                bodies.append(f"### Skill: {skill.name}\n{skill.body_md}")

    memory_block = memory_context.strip() if memory_context else "（无已检索到的长期记忆）"
    return SYSTEM_PROMPT_TEMPLATE.format(
        skill_descriptions="\n".join(descriptions)
        or "（无已加载 Skill）",
        skill_bodies="\n\n".join(bodies) or "（无）",
        memory_context=memory_block,
    )


def agent_node(state: dict) -> dict:
    """主 Agent 节点：调用 LLM（bind_tools）生成回复或工具调用。"""
    request_context = get_tool_context()
    llm = state.get("_llm") or create_llm(request_context.model_id)
    messages = state.get("messages", [])
    skill_names = state.get("skill_loaded", [])
    memory_context = state.get("memory_context")
    system_prompt = build_system_prompt(skill_names, memory_context)

    tools = state.get("_tools", [])
    llm_with_tools = llm.bind_tools(tools) if tools else llm

    from langchain_core.messages import SystemMessage

    prompt = [
        SystemMessage(content=system_prompt),
        *_with_attachments(messages, request_context.attachments or []),
    ]
    response: AIMessage = llm_with_tools.invoke(prompt)
    return {"messages": [response]}


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
