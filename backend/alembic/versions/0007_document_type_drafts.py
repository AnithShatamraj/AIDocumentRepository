"""AI-assisted document type authoring: drafts, their chat messages, and samples.

Revision ID: 0007_document_type_drafts
Revises: 0006_field_value_user_edited
Create Date: 2026-09-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_document_type_drafts"
down_revision = "0006_field_value_user_edited"
branch_labels = None
depends_on = None


def _ts() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "document_type_drafts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("stage", sa.String(16), nullable=False, server_default="describe"),
        sa.Column("name", sa.String(255), nullable=False, server_default=""),
        sa.Column("name_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("description_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("fields", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("last_validation", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("published_type_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("document_types.id", ondelete="SET NULL")),
        *_ts(),
    )
    op.create_index("ix_document_type_drafts_tenant_id", "document_type_drafts", ["tenant_id"])
    op.create_index("ix_document_type_drafts_created_by", "document_type_drafts", ["created_by"])
    op.create_index("ix_document_type_drafts_status", "document_type_drafts", ["status"])

    op.create_table(
        "document_type_draft_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("draft_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("document_type_drafts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("payload", postgresql.JSONB()),
        *_ts(),
    )
    op.create_index("ix_document_type_draft_messages_draft_id", "document_type_draft_messages", ["draft_id"])

    op.create_table(
        "document_type_draft_samples",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("draft_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("document_type_drafts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("file_type", sa.String(16), nullable=False, server_default=""),
        sa.Column("file_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="processing"),
        sa.Column("error", sa.Text()),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("page_count", sa.Integer()),
        *_ts(),
    )
    op.create_index("ix_document_type_draft_samples_draft_id", "document_type_draft_samples", ["draft_id"])


def downgrade() -> None:
    op.drop_table("document_type_draft_samples")
    op.drop_table("document_type_draft_messages")
    op.drop_table("document_type_drafts")
