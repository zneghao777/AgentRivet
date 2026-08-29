"""LangGraph 自定义编排事件出口。"""

from __future__ import annotations

from datetime import UTC, datetime

from langchain_core.callbacks.manager import adispatch_custom_event


async def emit(name: str, payload: dict) -> None:
    """在图运行中发送事件；单元测试直接调用节点时允许无回调上下文。"""
    data = {**payload, "ts": datetime.now(UTC).isoformat()}
    try:
        await adispatch_custom_event(name, data)
    except RuntimeError:
        return
