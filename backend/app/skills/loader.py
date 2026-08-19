"""SKILL.md 解析与加载器。

遵循 Anthropic SKILL.md 开放规范，解析 frontmatter + 正文。
"""

from __future__ import annotations

import logging
from pathlib import Path

from .models import SkillModel

logger = logging.getLogger(__name__)


def _parse_frontmatter(raw: str) -> tuple[dict, str]:
    """解析 YAML frontmatter（简易实现，不引入 pyyaml 依赖负担）。"""
    import json

    meta: dict = {}
    body = raw
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            fm = parts[1]
            body = parts[2].strip()
            for line in fm.splitlines():
                if not line.strip() or ":" not in line:
                    continue
                key, _, value = line.partition(":")
                key = key.strip()
                value = value.strip()
                meta[key] = _parse_scalar(value)
    return meta, body


def _parse_scalar(value: str):
    """解析 frontmatter 标量值：支持列表 / 布尔 / 数字 / 字符串。"""
    import json

    if value.startswith("[") and value.endswith("]"):
        # YAML 列表（元素可能无引号），转成 JSON 再解析
        inner = value[1:-1].strip()
        if not inner:
            return []
        items = [item.strip().strip("'\"") for item in inner.split(",") if item.strip()]
        return items
    if value.startswith("{") and value.endswith("}"):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    if value == "true":
        return True
    if value == "false":
        return False
    try:
        return int(value)
    except ValueError:
        pass
    return value


def load_skill_from_dir(path: Path) -> SkillModel | None:
    """加载单个 Skill 目录。返回 None 表示无效 frontmatter。"""
    skill_md = path / "SKILL.md"
    if not skill_md.exists():
        logger.warning("Skill 目录 %s 缺少 SKILL.md，跳过", path)
        return None

    raw = skill_md.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(raw)

    name = meta.get("name", path.name)
    description = meta.get("description", "")
    if not description:
        logger.warning("Skill %s 缺少 description，跳过", name)
        return None

    return SkillModel(
        name=name,
        path=str(path),
        description=description,
        body_md=body,
        version=meta.get("version", "0.0.0"),
        author=meta.get("author", ""),
        tags=meta.get("tags", []),
        enabled=str(meta.get("enabled", "true")).lower() == "true",
    )


def discover_skill_dirs(*roots: Path) -> list[Path]:
    """发现全部含 SKILL.md 的 Skill 目录（一层递归扫描）。"""
    found: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        if (root / "SKILL.md").exists():
            found.append(root)
        for child in root.iterdir():
            if child.is_dir() and (child / "SKILL.md").exists():
                found.append(child)
    return found
