"""内置工具：builtin_get_time —— 返回当前时间。"""

from datetime import datetime, timedelta, timezone

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

# 简易 IANA 时区偏移映射（Asia/Shanghai 等常见时区）
_TZ_OFFSETS: dict[str, int] = {
    "UTC": 0,
    "Asia/Shanghai": 8,
    "Asia/Tokyo": 9,
    "Asia/Singapore": 8,
    "Europe/London": 0,
    "America/New_York": -5,
    "America/Los_Angeles": -8,
}


class GetTimeArgs(BaseModel):
    timezone: str = Field(
        default="Asia/Shanghai",
        description="时区，如 Asia/Shanghai、UTC。",
    )


def _now_with_offset(offset_hours: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=offset_hours)


def _get_time(timezone: str = "Asia/Shanghai") -> str:
    offset_hours = _TZ_OFFSETS.get(timezone, 8)
    dt = _now_with_offset(offset_hours)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


builtin_get_time = StructuredTool.from_function(
    func=_get_time,
    name="builtin_get_time",
    description="返回当前时间。当用户询问现在几点、当前时间、日期时使用。可指定时区（默认 Asia/Shanghai）。",
    args_schema=GetTimeArgs,
)
