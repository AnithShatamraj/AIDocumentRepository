"""Audit log viewer (admin)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.core.db import get_db
from app.models.ops import AuditLog
from app.models.tenant import User

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("")
async def list_audit(
    event_type: str | None = Query(default=None),
    object_id: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(AuditLog).where(AuditLog.tenant_id == admin.tenant_id)
    if event_type:
        stmt = stmt.where(AuditLog.event_type == event_type)
    if object_id:
        stmt = stmt.where(AuditLog.object_id == object_id)
    stmt = stmt.order_by(AuditLog.created_at.desc()).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [{"id": str(a.id), "event_type": a.event_type, "actor_id": str(a.actor_id) if a.actor_id else None,
             "object_type": a.object_type, "object_id": a.object_id, "detail": a.detail,
             "ip_address": a.ip_address, "created_at": a.created_at.isoformat()} for a in rows]
