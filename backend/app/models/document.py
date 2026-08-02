"""Documents, immutable versions, document-type assignments, and per-document ACLs."""
from __future__ import annotations

import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import TimestampMixin, uuid_pk
from app.models.constants import DOC_PENDING

# Many-to-many document <-> document type (multi-label classification).
document_type_links = Table(
    "document_type_links",
    Base.metadata,
    Column("document_id", PGUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True),
    Column("document_type_id", PGUUID(as_uuid=True), ForeignKey("document_types.id", ondelete="CASCADE"), primary_key=True),
)


class Document(Base, TimestampMixin):
    __tablename__ = "documents"
    __table_args__ = (
        # Names are unique per tenant among live documents; deleted ones release
        # their name (partial index, so soft-deleted rows don't block reuse).
        Index("uq_document_tenant_name_live", "tenant_id", "name",
              unique=True, postgresql_where=text("is_deleted = false")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    file_type: Mapped[str] = mapped_column(String(64), default="")  # extension / short type
    mime_type: Mapped[str] = mapped_column(String(128), default="")
    file_size: Mapped[int] = mapped_column(BigInteger, default=0)
    content_hash: Mapped[str] = mapped_column(String(64), default="", index=True)  # SHA-256

    current_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    processing_status: Mapped[str] = mapped_column(String(32), default=DOC_PENDING, nullable=False, index=True)

    # Set when this document's content matched an existing one. Derived artifacts
    # are cloned from the original instead of re-running the (paid) pipeline.
    duplicate_of_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    # Type chosen by the uploader; null means "auto-detect".
    requested_type_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("document_types.id", ondelete="SET NULL"), nullable=True
    )

    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    deleted_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Full-text search vector (Postgres tsvector) — populated by the pipeline.
    # Stored as a generated/maintained column via trigger in init-db.

    versions: Mapped[list["DocumentVersion"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", order_by="DocumentVersion.version"
    )
    document_types: Mapped[list["DocumentType"]] = relationship(
        "DocumentType", secondary=document_type_links)
    permissions: Mapped[list["DocumentPermission"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class DocumentVersion(Base, TimestampMixin):
    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("document_id", "version", name="uq_docversion"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)  # object storage key
    file_size: Mapped[int] = mapped_column(BigInteger, default=0)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    mime_type: Mapped[str] = mapped_column(String(128), default="")

    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # Extracted plain text + structured document understanding (pages, bboxes).
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    understanding: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    document: Mapped["Document"] = relationship(back_populates="versions")


class DocumentPermission(Base, TimestampMixin):
    """A single grant: (document, principal[user|group], level).

    ACLs are resolved at query time by joining chunks -> document -> here,
    filtered by the caller's user id + group ids. Never denormalized into chunks.
    """

    __tablename__ = "document_permissions"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "user_id", "group_id", "level", name="uq_docperm"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Exactly one of user_id / group_id is set.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), nullable=True, index=True
    )
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    granted_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    document: Mapped["Document"] = relationship(back_populates="permissions")


class DocumentTypeDefaultPermission(Base, TimestampMixin):
    """New documents of a type inherit these grants (e.g. Invoices -> Finance)."""

    __tablename__ = "document_type_default_permissions"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_type_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("document_types.id", ondelete="CASCADE"), nullable=False, index=True
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), nullable=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    level: Mapped[str] = mapped_column(String(16), nullable=False)
