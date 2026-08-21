"""安全导入本地 Skill 包、文件夹或单个 Markdown manifest。"""

from __future__ import annotations

import io
import json
import os
import shutil
import tarfile
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .loader import SkillValidationError, discover_skill_dirs, load_skill_from_dir
from .models import SkillModel

MAX_IMPORT_BYTES = 50 * 1024 * 1024
MAX_IMPORT_FILES = 1000
IGNORED_PARTS = {"__MACOSX", ".DS_Store", "__pycache__", ".git"}


class SkillImportError(ValueError):
    """导入内容不安全、格式错误或存在冲突。"""


@dataclass(frozen=True)
class UploadedSkillFile:
    filename: str
    relative_path: str
    content: bytes


@dataclass
class SkillImportResult:
    imported: list[SkillModel] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def parse_relative_paths(raw: str | None, filenames: list[str]) -> list[str]:
    """解析前端为文件夹上传提供的 webkitRelativePath 列表。"""
    if not raw:
        return filenames
    try:
        paths = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SkillImportError("relative_paths 不是有效 JSON") from exc
    if not isinstance(paths, list) or len(paths) != len(filenames):
        raise SkillImportError("relative_paths 必须与上传文件一一对应")
    return [str(path) for path in paths]


def _safe_relative_path(raw: str) -> Path:
    value = raw.replace("\\", "/").strip()
    pure = PurePosixPath(value)
    if not value or pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise SkillImportError(f"文件路径不安全：{raw}")
    return Path(*pure.parts)


def _is_ignored(path: Path) -> bool:
    return any(part in IGNORED_PARTS for part in path.parts)


def _write_file(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def _extract_zip(content: bytes, target: Path) -> None:
    total = 0
    count = 0
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise SkillImportError("ZIP/skill 包已损坏或格式不受支持") from exc
    with archive:
        for item in archive.infolist():
            if item.is_dir():
                if item.filename.rstrip("/"):
                    _safe_relative_path(item.filename.rstrip("/"))
                continue
            relative = _safe_relative_path(item.filename)
            if _is_ignored(relative):
                continue
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise SkillImportError(f"压缩包不能包含符号链接：{item.filename}")
            total += item.file_size
            count += 1
            if total > MAX_IMPORT_BYTES or count > MAX_IMPORT_FILES:
                raise SkillImportError("解压后超过 50 MB 或 1000 个文件的导入限制")
            _write_file(target / relative, archive.read(item))


def _extract_tar(content: bytes, target: Path) -> None:
    total = 0
    count = 0
    try:
        archive = tarfile.open(fileobj=io.BytesIO(content), mode="r:*")
    except tarfile.TarError as exc:
        raise SkillImportError("TAR 包已损坏或格式不受支持") from exc
    with archive:
        for item in archive.getmembers():
            if item.isdir():
                if item.name.rstrip("/") not in {"", "."}:
                    _safe_relative_path(item.name.rstrip("/"))
                continue
            relative = _safe_relative_path(item.name)
            if _is_ignored(relative):
                continue
            if not item.isfile():
                raise SkillImportError(f"压缩包只能包含普通文件和目录：{item.name}")
            total += item.size
            count += 1
            if total > MAX_IMPORT_BYTES or count > MAX_IMPORT_FILES:
                raise SkillImportError("解压后超过 50 MB 或 1000 个文件的导入限制")
            stream = archive.extractfile(item)
            if stream is None:
                raise SkillImportError(f"无法读取压缩包文件：{item.name}")
            _write_file(target / relative, stream.read())


def _looks_like_archive(file: UploadedSkillFile) -> bool:
    lower = file.filename.lower()
    return (
        lower.endswith((".zip", ".skill", ".tar", ".tar.gz", ".tgz"))
        or file.content.startswith(b"PK\x03\x04")
    )


def _materialize_upload(files: list[UploadedSkillFile], target: Path) -> None:
    if not files:
        raise SkillImportError("请选择 Skill 文件、文件夹或压缩包")
    if len(files) > MAX_IMPORT_FILES:
        raise SkillImportError("单次最多导入 1000 个文件")
    total = sum(len(file.content) for file in files)
    if total > MAX_IMPORT_BYTES:
        raise SkillImportError("单次导入内容不能超过 50 MB")

    if len(files) == 1 and _looks_like_archive(files[0]):
        upload = files[0]
        lower = upload.filename.lower()
        if lower.endswith((".tar", ".tar.gz", ".tgz")):
            _extract_tar(upload.content, target)
        else:
            _extract_zip(upload.content, target)
        return

    if len(files) == 1 and files[0].filename.lower().endswith(".md"):
        _write_file(target / "SKILL.md", files[0].content)
        return

    for upload in files:
        relative = _safe_relative_path(upload.relative_path or upload.filename)
        if _is_ignored(relative):
            continue
        _write_file(target / relative, upload.content)


def _collect_candidates(workspace: Path) -> list[tuple[Path, SkillModel]]:
    candidates: list[tuple[Path, SkillModel]] = []
    errors: list[str] = []
    for directory in discover_skill_dirs(workspace):
        try:
            skill = load_skill_from_dir(directory, strict=True)
        except SkillValidationError as exc:
            errors.append(f"{directory.relative_to(workspace)}：{exc}")
            continue
        if skill is not None:
            candidates.append((directory, skill))
    if errors:
        raise SkillImportError("；".join(errors))
    if not candidates:
        raise SkillImportError("未找到有效 SKILL.md；manifest 至少需要 name 和 description")
    names = [skill.name for _, skill in candidates]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise SkillImportError(f"导入内容包含同名 Skill：{', '.join(duplicates)}")
    return candidates


def install_uploaded_skills(
    files: list[UploadedSkillFile],
    *,
    external_root: Path,
    existing: dict[str, SkillModel],
    overwrite: bool,
) -> SkillImportResult:
    """校验全部候选后原子写入外部 Skill 根目录。"""
    with tempfile.TemporaryDirectory(prefix="skill-import-") as temporary:
        workspace = Path(temporary) / "payload"
        workspace.mkdir()
        _materialize_upload(files, workspace)
        candidates = _collect_candidates(workspace)

        conflicts: list[str] = []
        for _, skill in candidates:
            current = existing.get(skill.name)
            if current is None:
                continue
            if current.source == "builtin":
                conflicts.append(f"{skill.name}（内置 Skill 不可替换）")
            elif not overwrite:
                conflicts.append(skill.name)
        if conflicts:
            raise SkillImportError(f"Skill 已存在：{', '.join(conflicts)}")

        external_root.mkdir(parents=True, exist_ok=True)
        installed: list[tuple[Path, Path | None]] = []
        result = SkillImportResult()
        try:
            for source_dir, skill in candidates:
                target = external_root / skill.name
                incoming = external_root / f".import-{skill.name}-{uuid.uuid4().hex}"
                backup: Path | None = None
                shutil.copytree(source_dir, incoming)
                if target.exists():
                    backup = external_root / f".backup-{skill.name}-{uuid.uuid4().hex}"
                    os.replace(target, backup)
                    result.replaced.append(skill.name)
                os.replace(incoming, target)
                installed.append((target, backup))
        except Exception:
            for target, backup in reversed(installed):
                if target.exists():
                    shutil.rmtree(target)
                if backup and backup.exists():
                    os.replace(backup, target)
            raise
        else:
            for _, backup in installed:
                if backup and backup.exists():
                    shutil.rmtree(backup)

        result.imported = [skill for _, skill in candidates]
        if any(skill.has_scripts for skill in result.imported):
            result.warnings.append("包含 Python 脚本；启用前请确认代码来源可信")
        return result
