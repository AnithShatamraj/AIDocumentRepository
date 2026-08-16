"""Widen pipeline_stages.traceback_ref from varchar(255) to text.

The stage failure handler in worker/tasks.py writes up to 1500 chars into
this column. Any traceback over 255 chars (i.e. almost all of them) blew the
column limit and raised a second, unhandled exception *inside* the failure
handler itself -- so the stage's status update to 'failed' never committed
and the row stayed stuck at 'running' forever. Widening the column removes
the trigger; the handler is also made defensive in code so this class of bug
can't wedge a stage again regardless of column width.

Revision ID: 0002_widen_traceback_ref
Revises: 0001_doctypes
Create Date: 2026-08-03
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_widen_traceback_ref"
down_revision = "0001_doctypes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "pipeline_stages", "traceback_ref",
        existing_type=sa.String(255), type_=sa.Text(), existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "pipeline_stages", "traceback_ref",
        existing_type=sa.Text(), type_=sa.String(255), existing_nullable=True,
    )
