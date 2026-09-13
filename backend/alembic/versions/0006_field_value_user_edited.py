"""Add field_values.is_user_edited -- durable flag for a human-typed
correction, distinct from review_status (which a later reprocess could reset).

Revision ID: 0006_field_value_user_edited
Revises: 0005_drop_tenant_fk
Create Date: 2026-09-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_field_value_user_edited"
down_revision = "0005_drop_tenant_fk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "field_values",
        sa.Column("is_user_edited", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("field_values", "is_user_edited")
