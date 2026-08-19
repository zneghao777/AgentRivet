"""Skill 生命周期管理：发现 / 加载 / 启停 / 重载 / 卸载。

加载时扫描 scripts/* 注册为工具（skill_{name}__*），并同步 DB 状态。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from ..storage import repositories as repo
from ..storage.models import Skill as SkillORM
from ..tools.registry import tool_registry
from .loader import discover_skill_dirs, load_skill_from_dir
from .models import SkillModel

logger = logging.getLogger(__name__)

# 内置 + 外部 Skill 仓库根目录
BUILTIN_SKILLS_DIR = Path(__file__).resolve().parent / "builtin_skills"
EXTERNAL_SKILLS_DIR = Path(__file__).resolve().parents[3] / "skills"  # 仓库根/skills


class SkillManager:
    """Skill 生命周期管理单例。"""

    def __init__(self) -> None:
        self._skills: dict[str, SkillModel] = {}

    def get(self, name: str) -> SkillModel | None:
        return self._skills.get(name)

    def all(self) -> list[SkillModel]:
        return list(self._skills.values())

    async def discover(self, db: AsyncSession) -> list[SkillModel]:
        """全量扫描（内置 + 外部），解析 frontmatter，更新内存与 DB。"""
        roots = [BUILTIN_SKILLS_DIR, EXTERNAL_SKILLS_DIR]
        dirs = discover_skill_dirs(*roots)
        found: dict[str, SkillModel] = {}
        for d in dirs:
            skill = load_skill_from_dir(d)
            if skill is None:
                continue
            found[skill.name] = skill

        self._skills = found
        # 同步 DB
        for skill in found.values():
            await self._upsert_db(db, skill)
        return self.all()

    async def _upsert_db(self, db: AsyncSession, skill: SkillModel) -> None:
        orm = SkillORM(
            name=skill.name,
            path=skill.path,
            description=skill.description,
            version=skill.version,
            author=skill.author,
            tags=skill.tags,
            enabled=skill.enabled,
            loaded_at=datetime.now(),
        )
        await repo.upsert_skill(db, orm)
        # 同步 enabled 状态回内存
        skill.enabled = orm.enabled

    async def load(self, db: AsyncSession, name: str) -> SkillModel | None:
        """加载（启用）一个 Skill：注册 scripts 工具，更新 DB enabled。"""
        skill = self._skills.get(name)
        if not skill:
            logger.warning("尝试加载不存在的 Skill: %s", name)
            return None
        self._register_script_tools(skill)
        skill.enabled = True
        skill.loaded_at = datetime.now()
        await repo.set_skill_enabled(db, name, True)
        return skill

    async def disable(self, db: AsyncSession, name: str) -> SkillModel | None:
        """停用一个 Skill：卸载 scripts 工具，更新 DB enabled。"""
        skill = self._skills.get(name)
        if not skill:
            return None
        self._unregister_script_tools(skill)
        skill.enabled = False
        await repo.set_skill_enabled(db, name, False)
        return skill

    async def reload(self, db: AsyncSession) -> list[SkillModel]:
        """重新扫描并加载（开发热更新）。"""
        current = self._skills
        await self.discover(db)
        for name in list(current.keys()):
            if name in self._skills:
                self._skills[name].enabled = current[name].enabled
        return self.all()

    async def remove(self, db: AsyncSession, name: str) -> None:
        """卸载：删除 DB 记录与注册信息（不删磁盘文件）。"""
        skill = self._skills.pop(name, None)
        if skill:
            self._unregister_script_tools(skill)
        await repo.remove_skill(db, name)

    # --- 脚本工具注册 ---

    def _register_script_tools(self, skill: SkillModel) -> None:
        """将 skill 目录下 scripts/*.py 注册为工具（skill_{name}__*）。

        约定：脚本文件中的顶层 `run()` 函数即工具逻辑；
        工具 schema 通过脚本内的 `SCHEMA` 注解（dict）声明。
        """
        scripts_dir = Path(skill.path) / "scripts"
        if not scripts_dir.exists():
            return
        for py in scripts_dir.glob("*.py"):
            tool_name = f"skill_{skill.name}__{py.stem}"
            try:
                ns: dict = {}
                exec(compile(py.read_text(encoding="utf-8"), str(py), "exec"), ns)  # noqa: S102
                run_fn = ns.get("run")
                if not callable(run_fn):
                    continue
                from langchain_core.tools import StructuredTool

                tool = StructuredTool.from_function(
                    func=run_fn,
                    name=tool_name,
                    description=ns.get("DESCRIPTION", f"{skill.name} 提供的脚本工具 {py.stem}"),
                )
                tool_registry.register(tool, source=f"skill:{skill.name}")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Skill %s 工具 %s 注册失败: %s", skill.name, py.stem, exc)

    def _unregister_script_tools(self, skill: SkillModel) -> None:
        prefix = f"skill_{skill.name}__"
        for info in list(tool_registry.snapshot()):
            if info.name.startswith(prefix):
                tool_registry.unregister(info.name)


# 全局单例
skill_manager = SkillManager()
