"""Scoped retrieval, multi-condition document finding, and the field catalog.

The centrepiece is `test_scoped_vector_search_returns_the_true_top_k`: it
recomputes cosine similarity in Python and demands the database agree, in
order. That is the property the research assistant's coverage claims rest on --
"I searched all N documents in your scope" is only honest if the search was
exhaustive, and an HNSW traversal under a filter cannot promise that.

Integration tests -- require a live Postgres (run inside the stack):
    docker compose exec -T api pytest -q tests/test_retrieval_scope.py
"""
from __future__ import annotations

import math
import uuid

import pytest
from sqlalchemy import delete as sql_delete

from app.ai.registry import get_embedder
from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.security import hash_password
from app.models.catalog import DocumentType, TypeSchema
from app.models.constants import PERM_READ, RV_AUTO_ACCEPTED, ROLE_VIEWER, VT_TEXT
from app.models.content import Chunk, Embedding, FieldValue
from app.models.document import Document, DocumentPermission, document_type_links
from app.models.tag import Tag
from app.models.tenant import User
from app.services import field_catalog, search, structured_search
from app.services import tags as tag_service

# No module-level `pytestmark` -- this file mixes sync and async tests, and
# pytest.ini already sets asyncio_mode=auto so async defs need no marker.

CITIES = ["Mumbai", "Mumbai, MH", "Hyderabad", "Bengaluru"]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _embed(text: str) -> list[float]:
    return get_embedder().embed([text], model=settings.embedding_model).vectors[0]


# ------------------------------------------------------------- pure logic
def test_exact_scan_chosen_only_for_a_bounded_scope():
    assert search._use_exact_scan(None) is False, "unscoped search keeps the index"
    assert search._use_exact_scan([uuid.uuid4()]) is True
    assert search._use_exact_scan([uuid.uuid4() for _ in range(5)]) is True
    too_many = [uuid.uuid4() for _ in range(search.EXACT_SCAN_MAX_DOCUMENTS + 1)]
    assert search._use_exact_scan(too_many) is False


