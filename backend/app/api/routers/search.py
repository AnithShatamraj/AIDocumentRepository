"""Semantic + structured search endpoints (permission-aware)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.db import get_db
from app.models.tenant import User
from app.schemas import SearchHit, SearchRequest, StructuredSearchRequest
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


@router.post("/structured")
async def structured(body: StructuredSearchRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    results = await structured_search.query(
        db, user, field_name=body.field_name, op=body.op, value=body.value, value2=body.value2,
        category_id=body.category_id, verified_only=body.verified_only, limit=body.limit)
    return {"count": len(results), "results": results, "verified_only": body.verified_only}
