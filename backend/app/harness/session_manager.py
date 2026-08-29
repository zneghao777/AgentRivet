"""Session ↔ thread_id 管理。

映射规则：Session → thread_id 一对一，thread_id 生成规则 thr_{uuid4hex}。
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from ..storage import repositories as repo


def gen_thread_id() -> str:
    return f"thr_{uuid.uuid4().hex}"


def derive_session_title(message: str, max_length: int = 20) -> str:
    """Build a compact session title from the first user message."""
    compact = " ".join(message.split()).strip()
    return compact if len(compact) <= max_length else f"{compact[:max_length]}…"


class SessionManager:
    """会话管理。"""

    async def create_session(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        user_id: str,
        title: str | None = None,
        agent_id: uuid.UUID | None = None,
    ) -> dict:
        thread_id = gen_thread_id()
        session = await repo.create_session(
            db,
            thread_id=thread_id,
            tenant_id=tenant_id,
            user_id=user_id,
            title=title,
            agent_id=agent_id,
        )
        return {
            "session_id": str(session.id),
            "thread_id": session.thread_id,
            "title": session.title,
            "status": session.status,
            "agent_id": str(session.agent_id) if session.agent_id else None,
            "created_at": session.created_at.isoformat() if session.created_at else None,
            "updated_at": session.updated_at.isoformat() if session.updated_at else None,
        }

    async def resume_session(
        self, db: AsyncSession, session_id: uuid.UUID
    ) -> dict | None:
        """校验存在性，返回 thread_id。"""
        session = await repo.get_session(db, session_id)
        if session is None or session.status == "deleted":
            return None
        return {"session_id": str(session.id), "thread_id": session.thread_id}

    async def delete_session(self, db: AsyncSession, session_id: uuid.UUID) -> bool:
        return await repo.soft_delete_session(db, session_id)

    async def list_sessions(
        self, db: AsyncSession, *, tenant_id: str, page: int, page_size: int
    ) -> tuple[list[dict], int]:
        sessions, total = await repo.list_sessions(
            db, tenant_id=tenant_id, page=page, page_size=page_size
        )
        items = []
        for s in sessions:
            title = s.title
            if not title or title in {"新对话", "新会话"}:
                messages = await repo.list_messages(db, s.id, limit=1)
                if messages and messages[0].role == "user":
                    title = derive_session_title(messages[0].content)
            items.append({
                "session_id": str(s.id),
                "thread_id": s.thread_id,
                "title": title,
                "status": s.status,
                "agent_id": str(s.agent_id) if s.agent_id else None,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            })
        return items, total

    async def get_history(
        self, db: AsyncSession, session_id: uuid.UUID, limit: int = 100
    ) -> list[dict]:
        msgs = await repo.list_messages(db, session_id, limit=limit)
        agent_ids = {m.agent_id for m in msgs if m.agent_id is not None}
        names: dict[uuid.UUID, str] = {}
        if agent_ids:
            from sqlalchemy import select

            from ..storage.models import Agent

            names = dict(
                (
                    await db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids)))
                ).all()
            )
        return [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "tool_name": m.tool_name,
                "token_usage": m.token_usage,
                "trace_id": m.trace_id,
                "request_id": m.request_id,
                "step_index": m.step_index,
                "agent_id": str(m.agent_id) if m.agent_id else None,
                "agent_name": names.get(m.agent_id) if m.agent_id else None,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in msgs
        ]


session_manager = SessionManager()
