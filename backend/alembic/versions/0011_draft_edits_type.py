"""Let a draft edit an existing document type, not only create a new one.

Revision ID: 0011_draft_edits_type
Revises: 0010_answer_trust
Create Date: 2026-10-23
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011_draft_edits_type"
down_revision = "0010_answer_trust"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The type this draft edits (NULL = a draft that creates a new type). SET NULL
    # rather than CASCADE: deleting a type must not silently delete drafts, and a
    # draft whose type is gone is refused at publish with a clear message.
    op.add_column("document_type_drafts", sa.Column(
        "edits_type_id", postgresql.UUID(as_uuid=True),
        sa.ForeignKey("document_types.id", ondelete="SET NULL"), nullable=True))
    op.create_index("ix_document_type_drafts_edits_type_id", "document_type_drafts", ["edits_type_id"])
    # The schema version the draft was started from. If the type has moved on by
    # publish time (someone else edited it, or fields were added from a
    # document), saving would overwrite that work, so publish refuses instead.
    op.add_column("document_type_drafts", sa.Column("base_schema_version", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("document_type_drafts", "base_schema_version")
    op.drop_index("ix_document_type_drafts_edits_type_id", table_name="document_type_drafts")
    op.drop_column("document_type_drafts", "edits_type_id")
