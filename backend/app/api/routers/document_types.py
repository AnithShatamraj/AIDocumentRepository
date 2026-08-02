"""Document type management + versioned field-schema authoring.

Privileged users define their own document types here: a name, a description
and a tree of field definitions (scalars, objects, and lists of either). Saving
fields mints a new immutable schema version — existing extracted values keep
pointing at the version they were produced under.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.core.db import get_db
from app.models.catalog import DocumentType, TypeSchema
from app.models.document import document_type_links
from app.models.tenant import User
from app.schemas import DocumentTypeCreate, DocumentTypeOut, DocumentTypeUpdate
from app.services.fields import (
    SchemaError,
    compile_json_schema,
    dump_schema,
    slugify_key,
    validate_schema,
)

router = APIRouter(prefix="/document-types", tags=["document-types"])


def _ensure_keys(nodes: list[dict]) -> list[dict]:
    """Let the UI omit `key` — derive a stable one from the name, recursively."""
    out = []
    for n in nodes or []:
        n = dict(n)
        if not n.get("key"):
            n["key"] = slugify_key(n.get("name", ""))
        if n.get("fields"):
            n["fields"] = _ensure_keys(n["fields"])
        if n.get("item"):
            item = dict(n["item"])
            if not item.get("key"):
                item["key"] = slugify_key(item.get("name") or f"{n['key']}_item")
            if item.get("fields"):
                item["fields"] = _ensure_keys(item["fields"])
            n["item"] = item
        out.append(n)
    return out


def _validated(fields: list[dict]) -> list[dict]:
    try:
        return dump_schema(validate_schema(_ensure_keys(fields)))
    except SchemaError as e:
        raise HTTPException(422, f"Invalid field schema: {e}")


async def _active_fields(db: AsyncSession, dt: DocumentType) -> tuple[int | None, list]:
    if not dt.active_schema_id:
        return None, []
    schema = await db.get(TypeSchema, dt.active_schema_id)
    return (schema.version, schema.fields) if schema else (None, [])


@router.get("", response_model=list[DocumentTypeOut])
async def list_types(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rows = await db.execute(
        select(DocumentType).where(DocumentType.tenant_id == user.tenant_id).order_by(DocumentType.name))
    return [DocumentTypeOut.model_validate(t) for t in rows.scalars().all()]


@router.get("/{type_id}")
async def get_type(type_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    dt = await db.get(DocumentType, type_id)
    if not dt or dt.tenant_id != user.tenant_id:
        raise HTTPException(404, "Document type not found")
    version, fields = await _active_fields(db, dt)
    return {
        "id": str(dt.id), "name": dt.name, "description": dt.description,
        "is_enabled": dt.is_enabled, "is_system": dt.is_system,
        "schema_version": version, "fields": fields,
    }


@router.get("/{type_id}/schema")
async def get_schema(type_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Active field tree plus the compiled JSON Schema (useful for debugging)."""
    dt = await db.get(DocumentType, type_id)
    if not dt or dt.tenant_id != user.tenant_id:
        raise HTTPException(404, "Document type not found")
    version, fields = await _active_fields(db, dt)
    compiled = compile_json_schema(validate_schema(fields), dt.name) if fields else None
    return {"document_type_id": str(dt.id), "version": version, "fields": fields,
            "json_schema": compiled}


@router.get("/{type_id}/versions")
async def list_versions(type_id: str, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    dt = await db.get(DocumentType, type_id)
    if not dt or dt.tenant_id != admin.tenant_id:
        raise HTTPException(404, "Document type not found")
    rows = (await db.execute(
        select(TypeSchema).where(TypeSchema.document_type_id == dt.id).order_by(TypeSchema.version.desc())
    )).scalars().all()
    return [{"id": str(s.id), "version": s.version, "field_count": len(s.fields or []),
             "is_active": s.id == dt.active_schema_id, "created_at": s.created_at.isoformat()}
            for s in rows]


@router.post("", response_model=DocumentTypeOut, status_code=201)
async def create_type(body: DocumentTypeCreate, admin: User = Depends(require_admin),
                      db: AsyncSession = Depends(get_db)):
    exists = (await db.execute(
        select(DocumentType).where(DocumentType.tenant_id == admin.tenant_id,
                                   func.lower(DocumentType.name) == body.name.strip().lower())
    )).scalars().first()
    if exists:
        raise HTTPException(409, f"A document type named '{body.name}' already exists")

    dt = DocumentType(tenant_id=admin.tenant_id, name=body.name.strip(), description=body.description)
    db.add(dt)
    await db.flush()
    if body.fields:
        schema = TypeSchema(tenant_id=admin.tenant_id, document_type_id=dt.id, version=1,
                            fields=_validated(body.fields), created_by=admin.id)
        db.add(schema)
        await db.flush()
        dt.active_schema_id = schema.id
    await db.commit()
    await db.refresh(dt)
    return DocumentTypeOut.model_validate(dt)


@router.patch("/{type_id}", response_model=DocumentTypeOut)
async def update_type(type_id: str, body: DocumentTypeUpdate, admin: User = Depends(require_admin),
                      db: AsyncSession = Depends(get_db)):
    dt = await db.get(DocumentType, type_id)
    if not dt or dt.tenant_id != admin.tenant_id:
        raise HTTPException(404, "Document type not found")
    if body.name is not None:
        dt.name = body.name.strip()
    if body.description is not None:
        dt.description = body.description
    if body.is_enabled is not None:
        dt.is_enabled = body.is_enabled
    if body.fields is not None:
        # Field trees are immutable per version — editing mints the next one.
        maxv = (await db.execute(
            select(func.coalesce(func.max(TypeSchema.version), 0)).where(
                TypeSchema.document_type_id == dt.id)
        )).scalar() or 0
        schema = TypeSchema(tenant_id=admin.tenant_id, document_type_id=dt.id, version=maxv + 1,
                            fields=_validated(body.fields), created_by=admin.id)
        db.add(schema)
        await db.flush()
        dt.active_schema_id = schema.id
    await db.commit()
    await db.refresh(dt)
    return DocumentTypeOut.model_validate(dt)


@router.delete("/{type_id}", status_code=204)
async def delete_type(type_id: str, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    dt = await db.get(DocumentType, type_id)
    if not dt or dt.tenant_id != admin.tenant_id:
        raise HTTPException(404, "Document type not found")
    assigned = (await db.execute(
        select(func.count()).select_from(document_type_links).where(
            document_type_links.c.document_type_id == dt.id)
    )).scalar()
    if assigned:
        raise HTTPException(409, f"Cannot delete: {assigned} document(s) use this type. Reassign first.")
    await db.delete(dt)
    await db.commit()


@router.post("/validate")
async def validate_fields(body: dict, admin: User = Depends(require_admin)):
    """Dry-run validation for the builder UI — no persistence."""
    try:
        defs = validate_schema(_ensure_keys(body.get("fields") or []))
    except SchemaError as e:
        return {"valid": False, "error": str(e)}
    return {"valid": True, "fields": dump_schema(defs),
            "json_schema": compile_json_schema(defs, body.get("name") or "extraction")}
