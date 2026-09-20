"""Regression: deleting a document type that has schema versions.

`DocumentType.schemas` had no `passive_deletes`, so `session.delete(type)` made the
ORM try to NULL each child `type_schemas.document_type_id` (a NOT NULL column)
instead of leaving it to the database's ON DELETE CASCADE -- an IntegrityError,
i.e. a 500, for every type that had a schema.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete as sql_delete
from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.catalog import DocumentType, TypeSchema

pytestmark = pytest.mark.asyncio


async def test_deleting_a_document_type_removes_its_schema_versions():
    tenant_id = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        try:
            dt = DocumentType(tenant_id=tenant_id, name=f"T-{uuid.uuid4().hex[:6]}", description="")
            db.add(dt)
            await db.flush()
            schemas = [TypeSchema(tenant_id=tenant_id, document_type_id=dt.id, version=v, fields=[])
                       for v in (1, 2)]
            db.add_all(schemas)
            await db.flush()
            dt.active_schema_id = schemas[1].id
            await db.commit()
            ids = [s.id for s in schemas]

            await db.delete(dt)
            await db.commit()

            left = (await db.execute(select(TypeSchema.id).where(TypeSchema.id.in_(ids)))).all()
            assert left == []
        finally:
            await db.rollback()
            await db.execute(sql_delete(DocumentType).where(DocumentType.tenant_id == tenant_id))
            await db.commit()
