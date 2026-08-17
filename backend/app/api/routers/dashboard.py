"""Operational dashboard widgets."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends
from sqlalchemy import String, and_, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_tenant_db
from app.models.catalog import DocumentType
from app.models.constants import RV_AUTO_ACCEPTED
from app.models.content import FieldValue
from app.models.document import Document, document_type_links
from app.models.ops import CostRecord
from app.models.review import ReviewItem
from app.models.tenant import User
from app.services import permissions

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _now():
    return dt.datetime.now(dt.timezone.utc)


@router.get("")
async def dashboard(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_tenant_db)):
    group_ids = await permissions.user_group_ids(db, user.id)
    cond = permissions.readable_documents_condition(user, group_ids)

    total = (await db.execute(select(func.count()).select_from(Document).where(cond))).scalar()

    by_status_rows = (await db.execute(
        select(Document.processing_status, func.count()).where(cond).group_by(Document.processing_status)
    )).all()
    by_status = {s: c for s, c in by_status_rows}

    by_type_rows = (await db.execute(
        select(DocumentType.name, func.count(document_type_links.c.document_id))
        .join(document_type_links, document_type_links.c.document_type_id == DocumentType.id)
        .join(Document, Document.id == document_type_links.c.document_id)
        .where(cond).group_by(DocumentType.name)
    )).all()
    by_type = {name: c for name, c in by_type_rows}

    pending = (await db.execute(
        select(func.count()).select_from(ReviewItem)
        .join(Document, Document.id == ReviewItem.document_id)
        .where(ReviewItem.status == "pending_review", cond)
    )).scalar()
    oldest = (await db.execute(
        select(func.min(ReviewItem.created_at))
        .join(Document, Document.id == ReviewItem.document_id)
        .where(ReviewItem.status == "pending_review", cond)
    )).scalar()
    oldest_age_hours = round((_now() - oldest).total_seconds() / 3600, 1) if oldest else None

    recent_rows = (await db.execute(
        select(Document).where(cond).order_by(Document.created_at.desc()).limit(5)
    )).scalars().all()
    recent = [{"id": str(d.id), "name": d.name, "status": d.processing_status,
               "created_at": d.created_at.isoformat()} for d in recent_rows]

    autoaccept = {}
    for days in (7, 30):
        since = _now() - dt.timedelta(days=days)
        tot = (await db.execute(
            select(func.count()).select_from(FieldValue).where(FieldValue.tenant_id == user.tenant_id,
                                                               FieldValue.created_at >= since)
        )).scalar() or 0
        auto = (await db.execute(
            select(func.count()).select_from(FieldValue).where(
                FieldValue.tenant_id == user.tenant_id, FieldValue.created_at >= since,
                FieldValue.review_status == RV_AUTO_ACCEPTED)
        )).scalar() or 0
        autoaccept[f"{days}d"] = round(auto / tot, 3) if tot else None

    cost_ptd = (await db.execute(
        select(func.coalesce(func.sum(CostRecord.cost_usd), 0.0)).where(
            CostRecord.tenant_id == user.tenant_id,
            CostRecord.created_at >= _now().replace(day=1, hour=0, minute=0, second=0, microsecond=0))
    )).scalar()

    return {
        "total_documents": total,
        "documents_by_type": by_type,
        "processing_status": by_status,
        "pending_reviews": {"count": pending, "oldest_age_hours": oldest_age_hours},
        "recent_uploads": recent,
        "extraction_auto_accept_rate": autoaccept,
        "ai_cost_period_to_date_usd": round(float(cost_ptd or 0.0), 4),
    }
