"""In-app notifications."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_tenant_db
from app.models.ops import Notification
from app.models.tenant import User

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("")
async def list_notifications(unread_only: bool = False, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_tenant_db)):
    stmt = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        stmt = stmt.where(Notification.is_read.is_(False))
    stmt = stmt.order_by(Notification.created_at.desc()).limit(100)
    rows = (await db.execute(stmt)).scalars().all()
    unread = (await db.execute(
        select(func.count()).select_from(Notification).where(Notification.user_id == user.id, Notification.is_read.is_(False))
    )).scalar()
    return {"unread": unread, "items": [{"id": str(n.id), "kind": n.kind, "title": n.title, "body": n.body,
                                         "link": n.link, "is_read": n.is_read, "created_at": n.created_at.isoformat()} for n in rows]}


@router.post("/{notification_id}/read")
async def mark_read(notification_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_tenant_db)):
    n = await db.get(Notification, notification_id)
    if not n or n.user_id != user.id:
        raise HTTPException(404, "Notification not found")
    n.is_read = True
    await db.commit()
    return {"status": "ok"}


@router.post("/read-all")
async def mark_all_read(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_tenant_db)):
    rows = (await db.execute(select(Notification).where(Notification.user_id == user.id, Notification.is_read.is_(False)))).scalars().all()
    for n in rows:
        n.is_read = True
    await db.commit()
    return {"status": "ok", "updated": len(rows)}
