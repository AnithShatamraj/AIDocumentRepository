"""Discovery: find documents by structured criteria, and learn the vocabulary to do it with.

These are the read-only operations the research assistant will later call as
tools, exposed as ordinary endpoints so the UI can build a scope from the same
primitives — one implementation, not two.

Everything here is permission-filtered. Type counts, field values and results
all resolve against what the caller can read, so none of it can be used to
learn that a document, a vendor or a project exists.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_tenant_db
from app.models.tenant import User
from app.schemas import FindDocumentsRequest
from app.services import field_catalog, structured_search

router = APIRouter(prefix="/discovery", tags=["discovery"])


@router.get("/document-types", summary="List document types with per-caller document counts")
async def list_types(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_tenant_db)):
    """Enabled types, each with its field count and how many documents the
    caller can actually see under it — the first question to ask before
    filtering on a type."""
    return await field_catalog.list_document_types(db, user)


@router.get("/document-types/{type_id}", summary="Describe one type's searchable fields")
async def describe_type(type_id: str, user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_tenant_db)):
    """The type's leaf fields flattened to `field_key` + `path_pattern`, which
    are what `POST /discovery/documents` accepts as field conditions."""
    out = await field_catalog.describe_document_type(db, user, type_id)
    if out is None:
        raise HTTPException(404, "Document type not found")
    return out


@router.get("/fields/{field_key}/values", summary="Distinct values a field actually holds")
async def field_values(
    field_key: str,
    contains: str | None = Query(default=None, description="Substring filter over values"),
    document_type_id: str | None = Query(default=None),
    limit: int = Query(default=field_catalog.MAX_DISTINCT_VALUES, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Check what a field really contains before filtering on it.

    Guessing `city = "Mumbai"` against values stored as `"Mumbai, MH"` returns
    nothing and looks exactly like "there are no such documents". `truncated`
    marks a sampled list, so an absent value never means "not in the data".
    """
    return await field_catalog.field_values(
        db, user, field_key=field_key, contains=contains,
        document_type_id=document_type_id, limit=limit)


@router.post("/documents", summary="Find documents matching several structured criteria")
async def find_documents(body: FindDocumentsRequest, user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_tenant_db)):
    """ANDs every criterion: type, tags, extracted-field predicates, name,
    status and date range. Field predicates match when *any* value of that
    field qualifies, which is the right reading for fields that repeat inside
    lists. 422 if a required tag doesn't exist — no document can carry it, and
    dropping it silently would return a wider set than was asked for."""
    try:
        results = await structured_search.find_documents(
            db, user,
            document_type_ids=list(body.document_type_ids) or None,
            tags=body.tags or None,
            tags_match_all=(body.tags_match == "all"),
            field_conditions=[
                structured_search.FieldCondition(
                    field_key=c.field_key, field_path=c.field_path,
                    op=c.op, value=c.value, value2=c.value2)
                for c in body.field_conditions
            ] or None,
            name_contains=body.name_contains,
            status=body.status,
            created_after=body.created_after,
            created_before=body.created_before,
            verified_only=body.verified_only,
            limit=body.limit,
        )
    except structured_search.UnknownTag as e:
        raise HTTPException(422, str(e))
    return {"count": len(results), "documents": results}
