"""Path-aware structured search: leaf flattening, LIKE-pattern conversion,
the date-vs-number parsing priority fix, and end-to-end queries over a real
nested field-value tree (list-of-objects), including permission scoping.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete as sql_delete

from app.core.db import AsyncSessionLocal
from app.core.security import hash_password
from app.models.catalog import DocumentType
from app.models.constants import RV_AUTO_ACCEPTED, RV_PENDING, VT_TEXT
from app.models.content import FieldValue
from app.models.document import Document
from app.models.tenant import Tenant, User
from app.services import structured_search
from app.services.fields import list_leaf_paths, validate_schema

# No module-level `pytestmark` — this file mixes sync and async tests, and
# pytest.ini already sets asyncio_mode=auto so async defs need no marker.


# --------------------------------------------------------------- pure logic
def test_list_leaf_paths_flattens_nested_schema():
    schema = [
        {"key": "candidate_name", "name": "Candidate Name", "data_type": "string", "description": ""},
        {"key": "work_experience", "name": "Work Experience", "data_type": "list", "description": "",
         "item": {"key": "role", "name": "Role", "data_type": "object", "description": "", "fields": [
             {"key": "organization", "name": "Organization", "data_type": "string", "description": ""},
             {"key": "start_date", "name": "Start Date", "data_type": "date", "description": ""},
         ]}},
        {"key": "skills", "name": "Skills", "data_type": "list", "description": "",
         "item": {"key": "skill", "name": "Skill", "data_type": "string", "description": ""}},
    ]
    defs = validate_schema(schema)
    leaves = {f"{l.field_key}|{l.path_pattern}": l for l in list_leaf_paths(defs)}

    assert "candidate_name|candidate_name" in leaves
    assert leaves["candidate_name|candidate_name"].repeats is False

    org = leaves["organization|work_experience[].organization"]
    assert org.repeats is True and org.data_type == "string"
    assert org.label == "Work Experience → Organization"

    skill = leaves["skill|skills[]"]
    assert skill.repeats is True and skill.data_type == "string"


def test_path_like_converts_any_index_wildcard():
    # `_` is itself a SQL LIKE wildcard ("any one character"), so the literal
    # underscores in snake_case field keys must come back escaped — otherwise
    # "work_experience" would also match "workXexperience".
    assert structured_search._path_like("work_experience[].organization") == "work\\_experience[%].organization"
    assert structured_search._path_like("a%b_c[]") == "a\\%b\\_c[%]"


def test_value_filter_prioritizes_date_over_numeric_substring():
    # "2026-03-15" contains the substring "2026", which the number parser would
    # happily read as a bare number — date detection must win so "eq" compares
    # dates, not silently filter on value_number == 2026.
    clause = structured_search._value_filter("eq", "2026-03-15", None)
    compiled = str(clause.compile(compile_kwargs={"literal_binds": True}))
    assert "value_date" in compiled
    assert "value_number" not in compiled


# ----------------------------------------------------------- end-to-end (DB)
@pytest.fixture
async def resume_scenario():
    async with AsyncSessionLocal() as db:
        t = Tenant(name="T", slug=f"t-{uuid.uuid4().hex[:8]}")
        db.add(t)
        await db.flush()
        owner = User(tenant_id=t.id, email=f"owner-{uuid.uuid4().hex[:6]}@t.test",
                    hashed_password=hash_password("x" * 10), role="viewer")
        db.add(owner)
        await db.flush()

        dtype = DocumentType(tenant_id=t.id, name="Resume", description="CV")
        db.add(dtype)
        await db.flush()

        doc = Document(tenant_id=t.id, owner_id=owner.id, name="cv.txt", file_type="txt",
                       current_version=1, processing_status="processed")
        db.add(doc)
        await db.flush()

        # work_experience: a list of objects, each with organization + designation —
        # the exact shape a resume extraction produces.
        we = FieldValue(id=uuid.uuid4(), tenant_id=t.id, document_id=doc.id, document_version=1,
                        document_type_id=dtype.id, field_key="work_experience",
                        field_path="work_experience", field_name="Work Experience",
                        node_kind="list", data_type="list")
        db.add(we)
        await db.flush()

        item0 = FieldValue(id=uuid.uuid4(), tenant_id=t.id, document_id=doc.id, document_version=1,
                           document_type_id=dtype.id, parent_id=we.id, field_key="role",
                           field_path="work_experience[0]", field_name="Work Experience #1",
                           node_kind="object", data_type="object", ordinal=0)
        item1 = FieldValue(id=uuid.uuid4(), tenant_id=t.id, document_id=doc.id, document_version=1,
                           document_type_id=dtype.id, parent_id=we.id, field_key="role",
                           field_path="work_experience[1]", field_name="Work Experience #2",
                           node_kind="object", data_type="object", ordinal=1)
        db.add_all([item0, item1])
        await db.flush()

        def leaf(parent, key, path, name, value, status=RV_AUTO_ACCEPTED):
            return FieldValue(id=uuid.uuid4(), tenant_id=t.id, document_id=doc.id, document_version=1,
                              document_type_id=dtype.id, parent_id=parent.id, field_key=key,
                              field_path=path, field_name=name, node_kind="scalar", data_type="string",
                              raw_value=value, value_type=VT_TEXT, value_text=value,
                              confidence=0.95, review_status=status)

        db.add_all([
            leaf(item0, "organization", "work_experience[0].organization", "Organization", "Northwind Data Systems"),
            leaf(item0, "designation", "work_experience[0].designation", "Designation", "Principal Engineer"),
            leaf(item1, "organization", "work_experience[1].organization", "Organization", "Globex Consulting LLC"),
            leaf(item1, "designation", "work_experience[1].designation", "Designation", "Senior Engineer", status=RV_PENDING),
        ])
        await db.commit()

        tenant_id, doc_id = t.id, doc.id
        try:
            yield {"owner": owner, "doc": doc, "dtype": dtype}
        finally:
            async with AsyncSessionLocal() as cleanup:
                await cleanup.execute(sql_delete(Document).where(Document.id == doc_id))
                await cleanup.execute(sql_delete(Tenant).where(Tenant.id == tenant_id))
                await cleanup.commit()


async def test_field_key_matches_every_list_item(resume_scenario):
    async with AsyncSessionLocal() as db:
        owner = await db.get(User, resume_scenario["owner"].id)
        results = await structured_search.query(db, owner, field_key="organization", op="contains", value="")
        paths = sorted(r["field_path"] for r in results)
        assert paths == ["work_experience[0].organization", "work_experience[1].organization"]


async def test_field_path_wildcard_matches_any_index(resume_scenario):
    async with AsyncSessionLocal() as db:
        owner = await db.get(User, resume_scenario["owner"].id)
        results = await structured_search.query(
            db, owner, field_path="work_experience[].organization", op="contains", value="")
        assert len(results) == 2
        assert all(r["field_key"] == "organization" for r in results)


async def test_value_filter_finds_specific_match(resume_scenario):
    async with AsyncSessionLocal() as db:
        owner = await db.get(User, resume_scenario["owner"].id)
        results = await structured_search.query(
            db, owner, field_key="organization", op="contains", value="Globex")
        assert len(results) == 1
        assert results[0]["field_path"] == "work_experience[1].organization"


async def test_context_carries_sibling_fields(resume_scenario):
    async with AsyncSessionLocal() as db:
        owner = await db.get(User, resume_scenario["owner"].id)
        results = await structured_search.query(
            db, owner, field_key="organization", op="contains", value="Northwind")
        assert len(results) == 1
        assert results[0]["context"] == {
            "organization": "Northwind Data Systems", "designation": "Principal Engineer"}


async def test_document_type_filter_excludes_other_types(resume_scenario):
    async with AsyncSessionLocal() as db:
        owner = await db.get(User, resume_scenario["owner"].id)
        results = await structured_search.query(
            db, owner, field_key="organization", op="contains", value="",
            document_type_id=uuid.uuid4())
        assert results == []


async def test_verified_only_excludes_pending(resume_scenario):
    async with AsyncSessionLocal() as db:
        owner = await db.get(User, resume_scenario["owner"].id)
        results = await structured_search.query(
            db, owner, field_key="designation", op="contains", value="", verified_only=True)
        assert len(results) == 1
        assert results[0]["raw_value"] == "Principal Engineer"


async def test_permission_scoping_denies_unauthorized_user(resume_scenario):
    dtype = resume_scenario["dtype"]
    async with AsyncSessionLocal() as db:
        other = User(tenant_id=dtype.tenant_id, email=f"other-{uuid.uuid4().hex[:6]}@t.test",
                     hashed_password=hash_password("x" * 10), role="viewer")
        db.add(other)
        await db.commit()
    async with AsyncSessionLocal() as db:
        u = await db.get(User, other.id)
        results = await structured_search.query(db, u, field_key="organization", op="contains", value="")
        assert results == [], "a user with no grant on the document must see nothing"
