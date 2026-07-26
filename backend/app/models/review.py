"""Review queue items (low-confidence extractions & classifications)."""
from __future__ import annotations

import uuid

from sqlalchemy import DateTime, Float, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import TimestampMixin, uuid_pk
from app.models.constants import RV_PENDING


class ReviewItem(Base, TimestampMixin):
    __tablename__ = "review_items"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)  # extraction | classification
    # Target row (extraction id or a classification context).
    extraction_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("extractions.id", ondelete="CASCADE"), nullable=True
    )

    field_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    status: Mapped[str] = mapped_column(String(32), default=RV_PENDING, nullable=False, index=True)

    # Optimistic claim/assignment.
    claimed_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    claimed_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)

    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Structured (input, prediction, correction) for the future learning loop.
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
