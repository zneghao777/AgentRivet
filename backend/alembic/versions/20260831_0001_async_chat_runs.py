"""create durable asynchronous chat runs

Revision ID: 20260831_0001
Revises:
Create Date: 2026-08-31
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260831_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("client_request_id", sa.String(length=64), nullable=False),
        sa.Column("user_message_id", sa.BigInteger(), nullable=True),
        sa.Column("assistant_message_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("request_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("last_event_sequence", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("attempt", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'pending_approval', "
            "'succeeded', 'failed', 'canceled')",
            name="ck_chat_run_status",
        ),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_message_id"], ["messages.id"]),
        sa.ForeignKeyConstraint(["assistant_message_id"], ["messages.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "user_id", "client_request_id", name="uq_chat_runs_client_request"
        ),
    )
    op.create_index("ix_chat_runs_request_id", "chat_runs", ["request_id"])
    op.create_index("ix_chat_runs_trace_id", "chat_runs", ["trace_id"])
    op.create_index(
        "ix_chat_runs_session_created", "chat_runs", ["session_id", sa.text("created_at DESC")]
    )
    op.create_index(
        "ix_chat_runs_tenant_user_status_created",
        "chat_runs",
        ["tenant_id", "user_id", "status", "created_at"],
    )
    op.create_index(
        "uq_chat_runs_session_active",
        "chat_runs",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running', 'pending_approval')"),
    )

    op.create_table(
        "chat_run_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["chat_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_chat_run_event_sequence"),
    )
    op.create_index(
        "ix_chat_run_events_run_sequence", "chat_run_events", ["run_id", "sequence"]
    )

    op.create_table(
        "chat_run_outbox",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("command_type", sa.String(length=32), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["chat_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id", "command_type", "message_id", name="uq_chat_run_outbox_message"
        ),
    )
    op.create_index(
        "ix_chat_run_outbox_unpublished",
        "chat_run_outbox",
        ["published_at", "created_at"],
    )
    op.add_column("messages", sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index("ix_messages_run_id", "messages", ["run_id"])
    op.create_foreign_key(
        "fk_messages_run_id", "messages", "chat_runs", ["run_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_messages_run_id", "messages", type_="foreignkey")
    op.drop_index("ix_messages_run_id", table_name="messages")
    op.drop_column("messages", "run_id")
    op.drop_table("chat_run_outbox")
    op.drop_table("chat_run_events")
    op.drop_table("chat_runs")