# ------------------------------------------------------------- fixture
@pytest.fixture
async def corpus():
    """8 documents the owner can read, 2 of which are also shared with `other`.

    Each carries one chunk (+ embedding) and a `city` field value, so the same
    fixture exercises vector scope, structured finding and the catalog.
    """
    async with AsyncSessionLocal() as db:
        tenant_id = uuid.uuid4()
        owner = User(tenant_id=tenant_id, email=f"owner-{uuid.uuid4().hex[:6]}@t.test",
                     hashed_password=hash_password("x" * 10), role=ROLE_VIEWER)
        other = User(tenant_id=tenant_id, email=f"other-{uuid.uuid4().hex[:6]}@t.test",
                     hashed_password=hash_password("x" * 10), role=ROLE_VIEWER)
        db.add_all([owner, other])
        await db.flush()

        dtype = DocumentType(tenant_id=tenant_id, name=f"Rent Agreement {uuid.uuid4().hex[:4]}",
                             description="Residential lease")
        db.add(dtype)
        await db.flush()
        schema = TypeSchema(tenant_id=tenant_id, document_type_id=dtype.id, version=1, fields=[
            {"key": "city", "name": "City", "data_type": "string", "description": ""},
            {"key": "monthly_rent", "name": "Monthly Rent", "data_type": "number", "description": ""},
        ])
        db.add(schema)
        await db.flush()
        dtype.active_schema_id = schema.id

        tag = await tag_service.get_or_create(db, tenant_id=tenant_id, name="Leases",
                                              created_by=owner.id)

        docs, vectors = [], {}
        for i in range(8):
            city = CITIES[i % len(CITIES)]
            body = f"lease {i} covering a flat in {city} with a sub-leasing clause"
            d = Document(tenant_id=tenant_id, owner_id=owner.id, name=f"lease-{i}-{uuid.uuid4().hex[:4]}.txt",
                         file_type="txt", current_version=1, processing_status="processed")
            db.add(d)
            await db.flush()
            # Insert the link directly: `d.document_types.append(...)` would
            # lazy-load the collection first, which is IO on an async session.
            await db.execute(document_type_links.insert().values(
                document_id=d.id, document_type_id=dtype.id))

            chunk = Chunk(id=uuid.uuid4(), tenant_id=tenant_id, document_id=d.id,
                          document_version=1, ordinal=0, content=body)
            db.add(chunk)
            await db.flush()
            vec = _embed(body)
            vectors[chunk.id] = vec
            db.add(Embedding(tenant_id=tenant_id, chunk_id=chunk.id, document_id=d.id,
                             vector=vec, embedding_model="stub", embedding_dim=len(vec)))
            db.add(FieldValue(
                id=uuid.uuid4(), tenant_id=tenant_id, document_id=d.id, document_version=1,
                document_type_id=dtype.id, field_key="city", field_path="city",
                field_name="City", node_kind="scalar", data_type="string",
                raw_value=city, value_type=VT_TEXT, value_text=city,
                confidence=0.9, review_status=RV_AUTO_ACCEPTED))
            db.add(FieldValue(
                id=uuid.uuid4(), tenant_id=tenant_id, document_id=d.id, document_version=1,
                document_type_id=dtype.id, field_key="monthly_rent", field_path="monthly_rent",
                field_name="Monthly Rent", node_kind="scalar", data_type="number",
                raw_value=str(20000 + i * 5000), value_type=VT_TEXT,
                value_text=str(20000 + i * 5000), value_number=20000 + i * 5000,
                confidence=0.9, review_status=RV_AUTO_ACCEPTED))
            docs.append((d.id, chunk.id, city))

        # Only the first two are shared with `other`.
        for d_id, _, _ in docs[:2]:
            db.add(DocumentPermission(tenant_id=tenant_id, document_id=d_id,
                                      user_id=other.id, level=PERM_READ, granted_by=owner.id))
        # Tag one "Mumbai" (index 0) and one "Mumbai, MH" (index 5), so an
        # equality filter over the tagged set has something to discriminate.
        for idx in (0, 5):
            await tag_service.apply_changes(db, user=owner, document_id=docs[idx][0], add=[tag])
        await db.commit()

        state = {"tenant_id": tenant_id, "owner_id": owner.id, "other_id": other.id,
                 "dtype_id": dtype.id, "docs": docs, "vectors": vectors, "tag": tag.name}
        try:
            yield state
        finally:
            async with AsyncSessionLocal() as cleanup:
                await cleanup.execute(sql_delete(Document).where(Document.tenant_id == tenant_id))
                await cleanup.execute(sql_delete(TypeSchema).where(TypeSchema.tenant_id == tenant_id))
                await cleanup.execute(sql_delete(DocumentType).where(DocumentType.tenant_id == tenant_id))
                await cleanup.execute(sql_delete(Tag).where(Tag.tenant_id == tenant_id))
                await cleanup.execute(sql_delete(User).where(User.tenant_id == tenant_id))
                await cleanup.commit()


# ------------------------------------------------------- scoped retrieval
async def test_scoped_vector_search_returns_the_true_top_k(corpus):
    """The database's ranking must match cosine similarity computed by hand."""
    scope = [d for d, _, _ in corpus["docs"][:5]]
    chunk_of = {d: c for d, c, _ in corpus["docs"]}
    query = "flat in Mumbai with sub-leasing"
    qvec = _embed(query)

    expected = sorted(
        (chunk_of[d] for d in scope),
        key=lambda cid: _cosine(qvec, corpus["vectors"][cid]),
        reverse=True,
    )[:3]

    async with AsyncSessionLocal() as db:
        owner = await db.get(User, corpus["owner_id"])
        hits = await search.vector_search(db, owner, query, k=3, document_ids=scope)

    assert [h.chunk_id for h in hits] == expected, "scoped search must be exhaustive, not approximate"
    for h in hits:
        assert h.score == pytest.approx(_cosine(qvec, corpus["vectors"][h.chunk_id]), abs=1e-4)


async def test_scope_excludes_everything_outside_it(corpus):
    scope = [d for d, _, _ in corpus["docs"][:2]]
    async with AsyncSessionLocal() as db:
        owner = await db.get(User, corpus["owner_id"])
        hits = await search.vector_search(db, owner, "lease", k=10, document_ids=scope)
    assert {h.document_id for h in hits} <= set(scope)


