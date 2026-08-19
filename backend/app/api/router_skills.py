"""Skill 管理路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..observability import audit
from ..skills.manager import skill_manager
from ..storage.db import get_db
from .deps import AuthContext, require_auth
from .schemas import ApiResponse, SkillResponse

router = APIRouter(prefix="/skills", tags=["skills"])


def _to_resp(skill) -> SkillResponse:
    return SkillResponse(
        name=skill.name,
        description=skill.description,
        version=skill.version,
        author=skill.author,
        tags=skill.tags,
        enabled=skill.enabled,
        path=skill.path,
    )


@router.get("", response_model=ApiResponse)
async def list_skills(
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    skills = skill_manager.all()
    return ApiResponse(data=[_to_resp(s) for s in skills])


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
    await audit.audit(
        auth.tenant.tenant_id, "skill.reload", user_id=auth.user_id
    )
    return ApiResponse(data=[_to_resp(s) for s in skills])
