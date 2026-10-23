"""Promote a document's "new" (discovered) fields into one of its document types.

A discovered field is a fact the AI found that no document type's schema
defines. Promoting it does two things in one transaction:

  1. appends a field definition to the chosen document type's schema, as a new
     immutable schema version (or, when the fact is really an existing field
     worded differently, maps onto that field and changes no schema);
  2. converts this document's discovered row into a normal field of that type,
     so it appears with the type's other fields and is read for on every
     future document (and every reprocess) of the type.

Other documents of the type keep what they have until they are reprocessed;
that is the caller's message to show, not something this does silently.

The functions flush but never commit: the endpoint owns the transaction, so a
`PromoteError` anywhere leaves nothing half-applied.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import DocumentType, TypeSchema
from app.models.constants import RV_PENDING, RV_REJECTED, RV_VERIFIED
from app.models.content import FieldValue
from app.models.document import Document, document_type_links
from app.models.review import ReviewItem
from app.models.tenant import User
from app.services import normalize
from app.services.fields import (
    CONTAINER_TYPES, SCALAR_TYPES, SchemaError, dump_schema, ensure_keys, slugify_key, validate_schema,
)


class PromoteError(Exception):
    """A request that can't be applied; `status` is the HTTP code to answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


@dataclass
class PromoteItem:
    field_value_id: str
    name: str
    description: str
    data_type: str
    document_type_id: str
    existing_key: str | None = None  # map onto this existing field instead of adding one


def unique_key(name: str, taken: set[str]) -> str:
    """A schema key for `name` that doesn't collide with `taken` (adds _2, _3, …)."""
    base = slugify_key(name)
    key, n = base, 2
    while key in taken:
        suffix = f"_{n}"
        key = f"{base[: 63 - len(suffix)]}{suffix}"
        n += 1
    return key


