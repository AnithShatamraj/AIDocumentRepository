"""Scope field_values' path uniqueness per document type.

A document can carry more than one Document Type at once. Each type's
extraction -- schema fields and its own discovery pass -- runs independently
over the same source text, so two types legitimately producing the same
field_path (a shared schema field name, or both discovery passes surfacing
e.g. "_discovered.corporate_title") is expected, not a collision. The
existing (document_id, document_version, field_path) constraint didn't allow
for that and raised a real UniqueViolation, which crashed the
metadata_extraction stage for any multi-type document whose types' output
overlapped.

Revision ID: 0003_field_value_path_per_type
Revises: 0002_widen_traceback_ref
Create Date: 2026-08-03
"""
from __future__ import annotations

from alembic import op

revision = "0003_field_value_path_per_type"
down_revision = "0002_widen_traceback_ref"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_field_value_path", "field_values", type_="unique")
    op.create_unique_constraint(
        "uq_field_value_path", "field_values",
        ["document_id", "document_version", "document_type_id", "field_path"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_field_value_path", "field_values", type_="unique")
    op.create_unique_constraint(
        "uq_field_value_path", "field_values",
        ["document_id", "document_version", "field_path"],
    )
