"""User-applied tags: shared, flat, case-insensitively unique per tenant.

Revision ID: 0009_tags
Revises: 0008_draft_sample_check
Create Date: 2026-09-24
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_tags"
down_revision = "0008_draft_sample_check"
branch_labels = None
depends_on = None


def _ts() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "tags",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        *_ts(),
    )
    op.create_index("ix_tags_tenant_id", "tags", ["tenant_id"])
    # Case-insensitive uniqueness per tenant -- "Urgent"/"urgent"/"URGENT" must
    # be one tag, not three. Functional index, so the expression is raw SQL.
    op.create_index(
        "uq_tag_tenant_name_lower", "tags",
        ["tenant_id", sa.text("lower(name)")], unique=True,
    )

    op.create_table(
        "document_tags",
        sa.Column("document_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("tag_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
    )
    # (document_id, tag_id) is the primary key, which covers document -> tags.
    # This covers tag -> documents, which is what facet counts and tag filters
    # actually scan.
    op.create_index("ix_document_tags_tag", "document_tags", ["tag_id"])


def downgrade() -> None:
    op.drop_index("ix_document_tags_tag", table_name="document_tags")
    op.drop_table("document_tags")
    op.drop_index("uq_tag_tenant_name_lower", table_name="tags")
    op.drop_index("ix_tags_tenant_id", table_name="tags")
    op.drop_table("tags")
