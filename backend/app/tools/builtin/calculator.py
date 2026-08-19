"""内置工具：builtin_calculator —— 安全表达式求值。

🔒 安全项：使用 ast 白名单解析，仅允许四则运算，禁止 eval 任意代码。
"""

import ast
import operator

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

_ALLOWED_BINOPS: dict[type, object] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_ALLOWED_UNARYOPS: dict[type, object] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_ALLOWED_CONSTS = (int, float)


class CalculatorArgs(BaseModel):
    expression: str = Field(description="四则运算表达式，如 (1+2)*3/4")


def _safe_eval(expr: str) -> float | int:
    """基于 ast 的安全求值，仅允许数字与四则/取模/幂运算。"""
    tree = ast.parse(expr, mode="eval")

    def eval_node(node: ast.AST) -> float | int:
        if isinstance(node, ast.Constant) and isinstance(node.value, _ALLOWED_CONSTS):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
            op = _ALLOWED_BINOPS[type(node.op)]
            return op(eval_node(node.left), eval_node(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARYOPS:
            op = _ALLOWED_UNARYOPS[type(node.op)]
            return op(eval_node(node.operand))
        raise ValueError(f"不支持的表达式元素: {type(node).__name__}")

    return eval_node(tree.body)


def _calculator(expression: str) -> str:
    try:
        result = _safe_eval(expression)
        return f"{expression} = {result}"
    except Exception as exc:  # noqa: BLE001
        return f"计算失败: {exc}"


builtin_calculator = StructuredTool.from_function(
    func=_calculator,
    name="builtin_calculator",
    description="对四则运算表达式进行安全求值（仅支持数字和 + - * / % **）。当用户要求计算数学表达式时使用。",
    args_schema=CalculatorArgs,
)
