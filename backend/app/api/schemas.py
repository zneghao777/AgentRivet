"""请求/响应 Pydantic 模型（与前端 types 一一对应）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ---------- 通用响应包装 ----------


class ApiResponse(BaseModel):
    code: int = 0
    message: str = "ok"
    data: Any | None = None


class ApiError(BaseModel):
    code: int
    message: str
    data: Any | None = None


# ---------- 会话 ----------


class SessionCreateRequest(BaseModel):
    title: str | None = None


class SessionResponse(BaseModel):
    session_id: str
    thread_id: str
    title: str | None = None
    status: str
    created_at: str | None = None
    updated_at: str | None = None


class SessionListData(BaseModel):
    items: list[SessionResponse]
    total: int
    page: int
    page_size: int


# ---------- 消息 ----------


class MessageSendRequest(BaseModel):
    message: str = Field(min_length=1)
    skills: list[str] | None = None  # 本次会话使用的 Skill 白名单
    knowledge_bases: list[str] | None = None  # 本次会话绑定的知识库名称白名单（默认不启用 RAG）


class MessageStreamRequest(BaseModel):
    message: str = Field(min_length=1)
    skills: list[str] | None = None
    knowledge_bases: list[str] | None = None


class MessageResponse(BaseModel):
    session_id: str
    reply: str
    token_usage: dict[str, int] = Field(default_factory=dict)
    tool_calls: int = 0
    duration_ms: int = 0


class HistoryMessage(BaseModel):
    id: int
    role: str
    content: str
    tool_name: str | None = None
    created_at: str | None = None


class ResumeRequest(BaseModel):
    input: str
    decision: str | None = None


# ---------- Skill ----------


class SkillResponse(BaseModel):
    name: str
    description: str
    version: str
    author: str
    tags: list[str]
    enabled: bool
    path: str


# ---------- Tool ----------


class ToolResponse(BaseModel):
    name: str
    description: str
    source: str


# ---------- Audit ----------


class AuditResponse(BaseModel):
    id: int
    tenant_id: str
    session_id: str | None = None
    user_id: str | None = None
    action: str
    detail: dict | None = None
    ip: str | None = None
    created_at: str | None = None


# ---------- Knowledge（RAG）----------


class KnowledgeBaseCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None


class KnowledgeBaseStatusRequest(BaseModel):
    enabled: bool


class KnowledgeBaseResponse(BaseModel):
    kb_id: str
    name: str
    description: str | None = None
    doc_count: int = 0
    chunk_count: int = 0
    status: str
    created_at: str | None = None
    updated_at: str | None = None


class KnowledgeDocResponse(BaseModel):
    doc_id: int = 0
    source_file: str
    chunk_count: int = 0
    created_at: str | None = None


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=10)


class KnowledgeHit(BaseModel):
    text: str
    source_file: str
    chunk_index: int
    score: float


class KnowledgeUploadResponse(BaseModel):
    kb_id: str
    doc_id: int
    source_file: str
    chunk_count: int
