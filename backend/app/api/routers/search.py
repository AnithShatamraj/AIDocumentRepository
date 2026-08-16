"""Semantic + structured search endpoints (permission-aware)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.db import get_db
from app.models.catalog import DocumentType, TypeSchema
from app.models.tenant import User
from app.schemas import SearchHit, SearchRequest, StructuredSearchRequest
from app.services import fields as fieldsvc
from app.services import search, structured_search

router = APIRouter(prefix="/search", tags=["search"])


@router.post("", response_model=list[SearchHit])
async def semantic_search(body: SearchRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if body.mode == "vector":
        hits = await search.vector_search(db, user, body.query, k=body.k)
    elif body.mode == "keyword":
        hits = await search.keyword_search(db, user, body.query, k=body.k)
    else:
        hits = await search.hybrid_search(db, user, body.query, k=body.k)
    return [SearchHit(**h.__dict__) for h in hits]


@router.get("/fields")
async def searchable_fields(
    document_type_id: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Leaf fields available for structured search, flattened from each document
    type's active schema. A free-text field name can't express "any item in
    this list" — the UI drives structured search off this list instead."""
    types_q = select(DocumentType).where(
        DocumentType.tenant_id == user.tenant_id, DocumentType.is_enabled.is_(True))
    if document_type_id:
        types_q = types_q.where(DocumentType.id == document_type_id)
    types = (await db.execute(types_q)).scalars().all()

    seen: dict[tuple[str, str], dict] = {}
    for t in types:
        if not t.active_schema_id:
            continue
        schema = await db.get(TypeSchema, t.active_schema_id)
        if not schema or not schema.fields:
            continue
        try:
            defs = fieldsvc.validate_schema(schema.fields)
        except fieldsvc.SchemaError:
            continue
        for leaf in fieldsvc.list_leaf_paths(defs):
            dedupe_key = (leaf.field_key, leaf.data_type)
            entry = seen.setdefault(dedupe_key, {
                "field_key": leaf.field_key, "data_type": leaf.data_type,
                "label": leaf.label, "path_pattern": leaf.path_pattern,
                "repeats": leaf.repeats, "document_types": [],
            })
            entry["document_types"].append({"id": str(t.id), "name": t.name})

    return sorted(seen.values(), key=lambda e: e["label"])


@router.post("/structured")
async def structured(body: StructuredSearchRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    results = await structured_search.query(
        db, user, field_key=body.field_key, field_path=body.field_path, field_name=body.field_name,
        op=body.op, value=body.value, value2=body.value2,
        document_type_id=body.document_type_id, verified_only=body.verified_only, limit=body.limit)
    return {"count": len(results), "results": results, "verified_only": body.verified_only}
