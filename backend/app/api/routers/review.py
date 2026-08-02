"""Review queue: human validation of low-confidence AI results."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_reviewer
from app.core.db import get_db
from app.models.constants import (
    AUDIT_REVIEW,
    DOC_PROCESSED,
    REVIEW_CLASSIFICATION,
    REVIEW_EXTRACTION,
    RV_CORRECTED,
    RV_PENDING,
    RV_REJECTED,
    RV_VERIFIED,
    STAGE_METADATA,
)
from app.models.content import Classification, FieldValue
from app.models.document import Document, document_type_links
from app.models.review import ReviewItem
from app.models.tenant import User
from app.schemas import ReviewOut, ReviewResolve
from app.services import audit, normalize, permissions

router = APIRouter(prefix="/review", tags=["review"])
CLAIM_TIMEOUT_MIN = 30


def _now():
    return dt.datetime.now(dt.timezone.utc)


async def _readable_item(db, user, review_id) -> ReviewItem:
    item = await db.get(ReviewItem, review_id)
    if not item or item.tenant_id != user.tenant_id:
        raise HTTPException(404, "Review item not found")
    if not await permissions.can_read(db, user, item.document_id):
        raise HTTPException(403, "No access to the underlying document")
    return item


@router.get("", response_model=list[ReviewOut])
async def list_queue(
    document_type_id: str | None = Query(default=None),
    field_name: str | None = Query(default=None),
    min_confidence: float | None = Query(default=None),
    sort: str = Query(default="age"),  # age | confidence
    user: User = Depends(require_reviewer),
    db: AsyncSession = Depends(get_db),
):
    group_ids = await permissions.user_group_ids(db, user.id)
    cond = permissions.readable_documents_condition(user, group_ids)
    stmt = (select(ReviewItem)
            .join(Document, Document.id == ReviewItem.document_id)
            .where(ReviewItem.status == RV_PENDING, cond))
    if field_name:
        stmt = stmt.where(ReviewItem.field_name == field_name)
    if min_confidence is not None:
        stmt = stmt.where(ReviewItem.confidence >= min_confidence)
    if document_type_id:
        stmt = stmt.where(and_(document_type_links.c.document_id == ReviewItem.document_id,
                               document_type_links.c.document_type_id == document_type_id))
    stmt = stmt.order_by(ReviewItem.confidence.asc() if sort == "confidence" else ReviewItem.created_at.asc()).limit(200)
    rows = (await db.execute(stmt)).scalars().all()
    return [ReviewOut.model_validate(r) for r in rows]


@router.post("/{review_id}/claim", response_model=ReviewOut)
async def claim(review_id: str, user: User = Depends(require_reviewer), db: AsyncSession = Depends(get_db)):
    item = await _readable_item(db, user, review_id)
    # Release stale claims first.
    if item.claimed_by and item.claimed_at and (_now() - item.claimed_at).total_seconds() > CLAIM_TIMEOUT_MIN * 60:
        item.claimed_by = None
    if item.claimed_by and item.claimed_by != user.id:
        raise HTTPException(409, "Item already claimed by another reviewer")
    item.claimed_by = user.id
    item.claimed_at = _now()
    await db.commit()
    return ReviewOut.model_validate(item)


@router.post("/{review_id}/resolve", response_model=ReviewOut)
async def resolve(review_id: str, body: ReviewResolve, user: User = Depends(require_reviewer), db: AsyncSession = Depends(get_db)):
    item = await _readable_item(db, user, review_id)
    if item.claimed_by and item.claimed_by != user.id:
        raise HTTPException(409, "Item claimed by another reviewer")
    if item.status != RV_PENDING:
        raise HTTPException(409, "Item already resolved")

    if item.kind == REVIEW_EXTRACTION and item.field_value_id:
        ext = await db.get(FieldValue, item.field_value_id)
        if not ext:
            raise HTTPException(404, "FieldValue not found")
        correction = None
        if body.action == "accept":
            ext.review_status = RV_VERIFIED
        elif body.action == "modify":
            ext.raw_value = body.corrected_value
            norm = normalize.normalize(body.corrected_value, ext.value_type)
            ext.value_text, ext.value_number = norm.value_text, norm.value_number
            ext.value_date, ext.value_currency = norm.value_date, norm.value_currency
            ext.review_status = RV_CORRECTED
            correction = body.corrected_value
        elif body.action == "reject":
            ext.review_status = RV_REJECTED
        else:
            raise HTTPException(400, "action must be accept|modify|reject")
        # Learning-loop feedstock: (input, prediction, correction).
        item.payload = {**(item.payload or {}), "prediction": item.payload.get("raw_value") if item.payload else None,
                        "correction": correction, "action": body.action}

    elif item.kind == REVIEW_CLASSIFICATION:
        if body.action in ("accept", "modify") and body.document_type_ids:
            await db.execute(document_type_links.delete().where(document_type_links.c.document_id == item.document_id))
            for cid in body.document_type_ids:
                await db.execute(document_type_links.insert().values(document_id=item.document_id, document_type_id=cid))
                db.add(Classification(tenant_id=user.tenant_id, document_id=item.document_id, document_type_id=cid,
                                      label="(assigned)", confidence=1.0, source="human", is_override=True))
            doc = await db.get(Document, item.document_id)
            doc.processing_status = DOC_PROCESSED
            await db.commit()
            # FieldValue was skipped for the uncategorized doc — run it now.
            from app.worker.tasks import start_processing
            start_processing.delay(str(item.document_id), STAGE_METADATA)
        elif body.action == "reject":
            pass
        else:
            raise HTTPException(400, "Provide document_type_ids to accept a classification")
    else:
        raise HTTPException(400, "Unsupported review item")

    item.status = RV_REJECTED if body.action == "reject" else RV_VERIFIED
    item.resolved_by = user.id
    item.resolved_at = _now()
    item.notes = body.notes
    await audit.log_event(db, tenant_id=user.tenant_id, actor_id=user.id, event_type=AUDIT_REVIEW,
                          object_type="review_item", object_id=item.id,
                          detail={"action": body.action, "kind": item.kind}, commit=False)
    await db.commit()
    await db.refresh(item)
    return ReviewOut.model_validate(item)
