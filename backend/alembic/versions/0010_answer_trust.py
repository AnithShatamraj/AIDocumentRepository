"""Answer trust: per-message caveats, the query retrieval actually used, and reader feedback.

Revision ID: 0010_answer_trust
Revises: 0009_tags
Create Date: 2026-09-26
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_answer_trust"
down_revision = "0009_tags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Coverage + citation-check results travel with the message, so reopening
    # a conversation shows the caveats the answer was given with.
    op.add_column("messages", sa.Column(
        "meta", postgresql.JSONB(), nullable=False, server_default="{}"))

    op.add_column("qa_audit_logs", sa.Column(
        "message_id", postgresql.UUID(as_uuid=True),
        sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True))
    op.create_index("ix_qa_audit_logs_message_id", "qa_audit_logs", ["message_id"])
    # On a follow-up, what retrieval embedded is not what the user typed, and a
    # groundedness complaint cannot be diagnosed without it.
    op.add_column("qa_audit_logs", sa.Column("standalone_question", sa.Text(), nullable=True))
    op.add_column("qa_audit_logs", sa.Column(
        "coverage", postgresql.JSONB(), nullable=False, server_default="{}"))
    op.add_column("qa_audit_logs", sa.Column(
        "unsupported_claims", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("qa_audit_logs", sa.Column("feedback", sa.String(8), nullable=True))
    op.add_column("qa_audit_logs", sa.Column("feedback_reason", sa.Text(), nullable=True))
    op.add_column("qa_audit_logs", sa.Column(
        "feedback_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for col in ("feedback_at", "feedback_reason", "feedback", "unsupported_claims",
                "coverage", "standalone_question"):
        op.drop_column("qa_audit_logs", col)
    op.drop_index("ix_qa_audit_logs_message_id", table_name="qa_audit_logs")
    op.drop_column("qa_audit_logs", "message_id")
    op.drop_column("messages", "meta")
