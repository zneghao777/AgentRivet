"""内置工具（演示用，验证 Harness 链路）。"""

from .calculator import builtin_calculator
from .echo import builtin_echo
from .get_time import builtin_get_time

__all__ = ["builtin_calculator", "builtin_echo", "builtin_get_time"]
