"""Draft sample-consistency check: do the uploaded samples look like one kind of document?

Revision ID: 0008_draft_sample_check
Revises: 0007_document_type_drafts
Create Date: 2026-09-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_draft_sample_check"
down_revision = "0007_document_type_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_type_drafts",
        sa.Column("sample_check", postgresql.JSONB(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("document_type_drafts", "sample_check")
