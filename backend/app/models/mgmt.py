"""Tenant registry -- the only table in the management database.

Moved out of `models/tenant.py` (which stays as the tenant-schema `User`/
`Group` models): once each tenant's data lives in its own physical database,
a `tenants` table can no longer be a foreign-key target from tables that live
in a *different* database. This is the one place that knows how to reach a
given tenant's database and storage container.
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.mgmt_db import MgmtBase
from app.models.base import TimestampMixin, uuid_pk

PROVISIONING_PENDING = "pending"
PROVISIONING_READY = "ready"
PROVISIONING_FAILED = "failed"


class Tenant(MgmtBase, TimestampMixin):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # --- tenant's own dedicated Postgres database ---
    db_host: Mapped[str] = mapped_column(String(255), nullable=False)
    db_port: Mapped[int] = mapped_column(Integer, default=5432, nullable=False)
    db_name: Mapped[str] = mapped_column(String(63), nullable=False)  # Postgres identifier limit
    db_role: Mapped[str] = mapped_column(String(63), nullable=False)
    # Fernet ciphertext (app.core.crypto) -- never stored in plaintext.
    db_password_encrypted: Mapped[str] = mapped_column(String(512), nullable=False)
    db_sslmode: Mapped[str] = mapped_column(String(32), default="prefer", nullable=False)

    # --- tenant's own dedicated storage bucket/container ---
    storage_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_container: Mapped[str] = mapped_column(String(255), nullable=False)

    provisioning_status: Mapped[str] = mapped_column(
        String(32), default=PROVISIONING_PENDING, nullable=False
    )
    provisioned_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