async def test_empty_scope_is_not_the_whole_corpus(corpus):
    """`[]` means an empty scope; `None` means unscoped. Conflating them is how
    a scoped question gets answered from outside its scope."""
    async with AsyncSessionLocal() as db:
        owner = await db.get(User, corpus["owner_id"])
        assert await search.vector_search(db, owner, "lease", k=5, document_ids=[]) == []
        assert await search.keyword_search(db, owner, "lease", k=5, document_ids=[]) == []
        assert await search.hybrid_search(db, owner, "lease", k=5, document_ids=[]) == []
        assert len(await search.vector_search(db, owner, "lease", k=5, document_ids=None)) > 0


async def test_scope_never_widens_past_permissions(corpus):
    """A scope naming documents the caller cannot read yields nothing for them."""
    all_ids = [d for d, _, _ in corpus["docs"]]
    async with AsyncSessionLocal() as db:
        other = await db.get(User, corpus["other_id"])
        hits = await search.vector_search(db, other, "lease", k=10, document_ids=all_ids)
    readable = {d for d, _, _ in corpus["docs"][:2]}
    assert {h.document_id for h in hits} <= readable


async def test_keyword_and_hybrid_honour_scope(corpus):
    scope = [d for d, _, _ in corpus["docs"][:3]]
    async with AsyncSessionLocal() as db:
        owner = await db.get(User, corpus["owner_id"])
        kw = await search.keyword_search(db, owner, "sub-leasing", k=10, document_ids=scope)
        hy = await search.hybrid_search(db, owner, "sub-leasing", k=10, document_ids=scope)
    assert kw and {h.document_id for h in kw} <= set(scope)
    assert hy and {h.document_id for h in hy} <= set(scope)


# ------------------------------------------------------- find_documents
async def test_find_documents_ands_type_tag_and_field_conditions(corpus):
    async with AsyncSessionLocal() as db:
        owner = await db.get(User, corpus["owner_id"])
        got = await structured_search.find_documents(
            db, owner,
            document_type_ids=[corpus["dtype_id"]],
            tags=[corpus["tag"]],
            field_conditions=[structured_search.FieldCondition(
                field_key="city", op="eq", value="Mumbai")],
        )
    # Tagged documents are indices 0 ("Mumbai") and 5 ("Mumbai, MH"); an
    # equality filter must keep the first and reject the second.
    assert len(got) == 1
    assert got[0]["id"] == str(corpus["docs"][0][0])
    assert got[0]["tags"][0]["name"] == corpus["tag"]
    assert got[0]["document_types"][0]["id"] == str(corpus["dtype_id"])


async def test_eq_on_text_is_equality_not_substring(corpus):
    """Regression: `eq` used to fall through to a substring match, so "equals
    Mumbai" also returned "Mumbai, MH". The search UI offers both operators
    separately, so they have to mean different things."""
    async with AsyncSessionLocal() as db:
        owner = await db.get(User, corpus["owner_id"])
        exact = await structured_search.find_documents(
            db, owner, field_conditions=[structured_search.FieldCondition(
                field_key="city", op="eq", value="Mumbai")])
        loose = await structured_search.find_documents(
            db, owner, field_conditions=[structured_search.FieldCondition(
                field_key="city", op="contains", value="Mumbai")])
    assert len(exact) == 2, "only the two documents whose city is exactly Mumbai"
    assert len(loose) == 4, "contains still picks up 'Mumbai, MH'"


async def test_find_documents_numeric_condition(corpus):
    async with AsyncSessionLocal() as db:
        owner = await db.get(User, corpus["owner_id"])
        got = await structured_search.find_documents(
            db, owner,
            field_conditions=[structured_search.FieldCondition(
                field_key="monthly_rent", op="gte", value="40000")],
        )
    # rents are 20000 + 5000*i, so i >= 4 qualifies: four documents.
    assert len(got) == 4


async def test_find_documents_unknown_tag_refuses_rather_than_widening(corpus):
    async with AsyncSessionLocal() as db:
        owner = await db.get(User, corpus["owner_id"])
        with pytest.raises(structured_search.UnknownTag):
            await structured_search.find_documents(
                db, owner, tags=[corpus["tag"], "NoSuchTag"], tags_match_all=True)
        # Under "any", an unknown name simply contributes nothing.
        got = await structured_search.find_documents(
            db, owner, tags=[corpus["tag"], "NoSuchTag"], tags_match_all=False)
        assert len(got) == 2


