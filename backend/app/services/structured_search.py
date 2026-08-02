"""Structured search over normalized typed extracted values (permission-aware)."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.constants import RV_AUTO_ACCEPTED, RV_CORRECTED, RV_VERIFIED
from app.models.content import FieldValue
from app.models.document import Document
from app.models.tenant import User
from app.services import permissions

_VERIFIED_STATES = (RV_VERIFIED, RV_CORRECTED, RV_AUTO_ACCEPTED)


async def query(
    db: AsyncSession,
    user: User,
    *,
    field_name: str | None = None,
    op: str = "eq",  # eq | contains | gt | lt | gte | lte | before | after | between
    value: str | None = None,
    value2: str | None = None,
    document_type_id=None,
    verified_only: bool = False,
    limit: int = 100,
) -> list[dict]:
    group_ids = await permissions.user_group_ids(db, user.id)
    cond = permissions.readable_documents_condition(user, group_ids)

    filters = [cond]
    if field_name:
        filters.append(FieldValue.field_name == field_name)
    if document_type_id:
        filters.append(FieldValue.document_type_id == document_type_id)
    if verified_only:
        filters.append(FieldValue.review_status.in_(_VERIFIED_STATES))
    filters.append(FieldValue.document_version == Document.current_version)

    if value is not None and op:
        filters.append(_value_filter(op, value, value2))

    stmt = (
        select(FieldValue, Document.name)
        .join(Document, Document.id == FieldValue.document_id)
        .where(and_(*filters))
        .order_by(FieldValue.document_id)
        .limit(limit)
    )
    rows = await db.execute(stmt)
    out = []
    for ext, doc_name in rows.all():
        out.append({
            "document_id": str(ext.document_id), "document_name": doc_name,
            "field_name": ext.field_name, "raw_value": ext.raw_value, "value_type": ext.value_type,
            "value_text": ext.value_text, "value_number": ext.value_number,
            "value_date": ext.value_date.isoformat() if ext.value_date else None,
            "value_currency": ext.value_currency, "confidence": ext.confidence,
            "review_status": ext.review_status, "source_page": ext.source_page,
        })
    return out


def _value_filter(op: str, value: str, value2: str | None):
    # Try to interpret value as date/number; fall back to text.
    d = _try_date(value)
    n = _try_number(value)
    if op in ("before",) and d:
        return FieldValue.value_date < d
    if op in ("after",) and d:
        return FieldValue.value_date > d
    if op == "between" and d and value2 and _try_date(value2):
        return and_(FieldValue.value_date >= d, FieldValue.value_date <= _try_date(value2))
    if op in ("gt",) and n is not None:
        return FieldValue.value_number > n
    if op in ("gte",) and n is not None:
        return FieldValue.value_number >= n
    if op in ("lt",) and n is not None:
        return FieldValue.value_number < n
    if op in ("lte",) and n is not None:
        return FieldValue.value_number <= n
    if op == "eq" and n is not None:
        return FieldValue.value_number == n
    if op == "contains":
        return FieldValue.value_text.ilike(f"%{value}%")
    return FieldValue.value_text.ilike(f"%{value}%")


def _try_date(s: str) -> dt.date | None:
    from app.services.normalize import _parse_date

    return _parse_date(s)


def _try_number(s: str) -> float | None:
    from app.services.normalize import _parse_number

    n, _ = _parse_number(s)
    return n
