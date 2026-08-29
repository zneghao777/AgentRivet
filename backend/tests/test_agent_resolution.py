"""AgentHub 提示词叠加与工具收敛单测。"""

from langchain_core.tools import StructuredTool

from app.agents.base_agent import build_system_prompt
from app.harness.context import ToolContext, reset_tool_context, set_tool_context
from app.harness.graph_builder import resolve_request_tools


def _tool(name: str) -> StructuredTool:
    return StructuredTool.from_function(
        func=lambda: name,
        name=name,
        description=f"{name} test tool",
    )


def test_agent_identity_is_prepended_without_replacing_harness_boundary():
    prompt = build_system_prompt([], "", "你是一个严谨的分析助手。")
    assert prompt.startswith("# Agent 身份与任务边界")
    assert "你是一个严谨的分析助手" in prompt
    assert "默认 Harness 能力与安全边界（始终生效）" in prompt
    assert "未加载的 Skill 一律不得臆想其功能" in prompt


def test_request_tools_respect_agent_skill_builtin_and_memory_boundaries():
    tools = [
        _tool("builtin_echo"),
        _tool("builtin_calculator"),
        _tool("memory_search"),
        _tool("skill_alpha__run"),
        _tool("skill_beta__run"),
    ]
    set_tool_context(
        ToolContext(
            allowed_skill_names=["alpha"],
            allowed_tool_names={"builtin_echo", "memory_search"},
            memory_enabled=False,
        )
    )
    try:
        assert [tool.name for tool in resolve_request_tools(tools)] == [
            "builtin_echo",
            "skill_alpha__run",
        ]
    finally:
        reset_tool_context()