async def test_find_documents_all_unknown_tags_narrows_to_nothing(corpus):
    """Regression: with every requested name unresolved the id list is empty,
    which used to read as "no tag filter" and return the whole corpus."""
    async with AsyncSessionLocal() as db:
        owner = await db.get(User, corpus["owner_id"])
        for match_all in (True, False):
            if match_all:
                with pytest.raises(structured_search.UnknownTag):
                    await structured_search.find_documents(
                        db, owner, tags=["Nope", "AlsoNope"], tags_match_all=True)
            else:
                got = await structured_search.find_documents(
                    db, owner, tags=["Nope", "AlsoNope"], tags_match_all=False)
                assert got == []


async def test_find_documents_is_permission_filtered(corpus):
    async with AsyncSessionLocal() as db:
        other = await db.get(User, corpus["other_id"])
        got = await structured_search.find_documents(db, other, document_type_ids=[corpus["dtype_id"]])
    assert {g["id"] for g in got} == {str(d) for d, _, _ in corpus["docs"][:2]}


# ------------------------------------------------------- field catalog
async def test_field_values_exposes_the_real_stored_spellings(corpus):
    """The whole point: 'Mumbai' and 'Mumbai, MH' are different values, and a
    filter writer has to see that before asserting equality on one of them."""
    async with AsyncSessionLocal() as db:
        owner = await db.get(User, corpus["owner_id"])
        out = await field_catalog.field_values(db, owner, field_key="city")
    by_value = {v["value"]: v["document_count"] for v in out["values"]}
    assert "Mumbai" in by_value and "Mumbai, MH" in by_value
    assert by_value["Mumbai"] == 2 and by_value["Mumbai, MH"] == 2
    assert out["truncated"] is False


async def test_field_values_are_permission_filtered(corpus):
    """A field value is disclosure just like a tag name: "which cities do we
    hold leases in" must not be answerable from documents you can't read."""
    async with AsyncSessionLocal() as db:
        other = await db.get(User, corpus["other_id"])
        out = await field_catalog.field_values(db, other, field_key="city")
    by_value = {v["value"]: v["document_count"] for v in out["values"]}
    # `other` can read documents 0 and 1 only -> Mumbai and "Mumbai, MH", once each.
    assert by_value == {"Mumbai": 1, "Mumbai, MH": 1}
    assert "Hyderabad" not in by_value


async def test_field_values_contains_filter(corpus):
    async with AsyncSessionLocal() as db:
        owner = await db.get(User, corpus["owner_id"])
        out = await field_catalog.field_values(db, owner, field_key="city", contains="mumbai")
    assert {v["value"] for v in out["values"]} == {"Mumbai", "Mumbai, MH"}


async def test_field_values_truncation_is_reported(corpus):
    async with AsyncSessionLocal() as db:
        owner = await db.get(User, corpus["owner_id"])
        out = await field_catalog.field_values(db, owner, field_key="city", limit=1)
    assert len(out["values"]) == 1
    assert out["truncated"] is True, "an absent value must never read as 'not in the data'"


async def test_document_type_counts_are_per_caller(corpus):
    async with AsyncSessionLocal() as db:
        owner = await db.get(User, corpus["owner_id"])
        other = await db.get(User, corpus["other_id"])
        mine = {t["id"]: t for t in await field_catalog.list_document_types(db, owner)}
        theirs = {t["id"]: t for t in await field_catalog.list_document_types(db, other)}
    tid = str(corpus["dtype_id"])
    assert mine[tid]["document_count"] == 8
    assert theirs[tid]["document_count"] == 2
    assert mine[tid]["field_count"] == 2


async def test_describe_document_type_lists_leaf_fields(corpus):
    async with AsyncSessionLocal() as db:
        owner = await db.get(User, corpus["owner_id"])
        out = await field_catalog.describe_document_type(db, owner, corpus["dtype_id"])
    assert {f["field_key"] for f in out["fields"]} == {"city", "monthly_rent"}
    assert out["fields"][0]["path_pattern"] in ("city", "monthly_rent")
