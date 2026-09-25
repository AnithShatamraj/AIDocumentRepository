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
import uuid
from dataclasses import dataclass

from sqlalchemy import and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.constants import RV_AUTO_ACCEPTED, RV_CORRECTED, RV_VERIFIED
from app.models.content import FieldValue
from app.models.document import Document, document_type_links
from app.models.tenant import User
from app.services import permissions
from app.services import tags as tag_service

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


@dataclass
class FieldCondition:
    """One structured predicate over an extracted field.

    `field_key` matches a leaf wherever it sits (any nesting, any list index);
    `field_path` pins an explicit pattern where `[]` means "any index".
    """

    field_key: str | None = None
    field_path: str | None = None
    op: str = "eq"
    value: str | None = None
    value2: str | None = None


class UnknownTag(LookupError):
    """A requested tag name doesn't exist, and the match mode needs them all."""


async def find_documents(
    db: AsyncSession,
    user: User,
    *,
    document_type_ids: list[uuid.UUID] | None = None,
    tags: list[str] | None = None,
    tags_match_all: bool = True,
    field_conditions: list[FieldCondition] | None = None,
    name_contains: str | None = None,
    status: str | None = None,
    created_after: dt.datetime | None = None,
    created_before: dt.datetime | None = None,
    verified_only: bool = False,
    limit: int = 200,
) -> list[dict]:
    """Find *documents* matching several structured criteria at once.

    `query()` above answers "which field values match this one predicate";
    this answers "which documents satisfy all of these", which is the shape
    scope-building and agent tool calls actually need. Every predicate is
    ANDed, and field predicates become correlated EXISTS subqueries so a
    document qualifies when *some* value of that field matches — the right
    reading for repeating fields inside lists.

    Raises `UnknownTag` when `tags_match_all` is set and a name doesn't
    resolve: no document can carry a tag that doesn't exist, and quietly
    dropping the name would return a wider set than was asked for.
    """
    group_ids = await permissions.user_group_ids(db, user.id)
    filters = [permissions.readable_documents_condition(user, group_ids)]

    if name_contains:
        filters.append(Document.name.ilike(f"%{name_contains}%"))
    if status:
        filters.append(Document.processing_status == status)
    if created_after:
        filters.append(Document.created_at >= created_after)
    if created_before:
        filters.append(Document.created_at <= created_before)

    if document_type_ids:
        filters.append(exists(
            select(document_type_links.c.document_id).where(
                document_type_links.c.document_id == Document.id,
                document_type_links.c.document_type_id.in_(document_type_ids),
            )
        ))

    if tags:
        resolved = await tag_service.resolve_names(db, user.tenant_id, tags)
        if tags_match_all and len(resolved) < len({t.lower() for t in tags}):
            missing = sorted({t for t in tags if t.lower() not in resolved})
            raise UnknownTag(f"No such tag: {', '.join(missing)}")
        if not resolved:
            # Tags were asked for and none exist. An empty id list reads as
            # "no tag filter" downstream, which would widen the result to the
            # whole corpus instead of narrowing it to nothing.
            return []
        filters.append(tag_service.documents_with_tags_condition(
            [t.id for t in resolved.values()], match_all=tags_match_all))

    for c in field_conditions or []:
        sub = [
            FieldValue.document_id == Document.id,
            FieldValue.document_version == Document.current_version,
            FieldValue.node_kind == "scalar",
        ]
        if c.field_key:
            sub.append(FieldValue.field_key == c.field_key)
        if c.field_path:
            sub.append(FieldValue.field_path.like(_path_like(c.field_path), escape="\\"))
        if verified_only:
            sub.append(FieldValue.review_status.in_(_VERIFIED_STATES))
        if c.value is not None and c.op:
            sub.append(_value_filter(c.op, c.value, c.value2))
        filters.append(exists(select(FieldValue.id).where(and_(*sub))))

    stmt = (
        select(Document)
        # document_types is a plain lazy relationship; without this it would
        # raise MissingGreenlet the moment we read it on an async session.
        .options(selectinload(Document.document_types))
        .where(and_(*filters))
        .order_by(Document.created_at.desc())
        .limit(limit)
    )
    docs = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(d.id),
            "name": d.name,
            "processing_status": d.processing_status,
            "file_type": d.file_type,
            "current_version": d.current_version,
            "created_at": d.created_at.isoformat(),
            "document_types": [{"id": str(t.id), "name": t.name} for t in d.document_types],
            "tags": [{"id": str(t.id), "name": t.name} for t in d.tags],
        }
        for d in docs
    ]


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

    if op == "eq":
        # Equality has to mean equality. This used to fall through to the
        # substring match below, so "city equals Mumbai" also returned
        # "Mumbai, MH" -- even though the search UI offers "equals" and
        # "contains" as separate operators. Case-insensitive, because the
        # stored casing is whatever the extractor produced.
        return func.lower(FieldValue.value_text) == value.strip().lower()

    return FieldValue.value_text.ilike(f"%{value}%")


def _try_date(s: str) -> dt.date | None:
    from app.services.normalize import _parse_date

    return _parse_date(s)


def _try_number(s: str) -> float | None:
    from app.services.normalize import _parse_number

    n, _ = _parse_number(s)
    return n
