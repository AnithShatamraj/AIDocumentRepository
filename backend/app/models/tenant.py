"""Tenant, User, Group models."""
from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, String, Table, Column, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import TimestampMixin, uuid_pk
from app.models.constants import ROLE_VIEWER

# Many-to-many user <-> group.
user_group = Table(
    "user_group",
    Base.metadata,
    Column("user_id", PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", PGUUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
)


class Tenant(Base, TimestampMixin):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    users: Mapped[list["User"]] = relationship(back_populates="tenant")
    groups: Mapped[list["Group"]] = relationship(back_populates="tenant")


class User(Base, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_user_tenant_email"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(255), default="")
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), default=ROLE_VIEWER, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    tenant: Mapped["Tenant"] = relationship(back_populates="users")
    groups: Mapped[list["Group"]] = relationship(secondary=user_group, back_populates="members")


class Group(Base, TimestampMixin):
    __tablename__ = "groups"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_group_tenant_name"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(String(1024), default="")

    tenant: Mapped["Tenant"] = relationship(back_populates="groups")
    members: Mapped[list["User"]] = relationship(secondary=user_group, back_populates="groups")
