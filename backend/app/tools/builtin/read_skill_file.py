"""内置工具：读取已注册 Skill 目录中的 UTF-8 文本资源。"""

from __future__ import annotations

from pathlib import Path

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from ...config import settings

_DENIED_FILENAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "credentials.json",
    "secrets.json",
}
_DENIED_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}


class ReadSkillFileArgs(BaseModel):
    skill_name: str = Field(
        description="资源所属的已加载 Skill 名称，例如 agent-mbti。"
    )
    path: str = Field(
        description=(
            "相对于该 Skill 根目录的文本文件路径，例如 "
            "references/survey-free.json。不得使用绝对路径或 ..。"
        )
    )


def _has_symlink(path: Path, root: Path) -> bool:
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _read_skill_file(skill_name: str, path: str) -> str:
    """读取 Skill 内部文本资源，并返回可直接供模型使用的正文。"""
    # 惰性导入避免 SkillManager 注册脚本工具时与 ToolRegistry 循环依赖。
    from ...skills.manager import skill_manager

    skill = skill_manager.get(skill_name)
    if skill is None:
        return f"读取失败：Skill {skill_name!r} 未注册。"
    if not skill.enabled:
        return f"读取失败：Skill {skill_name!r} 当前未启用。"

    relative = Path(path.strip())
    if not path.strip() or relative.is_absolute() or ".." in relative.parts:
        return "读取失败：path 必须是 Skill 目录内不含 .. 的相对路径。"
    if any(part.startswith(".") for part in relative.parts):
        return "读取失败：不允许读取隐藏文件或隐藏目录。"
    if relative.name.lower() in _DENIED_FILENAMES or relative.suffix.lower() in _DENIED_SUFFIXES:
        return "读取失败：该文件类型可能包含凭据，禁止读取。"

    root = Path(skill.path).resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root) or _has_symlink(root / relative, root):
        return "读取失败：目标路径超出 Skill 目录或包含符号链接。"
    if not candidate.is_file():
        return f"读取失败：Skill {skill_name!r} 中不存在文件 {path!r}。"

    max_bytes = settings.SKILL_FILE_READ_MAX_BYTES
    try:
        with candidate.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
    except OSError as exc:
        return f"读取失败：无法读取文件（{exc.strerror or 'I/O error'}）。"
    if len(raw) > max_bytes:
        return f"读取失败：文件超过 {max_bytes} 字节的单次读取上限。"
    if b"\x00" in raw:
        return "读取失败：仅支持 UTF-8 文本资源，不支持二进制文件。"
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "读取失败：文件不是有效的 UTF-8 文本。"

    return (
        f"[Skill resource: {skill_name}/{relative.as_posix()} | {len(raw)} bytes]\n"
        f"{content}"
    )


builtin_read_skill_file = StructuredTool.from_function(
    func=_read_skill_file,
    name="builtin_read_skill_file",
    description=(
        "读取已加载 Skill 目录中的 UTF-8 文本资源。"
        "当 Skill 指令引用 references/*.json、*.md 或其他相对文件时使用；"
        "必须传入 Skill 名称和相对路径。不能读取 Skill 目录之外的文件。"
    ),
    args_schema=ReadSkillFileArgs,
)
