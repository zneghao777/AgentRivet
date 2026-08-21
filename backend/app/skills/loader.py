"""SKILL.md 解析与加载器。

遵循 Anthropic SKILL.md 开放规范，解析 frontmatter + 正文。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from .models import SkillModel

logger = logging.getLogger(__name__)


SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class SkillValidationError(ValueError):
    """Skill manifest 不符合约定。"""


def _parse_frontmatter(raw: str) -> tuple[dict, str]:
    """使用完整 YAML 解析 frontmatter，兼容多行描述和嵌套 metadata。"""
    meta: dict = {}
    body = raw
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            body = parts[2].strip()
            try:
                loaded = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError as exc:
                raise SkillValidationError(f"frontmatter YAML 无法解析：{exc}") from exc
            if not isinstance(loaded, dict):
                raise SkillValidationError("frontmatter 必须是 YAML 对象")
            meta = loaded
    return meta, body


def load_skill_from_dir(path: Path, *, strict: bool = False) -> SkillModel | None:
    """加载单个 Skill 目录；导入场景可用 strict 获取明确校验错误。"""
    skill_md = path / "SKILL.md"
    if not skill_md.exists():
        error = SkillValidationError("目录中缺少 SKILL.md")
        if strict:
            raise error
        logger.warning("Skill 目录 %s 缺少 SKILL.md，跳过", path)
        return None

    try:
        raw = skill_md.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(raw)
        name = str(meta.get("name") or path.name).strip()
        description = str(meta.get("description") or "").strip()
        if not SKILL_NAME_RE.fullmatch(name):
            raise SkillValidationError(
                "name 仅支持字母、数字、点、下划线和连字符，且长度不超过 128"
            )
        if not description:
            raise SkillValidationError("frontmatter 缺少 description")
        tags_raw = meta.get("tags", [])
        if isinstance(tags_raw, str):
            tags = [item.strip() for item in tags_raw.split(",") if item.strip()]
        elif isinstance(tags_raw, list):
            tags = [str(item).strip() for item in tags_raw if str(item).strip()]
        else:
            raise SkillValidationError("tags 必须是字符串数组")
    except (UnicodeDecodeError, SkillValidationError) as exc:
        if strict:
            raise SkillValidationError(str(exc)) from exc
        logger.warning("Skill 目录 %s 校验失败：%s", path, exc)
        return None

    files = [item for item in path.rglob("*") if item.is_file()]

    return SkillModel(
        name=name,
        path=str(path),
        description=description,
        body_md=body,
        version=str(meta.get("version", "0.0.0")),
        author=str(meta.get("author", "")),
        tags=tags,
        enabled=str(meta.get("enabled", "true")).lower() == "true",
        file_count=len(files),
        has_scripts=any(item.parent.name == "scripts" and item.suffix == ".py" for item in files),
    )


def discover_skill_dirs(*roots: Path) -> list[Path]:
    """递归发现全部含 SKILL.md 的 Skill 目录。"""
    found: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        found.extend(skill_md.parent for skill_md in root.rglob("SKILL.md"))
    return sorted(set(found))
