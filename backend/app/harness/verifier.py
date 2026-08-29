"""Worker 结果校验与一次重派。"""

from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, SystemMessage

from ..agents.base_agent import create_llm
from ..config import settings
from .worker import execute_task

REFUSAL_MARKERS = ("CANNOT_HANDLE:", "无法处理", "不能处理", "抱歉，我无法")


def verify_result(output: dict) -> tuple[bool, str]:
    if output.get("status") != "done":
        return False, output.get("error") or "worker_failed"
    result = str(output.get("result") or "").strip()
    if not result:
        return False, "result_empty"
    if len(result) < 20:
        return False, "result_too_short"
    if any(result.startswith(marker) for marker in REFUSAL_MARKERS):
        return False, "result_is_refusal"
    return True, "ok"


async def verify_outputs(state: dict, base_tools: list) -> list[dict]:
    tasks = {item["task_id"]: item for item in state.get("plan", [])}
    verified: list[dict] = []
    for output in state.get("agent_outputs", []):
        passed, reason = verify_result(output)
        if passed and settings.VERIFIER_LLM_ENABLED:
            passed, reason = await _llm_critic(output)
        if not passed and output.get("error") != "agent_declined":
            task = tasks.get(output.get("task_id"))
            if task:
                retried = await execute_task(
                    state,
                    task,
                    base_tools,
                    verification_reason=reason,
                )
                passed, retry_reason = verify_result(retried)
                if passed and settings.VERIFIER_LLM_ENABLED:
                    passed, retry_reason = await _llm_critic(retried)
                if not passed:
                    retried = {
                        **retried,
                        "status": "failed",
                        "result": None,
                        "error": f"verification_failed:{retry_reason}",
                    }
                output = retried
        verified.append(output)
    return verified


async def _llm_critic(output: dict) -> tuple[bool, str]:
    llm = create_llm(settings.SUPERVISOR_MODEL or settings.LLM_MODEL)
    response = await llm.ainvoke(
        [
            SystemMessage(
                content=(
                    "判断结果是否回答了目标。只输出 JSON："
                    '{"pass": true|false, "reason": "..."}'
                )
            ),
            HumanMessage(
                content=f"目标：{output.get('goal', '')}\n结果：{output.get('result', '')}"
            ),
        ]
    )
    try:
        data = json.loads(str(getattr(response, "content", response)))
        return bool(data.get("pass")), str(data.get("reason") or "critic_rejected")
    except Exception:  # noqa: BLE001
        return False, "critic_invalid_output"
