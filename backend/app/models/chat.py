"""Research Assistant conversations, messages, and Q&A audit log."""
from __future__ import annotations

import uuid

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import TimestampMixin, uuid_pk


class Conversation(Base, TimestampMixin):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(512), default="New conversation")

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="Message.created_at"
    )


class Message(Base, TimestampMixin):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | assistant
    content: Mapped[str] = mapped_column(Text, default="")
    # Citations: list of {index, chunk_id, document_id, document_name, page,
    # anchor, section_title, bbox, snippet}
    citations: Mapped[list] = mapped_column(JSONB, default=list)
    # {coverage: {...}, check: {...}} — the caveats the answer was produced
    # with. Stored on the message so reopening a conversation shows the same
    # hedges, instead of a bare answer that reads more confident than it was.
    meta: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False, server_default="{}")

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")


class QAAuditLog(Base, TimestampMixin):
    """Question, retrieved chunk ids, answer, model/prompt version — for
    debugging groundedness complaints and security review."""

    __tablename__ = "qa_audit_logs"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    # What retrieval actually embedded. On a follow-up this differs from the
    # question, and a groundedness complaint is unanswerable without it.
    standalone_question: Mapped[str | None] = mapped_column(Text, nullable=True)
    retrieved_chunk_ids: Mapped[list] = mapped_column(JSONB, default=list)
    answer: Mapped[str] = mapped_column(Text, default="")
    refused: Mapped[bool] = mapped_column(default=False)
    coverage: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False, server_default="{}")
    unsupported_claims: Mapped[int] = mapped_column(Integer, default=0, nullable=False,
                                                    server_default="0")
    model_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tokens: Mapped[int] = mapped_column(Integer, default=0)

    # Reader feedback. The audit log is already the row that records what was
    # asked, retrieved and answered, so a groundedness complaint belongs on it
    # rather than in a table that has to be joined back.
    feedback: Mapped[str | None] = mapped_column(String(8), nullable=True)  # up | down
    feedback_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    feedback_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
