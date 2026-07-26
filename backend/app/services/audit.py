"""Audit logging helper (async + sync)."""
from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ops import AuditLog


async def log_event(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    event_type: str,
    object_type: str | None = None,
    object_id: str | None = None,
    detail: dict | None = None,
    ip_address: str | None = None,
    commit: bool = True,
) -> None:
    db.add(AuditLog(
        tenant_id=tenant_id, actor_id=actor_id, event_type=event_type,
        object_type=object_type, object_id=str(object_id) if object_id else None,
        detail=detail, ip_address=ip_address,
    ))
    if commit:
        await db.commit()
