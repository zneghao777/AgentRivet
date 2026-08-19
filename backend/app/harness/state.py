"""图状态定义：基于 MessagesState 扩展。"""

from typing import Annotated

from langgraph.graph import MessagesState
from langgraph.graph.message import add_messages


class HarnessState(MessagesState):
    """底座统一状态：消息 + 会话元信息 + 工具结果 + 控制字段。"""

    # messages 由 add_messages 自动追加（MessagesState 已内置）
    tenant_id: str = "default"
    user_id: str = "unknown"
    skill_loaded: Annotated[list[str], lambda a, b: list(dict.fromkeys(a + b))] = []
    tool_calls_total: int = 0
    interrupt_count: int = 0
    final_answer: str = ""
