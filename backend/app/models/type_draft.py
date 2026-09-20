"""AI-assisted document-type authoring: a draft is one guided chat session.

A draft carries the whole conversation plus the work-in-progress type (name,
description, field tree) so an admin can leave and resume, and only creates a
real DocumentType when they publish. `status` is deliberately a plain string:
an approval step (submitted -> approved) can slot in between `draft` and
`published` later without a schema change.
"""
from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, Boolean, ForeignKey, Identity, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import TimestampMixin, uuid_pk

# --- draft.status ---
DRAFT_ACTIVE = "draft"
DRAFT_PUBLISHED = "published"

# --- draft.stage: the guided flow, in order ---
STAGE_DESCRIBE = "describe"   # collecting / refining the description
STAGE_NAME = "name"           # description confirmed; choosing the name
STAGE_SAMPLES = "samples"     # name confirmed; offering sample documents
STAGE_FIELDS = "fields"       # deriving / refining / validating the field tree

# --- sample.status ---
SAMPLE_PROCESSING = "processing"
SAMPLE_READY = "ready"
SAMPLE_FAILED = "failed"


class DocumentTypeDraft(Base, TimestampMixin):
    __tablename__ = "document_type_drafts"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    status: Mapped[str] = mapped_column(String(16), default=DRAFT_ACTIVE, nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(16), default=STAGE_DESCRIBE, nullable=False)

    name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    name_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    description_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Work-in-progress field tree in the same shape as TypeSchema.fields, but
    # NOT validated on save -- a half-edited tree (blank names, an empty
    # group) is a normal state while authoring. Validated on validate/publish.
    fields: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # Latest "test extraction on a sample" results, keyed by sample id.
    last_validation: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # Does the uploaded sample set look like ONE kind of document? Computed once
    # per distinct set of ready samples (`key`), and cleared of its `confirmed`
    # flag whenever the set changes. Shape: {key, related, summary, confirmed,
    # documents: [{sample_id, filename, kind, group, outlier}]}.
    sample_check: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    published_type_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("document_types.id", ondelete="SET NULL"), nullable=True
    )


class DocumentTypeDraftMessage(Base, TimestampMixin):
    __tablename__ = "document_type_draft_messages"

    id: Mapped[uuid.UUID] = uuid_pk()
    draft_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("document_type_drafts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # created_at can't order a user turn and its reply (both are written in one
    # transaction, so Postgres stamps them with the same now()); a server-side
    # identity gives a strict order.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | assistant
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # UI hints: {kind, actions:[{action,label}], description, name, ...}
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class DocumentTypeDraftSample(Base, TimestampMixin):
    """A sample document. Only extracted text is kept (not the file): the AI
    needs the text to derive and test fields, and nothing else reads the file,
    so there's no storage object to orphan when a draft is discarded."""

    __tablename__ = "document_type_draft_samples"

    id: Mapped[uuid.UUID] = uuid_pk()
    draft_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("document_type_drafts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    file_type: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=SAMPLE_PROCESSING, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
