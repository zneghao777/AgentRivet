"""ORM 模型：tenants / sessions / messages / skills / audit_logs / knowledge。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..config import settings


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """声明式基类。"""


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    api_key_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    thread_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    title: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active"
    )  # active / completed / interrupted / deleted
    # AgentHub 会话绑定；不加外键以保留 Agent 归档后的历史审计引用。
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sessions.id"), index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user/assistant/tool
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(128))
    token_usage: Mapped[dict | None] = mapped_column(JSON)
    # 可观测性关联：允许 Trace 详情按一次请求精确回放输入与最终输出。
    # 历史消息保持 nullable，由事件接口使用 session_id + 时间窗兼容。
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    span_id: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(128), index=True)
    # 顶层异步对话运行关联；历史消息保持 nullable。
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("chat_runs.id", name="fk_messages_run_id", use_alter=True),
        nullable=True,
        index=True,
    )
    turn_index: Mapped[int | None] = mapped_column(Integer)
    step_index: Mapped[int | None] = mapped_column(Integer)
    # 记录生成该条助手消息时实际解析到的 Agent。
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class ChatRun(Base):
    """一条用户消息触发的顶层、可恢复 Agent 运行。"""

    __tablename__ = "chat_runs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "client_request_id",
            name="uq_chat_runs_client_request",
        ),
        Index("ix_chat_runs_session_created", "session_id", "created_at"),
        Index(
            "ix_chat_runs_tenant_user_status_created",
            "tenant_id",
            "user_id",
            "status",
            "created_at",
        ),
        Index(
            "uq_chat_runs_session_active",
            "session_id",
            unique=True,
            postgresql_where=text(
                "status IN ('queued', 'running', 'pending_approval')"
            ),
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'pending_approval', "
            "'succeeded', 'failed', 'canceled')",
            name="ck_chat_run_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    client_request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_message_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("messages.id"), nullable=True
    )
    assistant_message_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("messages.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    request_payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    last_event_sequence: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ChatRunEvent(Base):
    """供 Chat UI 重放的有序运行事件。"""

    __tablename__ = "chat_run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_chat_run_event_sequence"),
        Index("ix_chat_run_events_run_sequence", "run_id", "sequence"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("chat_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class ChatRunOutbox(Base):
    """ChatRun 命令的 Transactional Outbox。"""

    __tablename__ = "chat_run_outbox"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "command_type", "message_id", name="uq_chat_run_outbox_message"
        ),
        Index("ix_chat_run_outbox_unpublished", "published_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("chat_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    command_type: Mapped[str] = mapped_column(String(32), nullable=False)
    message_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)


class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str | None] = mapped_column(String(32))
    author: Mapped[str | None] = mapped_column(String(128))
    tags: Mapped[list | None] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    loaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Agent(Base):
    """租户内可配置的 Agent 定义（AGENT_HUB_SPEC）。"""

    __tablename__ = "agents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_agent_tenant_name"),
        Index("ix_agent_tenant_status", "tenant_id", "status"),
        CheckConstraint("scope IN ('user', 'internal')", name="ck_agent_scope"),
        CheckConstraint(
            "status IN ('active', 'disabled', 'archived')",
            name="ck_agent_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    system_prompt: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(128))
    model_base_url: Mapped[str | None] = mapped_column(String(2048))
    memory_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AgentSkillBinding(Base):
    __tablename__ = "agent_skill_bindings"
    __table_args__ = (
        UniqueConstraint("agent_id", "skill_id", name="uq_agent_skill_binding"),
        Index("ix_agent_skill_binding_agent", "agent_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE")
    )
    skill_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("skills.id", ondelete="CASCADE")
    )


class AgentToolBinding(Base):
    __tablename__ = "agent_tool_bindings"
    __table_args__ = (
        UniqueConstraint("agent_id", "tool_name", name="uq_agent_tool_binding"),
        Index("ix_agent_tool_binding_agent", "agent_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE")
    )
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)


class AgentMcpBinding(Base):
    __tablename__ = "agent_mcp_bindings"
    __table_args__ = (
        UniqueConstraint("agent_id", "capability_id", name="uq_agent_mcp_binding"),
        Index("ix_agent_mcp_binding_agent", "agent_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE")
    )
    capability_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("mcp_capabilities.id", ondelete="CASCADE"),
    )


class AgentKbBinding(Base):
    __tablename__ = "agent_kb_bindings"
    __table_args__ = (
        UniqueConstraint("agent_id", "kb_id", name="uq_agent_kb_binding"),
        Index("ix_agent_kb_binding_agent", "agent_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE")
    )
    kb_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
    )


class AgentTask(Base):
    """一次多 Agent 委派任务的持久化快照。"""

    __tablename__ = "agent_tasks"
    __table_args__ = (
        Index("ix_agent_tasks_task_id", "task_id"),
        Index("ix_agent_tasks_parent_task_id", "parent_task_id"),
        Index("ix_agent_tasks_session_created", "session_id", "created_at"),
        Index("ix_agent_tasks_trace_id", "trace_id"),
        CheckConstraint(
            "status IN ('pending', 'running', 'done', 'failed')",
            name="ck_agent_task_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[str] = mapped_column(String(32), nullable=False)
    parent_task_id: Mapped[str | None] = mapped_column(String(32))
    session_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=False
    )
    message_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("messages.id"), nullable=True
    )
    target_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    target_agent_name: Mapped[str] = mapped_column(String(128), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    context_brief: Mapped[str | None] = mapped_column(Text)
    constraints: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    expected_output: Mapped[str | None] = mapped_column(Text)
    artifacts: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    result: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class McpServer(Base):
    """租户级 MCP Server 配置；敏感认证信息只保存加密密文。"""

    __tablename__ = "mcp_servers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_mcp_server_tenant_name"),
        Index("ix_mcp_server_tenant_health", "tenant_id", "health_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    transport: Mapped[str] = mapped_column(
        String(24), nullable=False, default="streamable_http"
    )
    endpoint_url: Mapped[str] = mapped_column(Text, nullable=False)
    auth_type: Mapped[str] = mapped_column(String(24), nullable=False, default="none")
    secret_ciphertext: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    health_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unknown"
    )
    protocol_version: Mapped[str | None] = mapped_column(String(32))
    server_info: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    capability_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class McpCapability(Base):
    """MCP Server 最近一次成功发现的能力快照。"""

    __tablename__ = "mcp_capabilities"
    __table_args__ = (
        UniqueConstraint(
            "server_id", "kind", "remote_name", name="uq_mcp_capability_remote"
        ),
        Index("ix_mcp_capability_server_enabled", "server_id", "enabled"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    server_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("mcp_servers.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="tool")
    remote_name: Mapped[str] = mapped_column(String(255), nullable=False)
    effective_name: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    input_schema: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    output_schema: Mapped[dict | None] = mapped_column(JSON)
    annotations: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")
    approval_policy: Mapped[str] = mapped_column(String(16), nullable=False, default="write")
    schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    review_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_tenant_created", "tenant_id", "created_at"),
        Index("uq_audit_logs_mq_message_id", "mq_message_id", unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sessions.id")
    )
    user_id: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSON)
    ip: Mapped[str | None] = mapped_column(String(64))
    # C-12：审计日志与 Trace / Request / Resource 关联（OBSERVABILITY_SPEC §8.2）
    trace_id: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(128))
    resource_id: Mapped[str | None] = mapped_column(String(128))
    mq_message_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class MQProcessedMessage(Base):
    """MQ 消费幂等凭据；Redis 不可用时作为持久化降级。"""

    __tablename__ = "mq_processed_messages"

    message_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    message_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="processing")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class KnowledgeBase(Base):
    """知识库：租户下的命名空间 + 统计信息。"""

    __tablename__ = "knowledge_bases"
    __table_args__ = (
        Index("ix_kb_tenant_name", "tenant_id", "name", unique=True),
        CheckConstraint(
            "index_mode IN ('standard', 'graph_enhanced')",
            name="ck_kb_index_mode",
        ),
        CheckConstraint(
            "graph_build_policy IN ('auto', 'manual')",
            name="ck_kb_graph_build_policy",
        ),
        CheckConstraint(
            "index_mode = 'graph_enhanced' OR graph_build_policy = 'manual'",
            name="ck_kb_standard_manual_policy",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    doc_count: Mapped[int] = mapped_column(default=0)
    chunk_count: Mapped[int] = mapped_column(default=0)
    index_mode: Mapped[str] = mapped_column(
        String(24), nullable=False, default="standard"
    )
    graph_build_policy: Mapped[str] = mapped_column(
        String(16), nullable=False, default="manual"
    )
    index_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="empty"
    )
    graph_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="disabled"
    )
    graph_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # JSONB:与启动迁移 SQL 的 jsonb 运算符保持一致(db.py 使用 || 拼接)
    graph_meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # active / deleted
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class DocumentMeta(Base):
    """文档（原文件）元数据 + 解析任务状态（KB_MANAGEMENT_SPEC §3.1）。"""

    __tablename__ = "document_meta"
    __table_args__ = (
        Index("ix_doc_kb_tenant", "kb_id", "tenant_id"),
        CheckConstraint(
            "graph_override IN ('inherit', 'include', 'exclude')",
            name="ck_doc_graph_override",
        ),
        CheckConstraint(
            "parse_method IN ('text', 'ocr')",
            name="ck_doc_parse_method",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kb_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("knowledge_bases.id"), index=True
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_file: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str | None] = mapped_column(Text)
    file_size: Mapped[int] = mapped_column(default=0)
    mime_type: Mapped[str | None] = mapped_column(String(255))
    # pending -> parsing -> parsed / failed / reparsing
    parse_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )
    # 用户在上传时显式选择：text=本地文本提取，ocr=远程 OCR/版面解析
    parse_method: Mapped[str] = mapped_column(
        String(16), nullable=False, default="text"
    )
    parse_source: Mapped[str | None] = mapped_column(String(16))  # glm / local
    raw_text: Mapped[str | None] = mapped_column(Text)  # 解析出的原始文本
    parser_tool: Mapped[str | None] = mapped_column(String(16))  # lite/expert/prime
    glm_task_id: Mapped[str | None] = mapped_column(String(128))
    error: Mapped[str | None] = mapped_column(Text)
    graph_override: Mapped[str] = mapped_column(
        String(16), nullable=False, default="inherit"
    )
    vector_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )
    graph_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="excluded"
    )
    vector_error: Mapped[str | None] = mapped_column(Text)
    graph_error: Mapped[str | None] = mapped_column(Text)
    vector_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    graph_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    graph_version: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class KnowledgeIndexJob(Base):
    """可恢复的知识库解析、文本索引和图谱任务。"""

    __tablename__ = "knowledge_index_jobs"
    __table_args__ = (
        Index("ix_kb_jobs_tenant_kb", "tenant_id", "kb_id", "created_at"),
        Index("ix_kb_jobs_doc_type", "doc_id", "job_type", "status"),
        CheckConstraint("progress >= 0 AND progress <= 100", name="ck_kb_job_progress"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kb_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("knowledge_bases.id"), nullable=False, index=True
    )
    doc_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("document_meta.id"), nullable=True, index=True
    )
    job_type: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class KnowledgeChunk(Base):
    """文档分块 + 向量。"""

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        Index("ix_chunks_kb", "kb_id"),
        Index("ix_chunks_kb_doc", "kb_id", "doc_id"),
        Index("ix_chunks_kb_enabled", "kb_id", "enabled"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kb_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("knowledge_bases.id"), index=True
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 归属文档（新独立实体），与 source_file 并存
    doc_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("document_meta.id"), nullable=True
    )
    source_file: Mapped[str] = mapped_column(String(255))
    chunk_index: Mapped[int] = mapped_column(default=0)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # embedding 向量列：维度取 EMBEDDING_DIM
    embedding: Mapped[list | None] = mapped_column(Vector(settings.EMBEDDING_DIM))
    # metadata 为 SQLAlchemy 保留字，DB 列名仍为 metadata
    meta: Mapped[dict | None] = mapped_column("metadata", JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class MemoryEntry(Base):
    """跨会话长期记忆（MEMORY_SPEC §5.1）。

    用户级记忆：唯一归属 (tenant_id, user_id) 二元组。
    - memory_type: semantic / episodic
    - status: active / pending / superseded / disabled / expired
    - 检索仅命中 status == "active"
    """

    __tablename__ = "memory_entries"
    __table_args__ = (
        Index("ix_mem_tenant_user", "tenant_id", "user_id"),
        Index("ix_mem_tenant_type", "tenant_id", "memory_type"),
        Index("ix_mem_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    memory_type: Mapped[str] = mapped_column(String(16), nullable=False)  # semantic/episodic
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active"
    )  # active/pending/superseded/disabled/expired
    confidence: Mapped[float] = mapped_column(default=1.0)
    embedding: Mapped[list | None] = mapped_column(Vector(settings.EMBEDDING_DIM))
    source_session_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sessions.id")
    )
    source_summary: Mapped[str | None] = mapped_column(Text)
    access_count: Mapped[int] = mapped_column(default=0)
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    meta: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class MemorySettings(Base):
    """用户级长期记忆开关（MEMORY_SPEC §5.2）：(tenant_id, user_id) 复合主键。"""

    __tablename__ = "memory_settings"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    long_term_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_extract: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    ttl_days: Mapped[int] = mapped_column(nullable=False, default=90)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AgentSpan(Base):
    """Agent 执行 trace 明细（OBSERVABILITY_SPEC §9.2）：一张表存所有 span。

    span_type: root / agent / workflow / node / llm / tool / retriever / memory / embedding / hitl / internal
    status: queued / running / ok / error / timeout / canceled / interrupted
    """

    __tablename__ = "agent_spans"
    __table_args__ = (
        Index("ix_spans_trace", "trace_id"),
        Index("ix_spans_tenant_started", "tenant_id", "started_at"),
        Index("ix_spans_type_started", "span_type", "started_at"),
        Index("ix_spans_session", "session_id"),
        Index("ix_spans_parent", "parent_span_id"),
        UniqueConstraint("trace_id", "span_id", name="uq_spans_trace_span"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    span_id: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_span_id: Mapped[str | None] = mapped_column(String(64))  # null=根
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sessions.id")
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(64))
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True, index=True
    )
    agent_name: Mapped[str | None] = mapped_column(String(128))
    span_name: Mapped[str] = mapped_column(String(255), nullable=False)
    span_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ok")
    model: Mapped[str | None] = mapped_column(String(128))
    token_usage: Mapped[dict | None] = mapped_column(JSON)
    input_summary: Mapped[str | None] = mapped_column(Text)
    output_summary: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int] = mapped_column(default=0)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attributes: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    # --- v2 扩展字段（OBSERVABILITY_SPEC §9.2）---
    run_id: Mapped[str | None] = mapped_column(String(64))
    parent_run_id: Mapped[str | None] = mapped_column(String(64))
    links: Mapped[list | None] = mapped_column(JSON)
    status_code: Mapped[str | None] = mapped_column(String(32))
    status_message: Mapped[str | None] = mapped_column(Text)
    model_provider: Mapped[str | None] = mapped_column(String(64))
    model_name_requested: Mapped[str | None] = mapped_column(String(128))
    model_name_resolved: Mapped[str | None] = mapped_column(String(128))
    model_parameters: Mapped[dict | None] = mapped_column(JSON)
    finish_reason: Mapped[str | None] = mapped_column(String(64))
    provider_request_id: Mapped[str | None] = mapped_column(String(128))
    prompt_name: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    prompt_hash: Mapped[str | None] = mapped_column(String(64))
    ttft_ms: Mapped[int | None] = mapped_column(Integer)
    retry_count: Mapped[int] = mapped_column(default=0)
    cost_input: Mapped[float | None] = mapped_column(Numeric(18, 8))
    cost_output: Mapped[float | None] = mapped_column(Numeric(18, 8))
    cost_total: Mapped[float | None] = mapped_column(Numeric(18, 8))
    currency: Mapped[str | None] = mapped_column(String(8))
    events: Mapped[list | None] = mapped_column(JSON)
    content_capture_mode: Mapped[str | None] = mapped_column(String(32))
    redaction_version: Mapped[str | None] = mapped_column(String(32))
    request_id: Mapped[str | None] = mapped_column(String(128))
    thread_id: Mapped[str | None] = mapped_column(String(64))


class AgentMetricsDaily(Base):
    """每日聚合指标（OBSERVABILITY_SPEC §2.1）：供 Dashboard 卡片/折线快速读取。"""

    __tablename__ = "agent_metrics_daily"
    __table_args__ = (
        Index("ix_metrics_tenant_day", "tenant_id", "day", unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    day: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # 按天
    requests: Mapped[int] = mapped_column(default=0)
    errors: Mapped[int] = mapped_column(default=0)
    tokens_prompt: Mapped[int] = mapped_column(default=0)
    tokens_completion: Mapped[int] = mapped_column(default=0)
    tokens_total: Mapped[int] = mapped_column(default=0)
    tool_calls: Mapped[int] = mapped_column(default=0)
    latency_p50_ms: Mapped[int] = mapped_column(default=0)
    latency_p95_ms: Mapped[int] = mapped_column(default=0)
    duration_avg_ms: Mapped[int] = mapped_column(default=0)


class AgentMetricsHourly(Base):
    """小时聚合指标（OBSERVABILITY_SPEC §9.3）。按 (tenant_id, bucket) 唯一。"""

    __tablename__ = "agent_metrics_hourly"
    __table_args__ = (
        Index("ix_metrics_hourly_tenant_bucket", "tenant_id", "bucket", unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    requests: Mapped[int] = mapped_column(default=0)
    errors: Mapped[int] = mapped_column(default=0)
    timeouts: Mapped[int] = mapped_column(default=0)
    canceled: Mapped[int] = mapped_column(default=0)
    tokens_input: Mapped[int] = mapped_column(default=0)
    tokens_output: Mapped[int] = mapped_column(default=0)
    tokens_total: Mapped[int] = mapped_column(default=0)
    cost_total: Mapped[float] = mapped_column(Numeric(18, 8), default=0)
    tool_calls: Mapped[int] = mapped_column(default=0)
    latency_p50_ms: Mapped[int] = mapped_column(default=0)
    latency_p95_ms: Mapped[int] = mapped_column(default=0)
    latency_p99_ms: Mapped[int] = mapped_column(default=0)
    ttft_p95_ms: Mapped[int] = mapped_column(default=0)
    duration_avg_ms: Mapped[int] = mapped_column(default=0)


class AgentTrace(Base):
    """Trace 汇总（OBSERVABILITY_SPEC §9.1）：列表与聚合字段，与 agent_spans 分表。"""

    __tablename__ = "agent_traces"
    __table_args__ = (
        Index("ix_traces_tenant_started", "tenant_id", "started_at"),
        Index("ix_traces_tenant_status_started", "tenant_id", "status", "started_at"),
        Index("ix_traces_tenant_session", "tenant_id", "session_id", "started_at"),
        Index("ix_traces_tenant_user", "tenant_id", "user_id", "started_at"),
    )

    trace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str | None] = mapped_column(String(128))
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str | None] = mapped_column(String(64))
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True, index=True
    )
    agent_name: Mapped[str | None] = mapped_column(String(128))
    session_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    thread_id: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="agent.run")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ok")
    model_names: Mapped[list | None] = mapped_column(JSON)
    span_count: Mapped[int] = mapped_column(default=0)
    llm_calls: Mapped[int] = mapped_column(default=0)
    tool_calls: Mapped[int] = mapped_column(default=0)
    token_usage: Mapped[dict | None] = mapped_column(JSON)
    cost: Mapped[float] = mapped_column(Numeric(18, 8), default=0)
    duration_ms: Mapped[int] = mapped_column(default=0)
    ttft_ms: Mapped[int] = mapped_column(default=0)
    input_preview: Mapped[str | None] = mapped_column(Text)
    output_preview: Mapped[str | None] = mapped_column(Text)
    prompt_versions: Mapped[list | None] = mapped_column(JSON)
    tags: Mapped[list | None] = mapped_column(JSON)
    meta: Mapped[dict | None] = mapped_column("metadata", JSON)
    error_type: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sampled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    content_capture_mode: Mapped[str | None] = mapped_column(String(32))
    has_feedback: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ObservabilityLog(Base):
    """结构化应用日志（OBSERVABILITY_SPEC §8.1 / §9.3）。"""

    __tablename__ = "observability_logs"
    __table_args__ = (
        Index("ix_obslog_tenant_ts", "tenant_id", "timestamp"),
        Index("ix_obslog_trace", "trace_id"),
        Index("ix_obslog_span", "span_id"),
        Index("ix_obslog_severity", "severity_number"),
        Index("uq_observability_logs_mq_message_id", "mq_message_id", unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    severity_text: Mapped[str] = mapped_column(String(16), nullable=False, default="INFO")
    severity_number: Mapped[int] = mapped_column(default=9)
    body: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    span_id: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(128))
    session_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(64))
    logger_name: Mapped[str | None] = mapped_column(String(128))
    module: Mapped[str | None] = mapped_column(String(128))
    function: Mapped[str | None] = mapped_column(String(128))
    service_name: Mapped[str | None] = mapped_column(String(64))
    instance_id: Mapped[str | None] = mapped_column(String(64))
    environment: Mapped[str | None] = mapped_column(String(32))
    exception_type: Mapped[str | None] = mapped_column(String(128))
    exception_message: Mapped[str | None] = mapped_column(Text)
    stacktrace: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict | None] = mapped_column(JSON)
    mq_message_id: Mapped[str | None] = mapped_column(String(36))


class ModelPriceVersion(Base):
    """带生效时间的模型价格（OBSERVABILITY_SPEC §6.4 / §9.3）。"""

    __tablename__ = "model_price_versions"
    __table_args__ = (
        Index("ix_price_model_effective", "model_name", "effective_from"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    model_provider: Mapped[str | None] = mapped_column(String(64))
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    input_price_per_million: Mapped[float] = mapped_column(Numeric(18, 8), default=0)
    cached_input_price_per_million: Mapped[float] = mapped_column(Numeric(18, 8), default=0)
    output_price_per_million: Mapped[float] = mapped_column(Numeric(18, 8), default=0)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    version: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TraceScore(Base):
    """Trace 评分：用户反馈 / 人工标注 / 自动评测（OBSERVABILITY_SPEC §11.1）。"""

    __tablename__ = "trace_scores"
    __table_args__ = (
        Index("ix_scores_trace", "trace_id"),
        Index("ix_scores_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    score_name: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # user/human/rule/llm_eval/rag_eval
    evaluator_version: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    creator_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvaluationRun(Base):
    """数据集评测 / 版本对比（OBSERVABILITY_SPEC §11.2）。"""

    __tablename__ = "evaluation_runs"
    __table_args__ = (
        Index("ix_eval_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")  # pending/running/done/failed
    dataset: Mapped[dict | None] = mapped_column(JSON)
    variants: Mapped[list | None] = mapped_column(JSON)  # 版本对比列表
    results: Mapped[list | None] = mapped_column(JSON)
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AlertRule(Base):
    """SLO / 告警规则（OBSERVABILITY_SPEC §10.2）。"""

    __tablename__ = "alert_rules"
    __table_args__ = (
        Index("ix_alert_tenant", "tenant_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)  # error_rate/p95_latency/...
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    comparison: Mapped[str] = mapped_column(String(8), nullable=False, default=">")
    window_minutes: Mapped[int] = mapped_column(default=15)
    min_samples: Mapped[int] = mapped_column(default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="warning")
    silent_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notify_channels: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AlertIncident(Base):
    """告警事件（OBSERVABILITY_SPEC §10.2）：pending → firing → acknowledged → resolved。"""

    __tablename__ = "alert_incidents"
    __table_args__ = (
        Index("ix_incident_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("alert_rules.id"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")  # pending/firing/acknowledged/resolved
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="warning")
    metric: Mapped[str] = mapped_column(String(64))
    threshold: Mapped[float] = mapped_column(Float, default=0)
    current_value: Mapped[float] = mapped_column(Float, default=0)
    window_minutes: Mapped[int] = mapped_column(default=15)
    message: Mapped[str | None] = mapped_column(Text)
    related_trace_ids: Mapped[list | None] = mapped_column(JSON)
    acknowledged_by: Mapped[str | None] = mapped_column(String(64))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ObservabilitySavedView(Base):
    """用户保存的筛选与列配置（OBSERVABILITY_SPEC §9.3）。"""

    __tablename__ = "observability_saved_views"
    __table_args__ = (
        Index("ix_savedview_tenant_user", "tenant_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    view_type: Mapped[str] = mapped_column(String(32), default="traces")  # traces/calls/logs/costs
    filters: Mapped[dict | None] = mapped_column(JSON)
    columns: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
