"""Categories and their (versioned, data-defined) extraction schemas."""
from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import TimestampMixin, uuid_pk


class Category(Base, TimestampMixin):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_category_tenant_name"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(String(2048), default="")
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Bound extraction schema (data, not code) — see Section 3/9 of the spec.
    active_schema_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("extraction_schemas.id", ondelete="SET NULL"), nullable=True
    )

    schemas: Mapped[list["ExtractionSchema"]] = relationship(
        back_populates="category", foreign_keys="ExtractionSchema.category_id"
    )


class ExtractionSchema(Base, TimestampMixin):
    """A versioned set of field definitions bound to a category.

    `fields` is a JSON list of {name, type, description, required}. Structured
    search queries the normalized typed value produced under a given schema
    version, so we keep the version on every extracted value.
    """

    __tablename__ = "extraction_schemas"
    __table_args__ = (UniqueConstraint("category_id", "version", name="uq_schema_cat_version"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("categories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    fields: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    category: Mapped["Category"] = relationship(
        back_populates="schemas", foreign_keys=[category_id]
    )
