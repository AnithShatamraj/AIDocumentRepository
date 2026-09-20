"""Creating document types -- shared by the direct-create endpoint and by
publishing an AI-assisted draft, so both enforce the same uniqueness rule and
mint the same initial schema version."""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import DocumentType, TypeSchema


class NameTaken(Exception):
    def __init__(self, name: str):
        super().__init__(name)
        self.name = name


async def name_exists(db: AsyncSession, tenant_id: uuid.UUID, name: str) -> bool:
    return (await db.execute(
        select(DocumentType.id).where(
            DocumentType.tenant_id == tenant_id,
            func.lower(DocumentType.name) == name.strip().lower())
    )).first() is not None


async def create_document_type(
    db: AsyncSession, *, tenant_id: uuid.UUID, created_by: uuid.UUID | None,
    name: str, description: str, fields: list[dict] | None,
) -> DocumentType:
    """`fields` must already be validated (see services.fields.validate_schema).
    Flushes but does not commit -- the caller owns the transaction."""
    name = name.strip()
    if await name_exists(db, tenant_id, name):
        raise NameTaken(name)
    dt = DocumentType(tenant_id=tenant_id, name=name, description=description)
    db.add(dt)
    await db.flush()
    if fields:
        schema = TypeSchema(tenant_id=tenant_id, document_type_id=dt.id, version=1,
                            fields=fields, created_by=created_by)
        db.add(schema)
        await db.flush()
        dt.active_schema_id = schema.id
    return dt
