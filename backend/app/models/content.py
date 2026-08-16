"""Derived content: chunks, embeddings, summaries, classifications, field values.

Embeddings live in a dedicated table (not widening `chunks`) to avoid TOAST
churn, per the pgvector implementation note in the spec.
"""
from __future__ import annotations

import datetime as dt
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import settings
from app.core.db import Base
from app.models.base import TimestampMixin, uuid_pk
from app.models.constants import RV_PENDING, VT_TEXT


class Chunk(Base, TimestampMixin):
    __tablename__ = "chunks"
    __table_args__ = (UniqueConstraint("document_id", "document_version", "ordinal", name="uq_chunk_pos"),)

    # Deterministic id (uuid5 of doc+version+ordinal+hash) set by the pipeline.
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Provenance / anchor (page, section, slide, sheet+range) + bbox when available.
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    anchor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    section_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    bbox: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    embedding: Mapped["Embedding"] = relationship(
        back_populates="chunk", uselist=False, cascade="all, delete-orphan"
    )


class Embedding(Base, TimestampMixin):
    __tablename__ = "embeddings"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("chunks.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    vector: Mapped[list[float]] = mapped_column(Vector(settings.embedding_dim), nullable=False)
    # Model pinning — a model change requires a detectable full re-embed.
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False)
    embedding_dim: Mapped[int] = mapped_column(Integer, nullable=False)

    chunk: Mapped["Chunk"] = relationship(back_populates="embedding")


class Summary(Base, TimestampMixin):
    __tablename__ = "summaries"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    executive_summary: Mapped[str] = mapped_column(Text, default="")
    highlights: Mapped[list] = mapped_column(JSONB, default=list)
    model_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(128), nullable=True)


class Classification(Base, TimestampMixin):
    """One row per predicted (or human-assigned) label."""

    __tablename__ = "classifications"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_type_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("document_types.id", ondelete="SET NULL"), nullable=True
    )
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    is_override: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(16), default="ai")  # ai | human
    model_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(128), nullable=True)


class FieldValue(Base, TimestampMixin):
    """One node of a document's extracted data tree.

    Values form a tree so document types can define objects and lists (e.g.
    `work_experience[2].organization`). Two addressing schemes are kept on
    purpose:

      * `parent_id` + `ordinal` — cheap recursive rendering and grouping, so all
        of one list item's fields hang off a single container row;
      * `field_path` — lets structured search stay a flat, indexed query
        (`field_path LIKE 'parties[%].name'`) instead of a recursive CTE.

    Leaves keep the typed columns, so each value is stored twice: `raw_value`
    (what a human verifies) and a normalized typed value (what search queries).
    Container rows (`node_kind` in object/list) hold no value themselves but
    carry provenance for the block and let a reviewer accept a whole item.
    """

    __tablename__ = "field_values"
    __table_args__ = (
        # A document can carry more than one Document Type at once (e.g. a
        # rent agreement tagged both "ResidentialRentAgreement-India" and the
        # generic "Contracts"); each type's extraction -- schema fields and
        # its own discovery pass -- runs independently over the same text and
        # must not collide just because both happen to produce the same
        # field_path (a shared schema field name, or two discovery passes
        # both surfacing e.g. "_discovered.corporate_title").
        UniqueConstraint("document_id", "document_version", "document_type_id", "field_path",
                         name="uq_field_value_path"),
        Index("ix_field_values_key", "tenant_id", "field_key"),
        Index("ix_field_values_path", "field_path"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    document_type_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("document_types.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # --- position in the tree ---
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("field_values.id", ondelete="CASCADE"), nullable=True, index=True
    )
    field_key: Mapped[str] = mapped_column(String(128), nullable=False)
    field_path: Mapped[str] = mapped_column(String(512), nullable=False)
    field_name: Mapped[str] = mapped_column(String(255), nullable=False)  # display label
    node_kind: Mapped[str] = mapped_column(String(16), default="scalar", nullable=False)
    ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- the value, stored twice (raw + normalized/typed) ---
    data_type: Mapped[str] = mapped_column(String(16), default=VT_TEXT)
    raw_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_type: Mapped[str] = mapped_column(String(16), default=VT_TEXT)
    value_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_number: Mapped[float | None] = mapped_column(Float, nullable=True)
    value_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    value_datetime: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    value_time: Mapped[dt.time | None] = mapped_column(Time, nullable=True)
    value_boolean: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    value_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)

    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_anchor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_bbox: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    review_status: Mapped[str] = mapped_column(String(32), default=RV_PENDING, nullable=False, index=True)
    model_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    schema_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Found by the discovery pass rather than defined in the type's schema.
    is_discovered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # No ORM self-relationship on purpose: the tree is assembled from a single
    # flat SELECT (cheaper than lazy loads), and Postgres ON DELETE CASCADE on
    # parent_id already removes descendants.
