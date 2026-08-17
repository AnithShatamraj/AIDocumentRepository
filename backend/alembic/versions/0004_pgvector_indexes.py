"""pgvector HNSW + full-text GIN indexes as a real migration.

Previously these were created via raw SQL in cli.py::init_db() (bootstrap-
only, never replayed by `alembic upgrade head`). Per-tenant database
provisioning needs a single code path -- `alembic upgrade head` -- to fully
stand up a new tenant's schema, so the index creation moves here.

Revision ID: 0004_pgvector_indexes
Revises: 0003_field_value_path_per_type
Create Date: 2026-08-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_pgvector_indexes"
down_revision = "0003_field_value_path_per_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_index(
        "ix_embeddings_hnsw", "embeddings", ["vector"],
        unique=False, postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"vector": "vector_cosine_ops"},
        if_not_exists=True,
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chunks_fts ON chunks "
        "USING gin (to_tsvector('english', content))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_fts")
    op.drop_index("ix_embeddings_hnsw", table_name="embeddings", if_exists=True)
