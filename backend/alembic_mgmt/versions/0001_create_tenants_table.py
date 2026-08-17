"""Create the tenants table -- the tenant registry for the management database.

Revision ID: 0001_mgmt_init
Revises:
Create Date: 2026-08-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_mgmt_init"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("db_host", sa.String(255), nullable=False),
        sa.Column("db_port", sa.Integer(), nullable=False, server_default="5432"),
        sa.Column("db_name", sa.String(63), nullable=False),
        sa.Column("db_role", sa.String(63), nullable=False),
        sa.Column("db_password_encrypted", sa.String(512), nullable=False),
        sa.Column("db_sslmode", sa.String(32), nullable=False, server_default="prefer"),
        sa.Column("storage_provider", sa.String(32), nullable=False),
        sa.Column("storage_container", sa.String(255), nullable=False),
        sa.Column("provisioning_status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("provisioned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_tenants_slug", "tenants", ["slug"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_tenants_slug", table_name="tenants")
    op.drop_table("tenants")
