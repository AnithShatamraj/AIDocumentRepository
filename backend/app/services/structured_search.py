"""Structured search over normalized typed extracted values (permission-aware).

Searches by `field_key` (a field's stable identity — matches every occurrence
regardless of nesting or list position, e.g. every `work_experience[].organization`
across every document) or by an explicit `field_path` pattern where `[]` stands
for "any index" (`parties[].name`). Both compile to a flat, indexed query —
never a recursive CTE — because `field_path` is materialized on every row.

Only `node_kind == "scalar"` rows carry a value, so results are always leaves;
when a match lives inside a list item or object, its sibling fields are
attached as `context` (e.g. show Designation + Start Date next to a matched
Organization) so a hit is legible without opening the document.
"""
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


def _path_like(pattern: str) -> str:
    """'work_experience[].organization' -> SQL LIKE 'work_experience[%].organization'.

    Existing SQL wildcards in the pattern are escaped first so a field name
    that happens to contain `%` or `_` can't be misread as a wildcard.
    """
    escaped = pattern.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    return escaped.replace("[]", "[%]")


async def query(
    db: AsyncSession,
    user: User,
    *,
    field_key: str | None = None,
    field_path: str | None = None,  # e.g. "parties[].name" — [] = any list index
    field_name: str | None = None,  # deprecated: pre-nesting alias for field_key
    op: str = "eq",
    value: str | None = None,
    value2: str | None = None,
    document_type_id=None,
    verified_only: bool = False,
    limit: int = 100,
) -> list[dict]:
    group_ids = await permissions.user_group_ids(db, user.id)
    cond = permissions.readable_documents_condition(user, group_ids)

    key = field_key or field_name
    filters = [cond, FieldValue.node_kind == "scalar"]
    if key:
        filters.append(FieldValue.field_key == key)
    if field_path:
        filters.append(FieldValue.field_path.like(_path_like(field_path), escape="\\"))
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
        .order_by(FieldValue.document_id, FieldValue.field_path)
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()

    # A match inside a list item / object is easier to read with its siblings
    # (e.g. the rest of that work-experience entry) — one batched query, not N+1.
    parent_ids = {fv.parent_id for fv, _ in rows if fv.parent_id}
    context_by_parent: dict = {}
    if parent_ids:
        siblings = (await db.execute(
            select(FieldValue).where(
                FieldValue.parent_id.in_(parent_ids), FieldValue.node_kind == "scalar")
        )).scalars().all()
        for s in siblings:
            context_by_parent.setdefault(s.parent_id, {})[s.field_key] = s.raw_value

    out = []
    for fv, doc_name in rows:
        out.append({
            "document_id": str(fv.document_id), "document_name": doc_name,
            "field_key": fv.field_key, "field_path": fv.field_path, "field_name": fv.field_name,
            "raw_value": fv.raw_value, "value_type": fv.value_type,
            "value_text": fv.value_text, "value_number": fv.value_number,
            "value_date": fv.value_date.isoformat() if fv.value_date else None,
            "value_currency": fv.value_currency, "confidence": fv.confidence,
            "review_status": fv.review_status, "source_page": fv.source_page,
            "context": context_by_parent.get(fv.parent_id) or None,
        })
    return out


def _value_filter(op: str, value: str, value2: str | None):
    # Date parsing is a strict whole-string match; number parsing is a loose
    # substring regex. Check date FIRST, or a date like "2026-03-15" gets
    # misread as the number 2026 and every op after "eq" silently does the
    # wrong comparison.
    d = _try_date(value)
    n = None if d is not None else _try_number(value)

    if d is not None:
        if op == "eq":
            return FieldValue.value_date == d
        if op in ("before", "lt"):
            return FieldValue.value_date < d
        if op in ("after", "gt"):
            return FieldValue.value_date > d
        if op == "lte":
            return FieldValue.value_date <= d
        if op == "gte":
            return FieldValue.value_date >= d
        if op == "between" and value2:
            d2 = _try_date(value2)
            if d2:
                lo, hi = (d, d2) if d <= d2 else (d2, d)
                return and_(FieldValue.value_date >= lo, FieldValue.value_date <= hi)

    if n is not None:
        if op == "eq":
            return FieldValue.value_number == n
        if op == "gt":
            return FieldValue.value_number > n
        if op == "gte":
            return FieldValue.value_number >= n
        if op == "lt":
            return FieldValue.value_number < n
        if op == "lte":
            return FieldValue.value_number <= n

    return FieldValue.value_text.ilike(f"%{value}%")


def _try_date(s: str) -> dt.date | None:
    from app.services.normalize import _parse_date

    return _parse_date(s)


def _try_number(s: str) -> float | None:
    from app.services.normalize import _parse_number

    n, _ = _parse_number(s)
    return n
