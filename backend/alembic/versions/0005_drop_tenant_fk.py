"""Drop the tenants table and its FK constraints from the tenant-schema chain.

Tenant now lives only in the management database (models/mgmt.py) -- each
tenant's own database no longer needs a local tenants table, and tenant_id
columns are now plain UUIDs (see models/tenant.py and the other tenant-schema
model files) rather than foreign keys, since the referenced table would live
in a different physical database.

This only matters for tenant databases provisioned BEFORE this change
landed: provision_tenant's create_all()+stamp("head") already builds new
tenant databases straight from the current model shape, with no local
tenants table at all, so this migration is a no-op there.

Revision ID: 0005_drop_tenant_fk
Revises: 0004_pgvector_indexes
Create Date: 2026-08-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_drop_tenant_fk"
down_revision = "0004_pgvector_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "tenants" not in inspector.get_table_names():
        return  # provisioned post-cutover -- nothing to drop

    for table_name in inspector.get_table_names():
        if table_name in ("tenants", "alembic_version"):
            continue
        for fk in inspector.get_foreign_keys(table_name):
            if fk.get("referred_table") == "tenants" and fk.get("name"):
                op.drop_constraint(fk["name"], table_name, type_="foreignkey")

    op.drop_table("tenants")


def downgrade() -> None:
    raise NotImplementedError(
        "downgrade not supported -- the tenants table's prior contents and FK "
        "constraint names aren't recoverable from this migration; restore from backup"
    )
