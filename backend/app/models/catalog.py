"""Document types and their versioned, data-defined field schemas.

A Document Type is what users pick at upload ("this is an invoice"); it owns a
versioned tree of field definitions stored as JSONB (see services/fields.py).
Schemas are data, not code, so evolving them needs no redeployment — and every
extracted value records the schema version it was produced under.
"""
from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import TimestampMixin, uuid_pk


class DocumentType(Base, TimestampMixin):
    __tablename__ = "document_types"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_doctype_tenant_name"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(String(2048), default="")
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Seeded types (Contracts/Invoices) — editable, but flagged for the UI.
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    active_schema_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("type_schemas.id", ondelete="SET NULL"), nullable=True
    )

    schemas: Mapped[list["TypeSchema"]] = relationship(
        back_populates="document_type", foreign_keys="TypeSchema.document_type_id"
    )


class TypeSchema(Base, TimestampMixin):
    """One immutable version of a document type's field tree.

    `fields` is a list of recursive field-definition nodes:
        {key, name, description, data_type, required, threshold,
         fields: [...]   # when data_type == "object"
         item:   {...}   # when data_type == "list"}
    """

    __tablename__ = "type_schemas"
    __table_args__ = (UniqueConstraint("document_type_id", "version", name="uq_schema_type_version"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_type_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("document_types.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    fields: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    document_type: Mapped["DocumentType"] = relationship(
        back_populates="schemas", foreign_keys=[document_type_id]
    )
