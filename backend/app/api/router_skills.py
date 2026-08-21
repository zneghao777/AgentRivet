"""Skill 管理路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from ..harness.app_runtime import harness_app
from ..observability import audit
from ..skills.importer import (
    MAX_IMPORT_BYTES,
    SkillImportError,
    UploadedSkillFile,
    install_uploaded_skills,
    parse_relative_paths,
)
from ..skills.manager import EXTERNAL_SKILLS_DIR, skill_manager
from ..storage.db import get_db
from ..tools.registry import tool_registry
from .deps import AuthContext, require_auth
from .schemas import ApiResponse, SkillResponse

router = APIRouter(prefix="/skills", tags=["skills"])


def _rebuild_harness() -> None:
    """ToolNode 在编译时固定工具集，Skill 生命周期变化后必须重建。"""
    harness_app.build(tool_registry, checkpointer=harness_app.checkpointer)


def _to_resp(skill) -> SkillResponse:
    return SkillResponse(
        name=skill.name,
        description=skill.description,
        version=skill.version,
        author=skill.author,
        tags=skill.tags,
        enabled=skill.enabled,
        path=skill.path,
        source=skill.source,
        file_count=skill.file_count,
        has_scripts=skill.has_scripts,
    )


@router.get("", response_model=ApiResponse)
async def list_skills(
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    skills = sorted(skill_manager.all(), key=lambda item: (item.source != "builtin", item.name))
    return ApiResponse(data=[_to_resp(s) for s in skills])


@router.post("/import", response_model=ApiResponse)
async def import_skills(
    files: list[UploadFile] = File(...),
    relative_paths: str | None = Form(None),
    overwrite: bool = Form(False),
    activate: bool = Form(False),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """导入 ZIP/TAR/skill 包、浏览器文件夹或单个 Markdown manifest。"""
    filenames = [upload.filename or "unnamed" for upload in files]
    try:
        paths = parse_relative_paths(relative_paths, filenames)
        uploaded: list[UploadedSkillFile] = []
        total = 0
        for upload, filename, relative_path in zip(files, filenames, paths, strict=True):
            content = await upload.read(MAX_IMPORT_BYTES + 1)
            total += len(content)
            if total > MAX_IMPORT_BYTES:
                raise SkillImportError("单次导入内容不能超过 50 MB")
            uploaded.append(
                UploadedSkillFile(
                    filename=filename,
                    relative_path=relative_path,
                    content=content,
                )
            )
        result = await run_in_threadpool(
            install_uploaded_skills,
            uploaded,
            external_root=EXTERNAL_SKILLS_DIR,
            existing={skill.name: skill for skill in skill_manager.all()},
            overwrite=overwrite,
        )
    except SkillImportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await skill_manager.reload(db)
    imported = []
    for imported_skill in result.imported:
        skill = (
            await skill_manager.load(db, imported_skill.name)
            if activate
            else await skill_manager.disable(db, imported_skill.name)
        )
        if skill is not None:
            imported.append(_to_resp(skill))
    _rebuild_harness()
    await audit.audit(
        auth.tenant.tenant_id,
        "skill.import",
        user_id=auth.user_id,
        detail={
            "skills": [skill.name for skill in imported],
            "replaced": result.replaced,
            "activated": activate,
        },
    )
    return ApiResponse(
        data={
            "skills": imported,
            "replaced": result.replaced,
            "warnings": result.warnings,
        }
    )


@router.get("/{name}", response_model=ApiResponse)
async def get_skill(
    name: str,
    auth: AuthContext = Depends(require_auth),
):
    skill = skill_manager.get(name)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    return ApiResponse(data=_to_resp(skill))


@router.post("/{name}/enable", response_model=ApiResponse)
async def enable_skill(
    name: str,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    skill = await skill_manager.load(db, name)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    _rebuild_harness()
    await audit.audit(
        auth.tenant.tenant_id, "skill.enable", user_id=auth.user_id, detail={"skill": name}
    )
    return ApiResponse(data=_to_resp(skill))


@router.post("/{name}/disable", response_model=ApiResponse)
async def disable_skill(
    name: str,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    skill = await skill_manager.disable(db, name)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    _rebuild_harness()
    await audit.audit(
        auth.tenant.tenant_id, "skill.disable", user_id=auth.user_id, detail={"skill": name}
    )
    return ApiResponse(data=_to_resp(skill))


@router.post("/reload", response_model=ApiResponse)
async def reload_skills(
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    skills = await skill_manager.reload(db)
    _rebuild_harness()
    await audit.audit(
        auth.tenant.tenant_id, "skill.reload", user_id=auth.user_id
    )
    return ApiResponse(data=[_to_resp(s) for s in skills])


@router.delete("/{name}", response_model=ApiResponse)
async def delete_skill(
    name: str,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    skill = skill_manager.get(name)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    if skill.source == "builtin":
        raise HTTPException(status_code=409, detail="内置 Skill 不可删除")
    removed = await skill_manager.remove(db, name, delete_files=True)
    if not removed:
        raise HTTPException(status_code=404, detail="skill not found")
    _rebuild_harness()
    await audit.audit(
        auth.tenant.tenant_id,
        "skill.delete",
        user_id=auth.user_id,
        detail={"skill": name},
    )
    return ApiResponse(data={"name": name})
