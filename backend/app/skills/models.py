"""Skill 数据模型：frontmatter 元数据 + 正文。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class SkillModel:
    """内存中的 Skill 模型（对应 DB 的 skills 表）。"""

    name: str
    path: str
    description: str
    body_md: str = ""
    version: str = "0.0.0"
    author: str = ""
    tags: list[str] = field(default_factory=list)
    enabled: bool = True
    loaded_at: datetime | None = None
    updated_at: datetime | None = None
