"""图状态定义：基于 MessagesState 扩展。"""

import operator
from typing import Annotated

from langgraph.graph import MessagesState


class HarnessState(MessagesState):
    """底座统一状态：消息 + 会话元信息 + 工具结果 + 控制字段。"""

    # messages 由 add_messages 自动追加（MessagesState 已内置）
    tenant_id: str = "default"
    user_id: str = "unknown"
    skill_loaded: Annotated[list[str], lambda a, b: list(dict.fromkeys(a + b))] = []
    tool_calls_total: int = 0
    interrupt_count: int = 0
    final_answer: str = ""
    memory_context: str = ""  # 对话前检索注入的用户长期记忆（MEMORY_SPEC §6.2）


class SupervisorState(HarnessState):
    """多 Agent 编排共享状态。"""

    route: str = "main"
    router_decision: dict = {}
    plan: list[dict] = []
    plan_summary: str = ""
    execution: str = "parallel"
    task: dict = {}
    agent_outputs: Annotated[list[dict], operator.add] = []
    verified_outputs: list[dict] = []
    aggregated: dict = {}
    clarification: dict = {}
    orchestration_error: str = ""
    session_id: str = ""
    message_id: int | None = None
    request_id: str = ""
