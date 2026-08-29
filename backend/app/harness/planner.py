"""多 Agent TaskCard Planner。"""

from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage, SystemMessage

from ..agents.base_agent import create_llm
from ..agents.models import OrchestrationAgent
from ..config import settings
from .models import Plan
from .router import render_agent_cards

logger = logging.getLogger(__name__)

PLANNER_SYSTEM_PROMPT = """你是任务规划器。将用户请求分解为至多 {max_tasks} 个并行或顺序子任务。

# 候选 Agent
{agent_cards}

# 硬性要求
1. context_brief 必须自包含，原样保留数字、日期、名称和文件名。
2. 子任务不得有隐式依赖；存在依赖时 execution="sequential"。
3. goal 用动词开头；constraints 明确格式、字数和禁止事项。
4. target_agent 只能使用候选 Agent 名称，不能使用 main。
5. 只输出符合 JSON Schema 的 JSON，不要输出 Markdown。

# JSON Schema
{schema}
"""


async def create_plan(
    message: str,
    *,
    agents: dict[str, OrchestrationAgent],
    llm=None,
) -> Plan | None:
    if not agents:
        return None
    model = llm or create_llm(
        settings.SUPERVISOR_MODEL or settings.LLM_MODEL,
        temperature=0,
    )
    system = PLANNER_SYSTEM_PROMPT.format(
        max_tasks=min(5, settings.PLAN_MAX_TASKS),
        agent_cards=render_agent_cards(agents),
        schema=Plan.model_json_schema(),
    )
    error_hint = ""
    for attempt in range(2):
        try:
            response = await model.ainvoke(
                [SystemMessage(content=system + error_hint), HumanMessage(content=message)]
            )
            plan = Plan.model_validate_json(_content(response))
            if len(plan.tasks) > settings.PLAN_MAX_TASKS:
                raise ValueError("任务数量超过 PLAN_MAX_TASKS")
            if (
                plan.execution == "parallel"
                and len(plan.tasks) > settings.MAX_PARALLEL_WORKERS
            ):
                raise ValueError("并行任务数量超过 MAX_PARALLEL_WORKERS")
            unknown = {task.target_agent for task in plan.tasks} - set(agents)
            if unknown:
                raise ValueError(f"存在未知 Agent: {', '.join(sorted(unknown))}")
            return plan
        except Exception as exc:  # noqa: BLE001
            logger.warning("Planner 输出校验失败 attempt=%d: %s", attempt + 1, exc)
            error_hint = f"\n上一次输出无效：{str(exc)[:300]}。请仅返回合法 JSON。"
    return None


def _content(response) -> str:
    content = getattr(response, "content", response)
    return content.strip() if isinstance(content, str) else str(content)
