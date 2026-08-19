"""图运行与工具注册测试（不依赖真实 LLM API）。"""

from __future__ import annotations

import asyncio

from app.harness.graph_builder import build_main_graph
from app.harness.graph_runtime import GraphRuntime
from app.tools.builtin.calculator import _safe_eval
from app.tools.registry import tool_registry


def test_builtin_tools_registered(clean_registry):
    """内置工具注册成功。"""
    tool_registry.register_builtin()
    names = [t.name for t in tool_registry.snapshot()]
    assert "builtin_echo" in names
    assert "builtin_get_time" in names
    assert "builtin_calculator" in names


def test_calculator_safe_eval():
    """计算器：正确计算且拦截任意代码。"""
    assert _safe_eval("(1+2)*3") == 9
    assert _safe_eval("10/4") == 2.5
    import pytest

    with pytest.raises(ValueError):
        _safe_eval('__import__("os").system("ls")')
    with pytest.raises(ValueError):
        _safe_eval("1 if True else 0")


def test_graph_compiles_with_tools(clean_registry):
    """主 Agent 图（含工具）可编译。"""
    tool_registry.register_builtin()
    g = build_main_graph(tool_registry, checkpointer=None)
    assert g is not None


def test_echo_tool_executes(clean_registry):
    """echo 工具功能正确。"""
    tool_registry.register_builtin()
    tool = tool_registry.get("builtin_echo")
    assert tool is not None
    result = tool.invoke({"text": "hello harness"})
    assert "hello harness" in str(result)


def test_get_time_tool_executes(clean_registry):
    """时间工具返回合法格式。"""
    tool_registry.register_builtin()
    tool = tool_registry.get("builtin_get_time")
    result = str(tool.invoke({}))
    assert "20" in result  # 年份或时间包含数字
    assert len(result) > 10


def test_sse_event_stream_shapes(clean_registry):
    """SSE 事件流包含关键事件类型（无 LLM key 时优雅降级）。"""

    async def run():
        tool_registry.register_builtin()
        g = build_main_graph(tool_registry, checkpointer=None)
        rt = GraphRuntime(g, None)
        frames: list[str] = []
        async for frame in rt.astream(
            "thr_sse", "hi", "tenant-a", "u1", [], session_id="sess"
        ):
            frames.append(frame)
        return "\n".join(frames)

    txt = asyncio.run(run())
    assert "event: session_start" in txt
    assert "event: message_start" in txt
    assert "event: message_end" in txt
    assert "event: run_end" in txt
