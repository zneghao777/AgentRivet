"""基于 AgentHub 能力卡的结构化 Router。"""

from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage, SystemMessage

from ..agents.base_agent import create_llm
from ..agents.models import OrchestrationAgent
from ..config import settings
from ..observability.metrics import metrics
from .agent_circuit_breaker import agent_circuit_breaker
from .models import RouterDecision

logger = logging.getLogger(__name__)

ROUTER_SYSTEM_PROMPT = """你是任务路由器。根据用户消息与候选 Agent 列表决定处理方式。

# 候选 Agent
{agent_cards}

# 规则
1. 只能路由到列出的 Agent；均不合适时 target_agent="main"。
2. 任务需要至少两个 Agent 的能力或可拆为独立子任务时 mode="orchestrate"。
3. confidence < {threshold} 时 needs_clarification=true，并提供问题和最多 3 个选项。
4. reason 必须引用候选 Agent description 中的具体能力。
5. 单 Agent 能完成的简单任务一律 direct。
6. 只输出符合 JSON Schema 的 JSON，不要输出 Markdown。

# JSON Schema
{schema}
"""


def available_agents(
    tenant_id: str, agents: dict[str, OrchestrationAgent]
) -> dict[str, OrchestrationAgent]:
    return {
        name: agent
        for name, agent in agents.items()
        if agent_circuit_breaker.available(tenant_id, name)
    }


def render_agent_cards(agents: dict[str, OrchestrationAgent]) -> str:
    cards = ["- main: 通用助手；未找到更合适的专用 Agent 时使用。"]
    cards.extend(
        f"- {item.name} [{item.scope}]: {item.description}\n  能力: {item.capability_summary}"
        for item in agents.values()
    )
    return "\n".join(cards)


async def decide_route(
    message: str,
    *,
    tenant_id: str,
    agents: dict[str, OrchestrationAgent],
    llm=None,
) -> RouterDecision:
    candidates = available_agents(tenant_id, agents)
    if not candidates:
        decision = RouterDecision(
            mode="direct",
            target_agent="main",
            confidence=1,
            reason="当前没有可用的专用 Agent，使用通用助手处理。",
        )
        metrics.record_orch_router_decision(tenant_id, decision.model_dump())
        return decision

    model = llm or create_llm(
        settings.SUPERVISOR_MODEL or settings.LLM_MODEL,
        temperature=0,
    )
    system = ROUTER_SYSTEM_PROMPT.format(
        agent_cards=render_agent_cards(candidates),
        threshold=settings.ROUTER_CONFIDENCE_THRESHOLD,
        schema=RouterDecision.model_json_schema(),
    )
    error_hint = ""
    for attempt in range(2):
        prompt = [
            SystemMessage(content=system + error_hint),
            HumanMessage(content=message),
        ]
        try:
            response = await model.ainvoke(prompt)
            decision = RouterDecision.model_validate_json(_content(response))
            if decision.target_agent != "main" and decision.target_agent not in candidates:
                raise ValueError("target_agent 不在候选列表")
            if decision.confidence < settings.ROUTER_CONFIDENCE_THRESHOLD:
                decision.needs_clarification = True
                decision.clarification_question = (
                    decision.clarification_question or "请补充希望完成的具体任务。"
                )
                if not decision.clarification_options:
                    decision.clarification_options = list(candidates)[:3]
            metrics.record_orch_router_decision(tenant_id, decision.model_dump())
            return decision
        except Exception as exc:  # noqa: BLE001
            logger.warning("Router 输出校验失败 attempt=%d: %s", attempt + 1, exc)
            error_hint = f"\n上一次输出无效：{str(exc)[:300]}。请仅返回合法 JSON。"

    fallback = RouterDecision(
        mode="direct",
        target_agent="main",
        confidence=0,
        reason="Router 输出连续校验失败，已安全回退通用助手。",
    )
    metrics.record_orch_router_decision(tenant_id, fallback.model_dump())
    return fallback


def _content(response) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content.strip()
    return str(content)
