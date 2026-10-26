"""Remember that a person chose a document's types, so reprocessing keeps them.

Revision ID: 0013_document_types_locked
Revises: 0012_version_rendition
Create Date: 2026-10-26
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_document_types_locked"
down_revision = "0012_version_rendition"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("types_locked", sa.Boolean(), nullable=False,
                                         server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("documents", "types_locked")
