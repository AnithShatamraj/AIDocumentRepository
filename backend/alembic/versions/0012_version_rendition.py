"""Keep a PDF rendition beside Word/PowerPoint/image originals.

Revision ID: 0012_version_rendition
Revises: 0011_draft_edits_type
Create Date: 2026-10-26
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_version_rendition"
down_revision = "0011_draft_edits_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("document_versions", sa.Column("rendition_key", sa.String(1024), nullable=True))


def downgrade() -> None:
    op.drop_column("document_versions", "rendition_key")
