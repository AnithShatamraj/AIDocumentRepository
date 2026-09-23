"""User-applied tags — the human organizing axis over documents.

Deliberately distinct from the two AI-driven labelling concepts:

- **Document Type** says *what a document is* and carries a field schema.
- **Classification** is the AI's confidence-scored prediction behind that type.
- **Tag** is how a person chooses to organize, carries no schema, and is never
  set by the pipeline.

Keeping them apart is what stops "why did tagging it change its fields?".

Tags are tenant-wide and shared (everyone sees the same taxonomy) and flat --
the repository/sub-category tree is a separate, hierarchical concept, and
nesting tags too would create a second competing taxonomy.

Relational rather than a JSONB array on `documents`, because rename, merge,
autocomplete and facet counts all want an indexed join; with JSONB a rename
becomes a mass update of every document row.
"""
from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, Table, func, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import TimestampMixin, uuid_pk

# Many-to-many document <-> tag. Both sides cascade at the database level, so
# deleting either end cleans up its links without the ORM touching them.
document_tags = Table(
    "document_tags",
    Base.metadata,
    Column("document_id", PGUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"),
           primary_key=True),
    Column("tag_id", PGUUID(as_uuid=True), ForeignKey("tags.id", ondelete="CASCADE"),
           primary_key=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    # Who applied it. Useful on a shared taxonomy -- "who tagged this" is a
    # fair question when the tag carries meaning for other people.
    Column("created_by", PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"),
           nullable=True),
    # The document_id half is the primary key's leading column; this covers the
    # reverse direction ("which documents carry this tag").
    Index("ix_document_tags_tag", "tag_id"),
)


class Tag(Base, TimestampMixin):
    __tablename__ = "tags"
    __table_args__ = (
        # Case-insensitive uniqueness per tenant. Without it "Urgent", "urgent"
        # and "URGENT" fragment the taxonomy within a week of shipping.
        Index("uq_tag_tenant_name_lower", "tenant_id", text("lower(name)"), unique=True),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False, index=True
    )
    # Stored with the creator's capitalization; compared case-insensitively.
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
