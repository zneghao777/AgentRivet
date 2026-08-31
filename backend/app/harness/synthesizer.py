"""多分支结果的降级聚合与最终综合。"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from ..agents.base_agent import create_llm
from ..config import settings
from .llm_guard import LLMGuardError

SYNTHESIZER_PROMPT = """你是结果综合器。请把多个 Agent 的子任务结果综合为最终回答。

要求：
1. 忠实于分支结果，不得编造。
2. 结论冲突时显式标注各 Agent 观点，不得擅自裁决。
3. 存在失败分支时说明缺失部分及原因，并基于成功部分回答。
4. 关键结论标注来源 Agent。
5. 最终回答必须回应原始会话任务和当前用户请求，不得只罗列分支结果。
"""


def aggregate_outputs(outputs: list[dict]) -> dict:
    ordered = sorted(outputs, key=lambda item: item.get("task_id", ""))
    return {
        "results": [item for item in ordered if item.get("status") == "done"],
        "failures": [item for item in ordered if item.get("status") != "done"],
    }


async def synthesize(
    aggregated: dict,
    *,
    request_context: str = "",
) -> tuple[str, str | None]:
    results = aggregated.get("results", [])
    failures = aggregated.get("failures", [])
    if not results:
        reasons = "；".join(
            f"{item.get('target_agent', 'Agent')}：{item.get('error', '执行失败')}"
            for item in failures
        )
        answer = (
            "本次编排的所有子任务均未成功完成。失败原因："
            f"{reasons or '未知错误'}。请稍后重试或缩小任务范围。"
        )
        return answer, "all_workers_failed"
    if len(results) == 1 and not failures:
        return str(results[0].get("result") or ""), None
    branches = []
    for item in results:
        branches.append(
            f"[成功][{item.get('target_agent')}] 目标：{item.get('goal')}\n"
            f"结果：{item.get('result')}"
        )
    for item in failures:
        branches.append(
            f"[失败][{item.get('target_agent')}] 目标：{item.get('goal')}\n"
            f"原因：{item.get('error')}"
        )
    try:
        llm = create_llm(settings.SUPERVISOR_MODEL or settings.LLM_MODEL)
        response = await llm.ainvoke(
            [
                SystemMessage(content=SYNTHESIZER_PROMPT),
                HumanMessage(
                    content=(
                        f"# 原始会话任务\n{request_context.strip() or '（无）'}\n\n"
                        "# Agent 分支结果\n"
                        + "\n\n".join(branches)
                    )
                ),
            ]
        )
        content = getattr(response, "content", response)
        answer = content.strip() if isinstance(content, str) else str(content)
        if answer:
            return answer, "partial_failure" if failures else None
    except LLMGuardError:
        raise
    except Exception:  # noqa: BLE001
        pass
    answer = "\n\n".join(
        f"### {item.get('target_agent')}\n{item.get('result')}" for item in results
    )
    if failures:
        answer += "\n\n未完成部分：" + "；".join(
            f"{item.get('target_agent')}（{item.get('error')}）" for item in failures
        )
    return answer, "partial_failure" if failures else None
