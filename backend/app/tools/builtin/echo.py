"""内置工具：builtin_echo —— 原样返回输入，验证工具链路。"""

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field


class EchoArgs(BaseModel):
    text: str = Field(description="要回显的文本")


def _echo(text: str) -> str:
    return text


builtin_echo = StructuredTool.from_function(
    func=_echo,
    name="builtin_echo",
    description="原样返回输入文本。用于验证工具调用链路是否通畅。当用户要求回显或测试工具时使用。",
    args_schema=EchoArgs,
)