async def promote_fields(db: AsyncSession, admin: User, doc: Document,
                         items: list[PromoteItem]) -> list[dict]:
    if not items:
        raise PromoteError(400, "Nothing to promote.")
    ids = [uuid.UUID(i.field_value_id) for i in items]
    if len(set(ids)) != len(ids):
        raise PromoteError(400, "The same field was listed twice.")

    rows = {r.id: r for r in (await db.execute(
        select(FieldValue).where(
            FieldValue.id.in_(ids), FieldValue.document_id == doc.id,
            FieldValue.document_version == doc.current_version))).scalars().all()}
    for fid in ids:
        row = rows.get(fid)
        if row is None or not row.is_discovered or row.node_kind != "scalar":
            raise PromoteError(409, "One of these is no longer a new field. Refresh and try again.")
        if row.review_status == RV_REJECTED:
            raise PromoteError(400, f"“{row.field_name}” was rejected; restore it before adding it to a type.")

    # Only the document's own types are valid homes: the field has to show up
    # in a section of *this* document, under a type that was actually matched.
    linked = {t.id: t for t in (await db.execute(
        select(DocumentType).join(document_type_links, document_type_links.c.document_type_id == DocumentType.id)
        .where(document_type_links.c.document_id == doc.id, DocumentType.tenant_id == admin.tenant_id)
    )).scalars().all()}

    by_type: dict[uuid.UUID, list[PromoteItem]] = {}
    for it in items:
        try:
            tid = uuid.UUID(it.document_type_id)
        except ValueError:
            raise PromoteError(400, "Unknown document type.")
        if tid not in linked:
            raise PromoteError(400, "A field can only be added to one of this document's own types.")
        by_type.setdefault(tid, []).append(it)

    original_keys = {fid: rows[fid].field_key for fid in ids}
    results: list[dict] = []

    for tid, group in by_type.items():
        dt = linked[tid]
        schema = await db.get(TypeSchema, dt.active_schema_id) if dt.active_schema_id else None
        fields = [dict(f) for f in (schema.fields if schema else [])]
        existing = {f["key"]: f for f in fields}
        taken = set(existing)
        added: list[str] = []
        plans = []

        for it in group:
            row = rows[uuid.UUID(it.field_value_id)]
            name = " ".join((it.name or "").split())[:80]
            if not name:
                raise PromoteError(422, "Every field needs a name.")

            if it.existing_key:
                target = existing.get(it.existing_key)
                if target is None or target["data_type"] in CONTAINER_TYPES:
                    raise PromoteError(409, f"“{dt.name}” has no plain field “{it.existing_key}” to map onto.")
                key, name, dtype = target["key"], target["name"], target["data_type"]
            else:
                dtype = it.data_type
                if dtype not in SCALAR_TYPES:
                    raise PromoteError(422, f"“{name}”: choose a simple type (text, number, date, …).")
                if slugify_key(name) in existing:
                    clash = existing[slugify_key(name)]["name"]
                    raise PromoteError(409, f"“{dt.name}” already has a field called “{clash}”. "
                                            f"Map “{row.field_name}” onto it, or pick a different name.")
                key = unique_key(name, taken)
                fields.append({"key": key, "name": name, "description": (it.description or "").strip()[:300],
                               "data_type": dtype})
                taken.add(key)
                added.append(key)

            clash_row = (await db.execute(select(FieldValue.id).where(
                FieldValue.document_id == doc.id, FieldValue.document_version == doc.current_version,
                FieldValue.document_type_id == dt.id, FieldValue.field_path == key,
                FieldValue.id != row.id))).first()
            if clash_row:
                raise PromoteError(409, f"This document already has a value for “{name}” under “{dt.name}”.")
            plans.append((row, key, name, dtype))

        version = schema.version if schema else None
        if added:
            try:
                clean = dump_schema(validate_schema(ensure_keys(fields)))
            except SchemaError as e:
                raise PromoteError(422, f"Can't add these fields to “{dt.name}”: {e}")
            version = (await db.execute(
                select(func.coalesce(func.max(TypeSchema.version), 0)).where(
                    TypeSchema.document_type_id == dt.id))).scalar() + 1
            new_schema = TypeSchema(tenant_id=admin.tenant_id, document_type_id=dt.id, version=version,
                                    fields=clean, created_by=admin.id)
            db.add(new_schema)
            await db.flush()
            dt.active_schema_id = new_schema.id
            order = {f["key"]: i for i, f in enumerate(clean)}
        else:
            order = {k: i for i, k in enumerate(existing)}

        for row, key, name, dtype in plans:
            norm = normalize.normalize(row.raw_value, normalize.NORMALIZER_TYPE.get(dtype, "text"))
            row.document_type_id = dt.id
            row.is_discovered = False
            row.field_key, row.field_path, row.field_name = key, key, name
            row.data_type, row.ordinal = dtype, order.get(key)
            row.value_type = norm.value_type
            row.value_text, row.value_number = norm.value_text, norm.value_number
            row.value_date, row.value_currency = norm.value_date, norm.value_currency
            row.schema_version = version
            # Adding it to a type *is* the acceptance. Already-accepted values keep their status.
            if row.review_status == RV_PENDING:
                row.review_status = RV_VERIFIED
            results.append({
                "field_value_id": str(row.id), "document_type_id": str(dt.id), "document_type_name": dt.name,
                "key": key, "name": name, "data_type": dtype, "schema_version": version,
                "created_field": key in added,
            })

    await db.flush()
    promoted = set(ids)
    await db.execute(update(ReviewItem).where(
        ReviewItem.field_value_id.in_(promoted), ReviewItem.status == RV_PENDING
    ).values(status=RV_VERIFIED))

    # The same fact is often discovered once per document type, and the page
    # shows them as one. Once it has a home, the copies are redundant.
    for key in set(original_keys.values()):
        await db.execute(delete(FieldValue).where(
            FieldValue.document_id == doc.id, FieldValue.document_version == doc.current_version,
            FieldValue.is_discovered.is_(True), FieldValue.field_key == key,
            FieldValue.id.not_in(promoted)))
    await db.flush()
    return results
