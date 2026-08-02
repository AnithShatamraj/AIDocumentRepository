"""Security acceptance tests (spec Section 21): permission-aware retrieval.

Integration tests — require a live Postgres (run inside the stack):
    docker compose --profile apps run --rm api pytest -q tests/test_permissions.py

Proves:
  (a) a user without Read on a document cannot retrieve its chunks via search,
  (b) granting Read makes it retrievable,
  (c) revocation is effective on the very next query (no reindex).
"""
from __future__ import annotations

import uuid

import pytest

from app.core.db import AsyncSessionLocal
from app.core.security import hash_password
from app.models.constants import PERM_READ, ROLE_VIEWER
from app.models.content import Chunk, Embedding
from app.models.document import Document, DocumentPermission
from app.models.tenant import Tenant, User
from app.services import search

pytestmark = pytest.mark.asyncio


async def _mk_user(db, tenant_id, email, role=ROLE_VIEWER):
    u = User(tenant_id=tenant_id, email=email, hashed_password=hash_password("x" * 10), role=role)
    db.add(u)
    await db.flush()
    return u


@pytest.fixture
async def scenario():
    async with AsyncSessionLocal() as db:
        t = Tenant(name="T", slug=f"t-{uuid.uuid4().hex[:8]}")
        db.add(t)
        await db.flush()
        owner = await _mk_user(db, t.id, f"owner-{uuid.uuid4().hex[:6]}@t.test")
        other = await _mk_user(db, t.id, f"other-{uuid.uuid4().hex[:6]}@t.test")

        doc = Document(tenant_id=t.id, owner_id=owner.id, name="secret.txt", file_type="txt",
                       current_version=1, processing_status="processed")
        db.add(doc)
        await db.flush()
        chunk = Chunk(id=uuid.uuid4(), tenant_id=t.id, document_id=doc.id, document_version=1,
                      ordinal=0, content="the eagle lands at midnight")
        db.add(chunk)
        await db.flush()
        # Stub embedding is deterministic; embed the same text.
        from app.ai.registry import get_embedder
        from app.core.config import settings
        vec = get_embedder().embed([chunk.content], model=settings.embedding_model).vectors[0]
        db.add(Embedding(tenant_id=t.id, chunk_id=chunk.id, document_id=doc.id, vector=vec,
                         embedding_model="stub", embedding_dim=len(vec)))
        await db.commit()
        tenant_id, doc_id = t.id, doc.id
        try:
            yield {"tenant": t, "owner": owner, "other": other, "doc": doc}
        finally:
            # Without this, each run leaks a tenant/user/document/chunk/embedding
            # into the database the tests point at. Documents go first:
            # documents.owner_id is ON DELETE RESTRICT, so dropping the tenant
            # (which cascades to users) is blocked while a document survives.
            # Core DELETEs, not session.delete(): the ORM would try to NULL the
            # children's tenant_id instead of letting Postgres ON DELETE CASCADE run.
            from sqlalchemy import delete as sql_delete

            async with AsyncSessionLocal() as cleanup:
                await cleanup.execute(sql_delete(Document).where(Document.id == doc_id))
                await cleanup.execute(sql_delete(Tenant).where(Tenant.id == tenant_id))
                await cleanup.commit()


async def test_owner_can_retrieve(scenario):
    async with AsyncSessionLocal() as db:
        owner = await db.get(User, scenario["owner"].id)
        hits = await search.vector_search(db, owner, "eagle midnight", k=5)
        assert any(h.document_id == scenario["doc"].id for h in hits)


async def test_unauthorized_cannot_retrieve(scenario):
    async with AsyncSessionLocal() as db:
        other = await db.get(User, scenario["other"].id)
        hits = await search.vector_search(db, other, "eagle midnight", k=5)
        assert all(h.document_id != scenario["doc"].id for h in hits)


async def test_grant_then_revoke_is_immediately_effective(scenario):
    doc, other = scenario["doc"], scenario["other"]
    async with AsyncSessionLocal() as db:
        db.add(DocumentPermission(tenant_id=doc.tenant_id, document_id=doc.id, user_id=other.id, level=PERM_READ))
        await db.commit()
    async with AsyncSessionLocal() as db:
        u = await db.get(User, other.id)
        hits = await search.vector_search(db, u, "eagle midnight", k=5)
        assert any(h.document_id == doc.id for h in hits), "grant should be effective immediately"
    # Revoke
    async with AsyncSessionLocal() as db:
        perm = (await db.execute(
            __import__("sqlalchemy").select(DocumentPermission).where(
                DocumentPermission.document_id == doc.id, DocumentPermission.user_id == other.id)
        )).scalars().first()
        await db.delete(perm)
        await db.commit()
    async with AsyncSessionLocal() as db:
        u = await db.get(User, other.id)
        hits = await search.vector_search(db, u, "eagle midnight", k=5)
        assert all(h.document_id != doc.id for h in hits), "revoke should be effective on next query"
