"""Category management + extraction-schema binding."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.core.db import get_db
from app.models.catalog import Category, ExtractionSchema
from app.models.document import document_category
from app.models.tenant import User
from app.schemas import CategoryCreate, CategoryOut, CategoryUpdate

router = APIRouter(prefix="/categories", tags=["categories"])


@router.get("", response_model=list[CategoryOut])
async def list_categories(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rows = await db.execute(select(Category).where(Category.tenant_id == user.tenant_id).order_by(Category.name))
    return [CategoryOut.model_validate(c) for c in rows.scalars().all()]


@router.get("/{category_id}/schema")
async def get_schema(category_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    cat = await db.get(Category, category_id)
    if not cat or cat.tenant_id != user.tenant_id:
        raise HTTPException(404, "Category not found")
    schema = await db.get(ExtractionSchema, cat.active_schema_id) if cat.active_schema_id else None
    return {"category_id": str(cat.id), "version": schema.version if schema else None,
            "fields": schema.fields if schema else []}


@router.post("", response_model=CategoryOut, status_code=201)
async def create_category(body: CategoryCreate, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    cat = Category(tenant_id=admin.tenant_id, name=body.name, description=body.description)
    db.add(cat)
    await db.flush()
    if body.fields:
        schema = ExtractionSchema(tenant_id=admin.tenant_id, category_id=cat.id, version=1, fields=body.fields)
        db.add(schema)
        await db.flush()
        cat.active_schema_id = schema.id
    await db.commit()
    await db.refresh(cat)
    return CategoryOut.model_validate(cat)


@router.patch("/{category_id}", response_model=CategoryOut)
async def update_category(category_id: str, body: CategoryUpdate, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    cat = await db.get(Category, category_id)
    if not cat or cat.tenant_id != admin.tenant_id:
        raise HTTPException(404, "Category not found")
    if body.name is not None:
        cat.name = body.name
    if body.description is not None:
        cat.description = body.description
    if body.is_enabled is not None:
        cat.is_enabled = body.is_enabled
    if body.fields is not None:
        # New schema version (schemas are versioned data).
        maxv = (await db.execute(
            select(func.coalesce(func.max(ExtractionSchema.version), 0)).where(ExtractionSchema.category_id == cat.id)
        )).scalar() or 0
        schema = ExtractionSchema(tenant_id=admin.tenant_id, category_id=cat.id, version=maxv + 1, fields=body.fields)
        db.add(schema)
        await db.flush()
        cat.active_schema_id = schema.id
    await db.commit()
    await db.refresh(cat)
    return CategoryOut.model_validate(cat)


@router.delete("/{category_id}", status_code=204)
async def delete_category(category_id: str, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    cat = await db.get(Category, category_id)
    if not cat or cat.tenant_id != admin.tenant_id:
        raise HTTPException(404, "Category not found")
    # Block deletion while documents are assigned (recommended behavior).
    assigned = (await db.execute(
        select(func.count()).select_from(document_category).where(document_category.c.category_id == cat.id)
    )).scalar()
    if assigned:
        raise HTTPException(409, f"Cannot delete: {assigned} document(s) assigned. Reassign first.")
    await db.delete(cat)
    await db.commit()
