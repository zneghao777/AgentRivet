"""仓储层：会话、消息、Skill、审计的数据库访问。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AuditLog, Message, Session, Skill


# ---------- Session ----------


async def create_session(
    db: AsyncSession,
    *,
    thread_id: str,
    tenant_id: str,
    user_id: str,
    title: str | None = None,
) -> Session:
    session = Session(
        thread_id=thread_id,
        tenant_id=tenant_id,
        user_id=user_id,
        title=title,
        status="active",
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def get_session(db: AsyncSession, session_id: uuid.UUID) -> Session | None:
    return await db.get(Session, session_id)


async def get_session_by_thread(
    db: AsyncSession, thread_id: str
) -> Session | None:
    result = await db.execute(select(Session).where(Session.thread_id == thread_id))
    return result.scalar_one_or_none()


async def list_sessions(
    db: AsyncSession, *, tenant_id: str, page: int, page_size: int
) -> tuple[list[Session], int]:
    total = (
        await db.execute(
            select(func.count(Session.id)).where(
                Session.tenant_id == tenant_id,
                Session.status != "deleted",
            )
        )
    ).scalar_one()
    result = await db.execute(
        select(Session)
        .where(Session.tenant_id == tenant_id, Session.status != "deleted")
        .order_by(Session.updated_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all()), total


async def soft_delete_session(db: AsyncSession, session_id: uuid.UUID) -> bool:
    result = await db.execute(
        update(Session)
        .where(Session.id == session_id)
        .values(status="deleted", updated_at=datetime.now())
    )
    await db.commit()
    return result.rowcount > 0


async def set_session_status(
    db: AsyncSession, session_id: uuid.UUID, status: str
) -> None:
    await db.execute(
        update(Session)
        .where(Session.id == session_id)
        .values(status=status, updated_at=datetime.now())
    )
    await db.commit()


async def set_session_title(
    db: AsyncSession, session_id: uuid.UUID, title: str
) -> None:
    await db.execute(
        update(Session)
        .where(Session.id == session_id)
        .values(title=title, updated_at=datetime.now())
    )
    await db.commit()


# ---------- Message ----------


async def add_message(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    role: str,
    content: str,
    tool_name: str | None = None,
    token_usage: dict | None = None,
) -> Message:
    msg = Message(
        session_id=session_id,
        role=role,
        content=content,
        tool_name=tool_name,
        token_usage=token_usage,
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    return msg


async def list_messages(
    db: AsyncSession, session_id: uuid.UUID, limit: int = 100
) -> list[Message]:
    result = await db.execute(
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.created_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


# ---------- Skill ----------


async def upsert_skill(db: AsyncSession, skill: Skill) -> Skill:
    """按 name 做 upsert（存在则更新，不存在则插入）。"""
    existing = await get_skill(db, skill.name)
    if existing is not None:
        existing.path = skill.path
        existing.description = skill.description
        existing.version = skill.version
        existing.author = skill.author
        existing.tags = skill.tags
        existing.enabled = skill.enabled
        existing.updated_at = datetime.now()
        await db.commit()
        await db.refresh(existing)
        return existing
    db.add(skill)
    await db.commit()
    await db.refresh(skill)
    return skill


async def get_skill(db: AsyncSession, name: str) -> Skill | None:
    result = await db.execute(select(Skill).where(Skill.name == name))
    return result.scalar_one_or_none()


async def list_skills(db: AsyncSession) -> list[Skill]:
    result = await db.execute(select(Skill).order_by(Skill.name.asc()))
    return list(result.scalars().all())


async def set_skill_enabled(
    db: AsyncSession, name: str, enabled: bool
) -> Skill | None:
    skill = await get_skill(db, name)
    if skill:
        skill.enabled = enabled
        skill.updated_at = datetime.now()
        await db.commit()
        await db.refresh(skill)
    return skill


async def remove_skill(db: AsyncSession, name: str) -> None:
    await db.execute(delete(Skill).where(Skill.name == name))
    await db.commit()


# ---------- Audit ----------


async def add_audit(
    db: AsyncSession,
    *,
    tenant_id: str,
    action: str,
    user_id: str | None = None,
    session_id: uuid.UUID | None = None,
    detail: dict | None = None,
    ip: str | None = None,
) -> AuditLog:
    log = AuditLog(
        tenant_id=tenant_id,
        action=action,
        user_id=user_id,
        session_id=session_id,
        detail=detail,
        ip=ip,
    )
    db.add(log)
    await db.commit()
    return log


async def query_audit(
    db: AsyncSession,
    *,
    tenant_id: str | None = None,
    action: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[AuditLog], int]:
    stmt = select(AuditLog)
    count_stmt = select(func.count(AuditLog.id))
    if tenant_id:
        stmt = stmt.where(AuditLog.tenant_id == tenant_id)
        count_stmt = count_stmt.where(AuditLog.tenant_id == tenant_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
        count_stmt = count_stmt.where(AuditLog.action == action)
    total = (await db.execute(count_stmt)).scalar_one()
    result = await db.execute(
        stmt.order_by(AuditLog.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all()), total
