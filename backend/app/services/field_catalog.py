"""What types, fields and values actually exist — the vocabulary for structured search.

Without this, anything building a structured filter is guessing. Ask for
"Mumbai rent agreements" and a filter gets written as `city = "Mumbai"` against
data stored as `"Mumbai, MH"`; the query returns nothing and the caller reports
"no matching documents" with total confidence. That silent-zero failure is
worse for trust than a wrong answer, because nothing about it looks wrong.

So: `list_document_types` says what kinds of documents exist,
`describe_document_type` says which fields one has, and `field_values` says
which values a field actually holds. Every one of them counts through
`readable_documents_condition` — a field value is disclosure exactly like a tag
name is, and "which vendors do we have contracts with" must not be answerable
by someone with no access to those contracts.
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import DocumentType, TypeSchema
from app.models.content import FieldValue
from app.models.document import Document, document_type_links
from app.models.tenant import User
from app.services import fields as fieldsvc
from app.services import permissions

MAX_DISTINCT_VALUES = 50


async def _readable(db: AsyncSession, user: User):
    group_ids = await permissions.user_group_ids(db, user.id)
    return permissions.readable_documents_condition(user, group_ids)


async def list_document_types(db: AsyncSession, user: User) -> list[dict]:
    """Enabled document types, each with how many documents the caller can see.

    The count is per-caller: a type every one of whose documents is closed to
    this user reports 0, which is the honest answer to "is it worth filtering
    on this?" without disclosing anything about the documents themselves.
    """
    cond = await _readable(db, user)
    counts = dict((await db.execute(
        select(document_type_links.c.document_type_id, func.count(Document.id))
        .select_from(document_type_links)
        .join(Document, Document.id == document_type_links.c.document_id)
        .where(cond)
        .group_by(document_type_links.c.document_type_id)
    )).all())

    types = (await db.execute(
        select(DocumentType)
        .where(DocumentType.tenant_id == user.tenant_id, DocumentType.is_enabled.is_(True))
        .order_by(DocumentType.name)
    )).scalars().all()

    out = []
    for t in types:
        leaves = await _leaves(db, t)
        out.append({
            "id": str(t.id),
            "name": t.name,
            "description": t.description,
            "field_count": len(leaves),
            "document_count": int(counts.get(t.id, 0)),
        })
    return out


async def _leaves(db: AsyncSession, dtype: DocumentType) -> list[fieldsvc.LeafPath]:
    if not dtype.active_schema_id:
        return []
    schema = await db.get(TypeSchema, dtype.active_schema_id)
    if not schema or not schema.fields:
        return []
    try:
        return fieldsvc.list_leaf_paths(fieldsvc.validate_schema(schema.fields))
    except fieldsvc.SchemaError:
        # A schema that no longer validates shouldn't take the catalog down;
        # it just has nothing to contribute.
        return []


async def describe_document_type(
    db: AsyncSession, user: User, type_id: uuid.UUID | str
) -> dict | None:
    """One type's searchable leaf fields, flattened with their path patterns."""
    dtype = await db.get(DocumentType, str(type_id))
    if not dtype or dtype.tenant_id != user.tenant_id:
        return None
    leaves = await _leaves(db, dtype)
    return {
        "id": str(dtype.id),
        "name": dtype.name,
        "description": dtype.description,
        "fields": [
            {"field_key": l.field_key, "path_pattern": l.path_pattern, "label": l.label,
             "data_type": l.data_type, "repeats": l.repeats}
            for l in leaves
        ],
    }


async def field_values(
    db: AsyncSession,
    user: User,
    *,
    field_key: str,
    contains: str | None = None,
    document_type_id: uuid.UUID | str | None = None,
    limit: int = MAX_DISTINCT_VALUES,
) -> dict:
    """Distinct values this field actually holds, most common first.

    This is what turns a vague filter into a correct one: before asserting
    `city = "Mumbai"`, look at what `city` really contains. `truncated` tells
    the caller the list is a sample, so "not in this list" is never mistaken
    for "not in the data".
    """
    cond = await _readable(db, user)
    filters = [
        cond,
        FieldValue.field_key == field_key,
        FieldValue.node_kind == "scalar",
        FieldValue.document_version == Document.current_version,
        FieldValue.raw_value.isnot(None),
        func.length(func.trim(FieldValue.raw_value)) > 0,
    ]
    if contains:
        filters.append(FieldValue.raw_value.ilike(f"%{contains}%"))
    if document_type_id:
        filters.append(FieldValue.document_type_id == str(document_type_id))

    base = (
        select(
            FieldValue.raw_value.label("value"),
            func.count(func.distinct(FieldValue.document_id)).label("document_count"),
        )
        .join(Document, Document.id == FieldValue.document_id)
        .where(*filters)
        .group_by(FieldValue.raw_value)
    )

    rows = (await db.execute(
        base.order_by(func.count(func.distinct(FieldValue.document_id)).desc(),
                      FieldValue.raw_value)
        .limit(limit + 1)
    )).all()

    truncated = len(rows) > limit
    values = [{"value": v, "document_count": int(c)} for v, c in rows[:limit]]

    data_type = (await db.execute(
        select(FieldValue.value_type)
        .join(Document, Document.id == FieldValue.document_id)
        .where(cond, FieldValue.field_key == field_key, FieldValue.value_type.isnot(None))
        .limit(1)
    )).scalar()

    return {
        "field_key": field_key,
        "value_type": data_type,
        "values": values,
        "truncated": truncated,
    }
