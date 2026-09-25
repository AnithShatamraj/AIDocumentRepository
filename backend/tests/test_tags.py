"""Tags: name normalization, permission-filtered listing, filtering, taxonomy edits.

The listing tests are the important ones. A tag is tenant-wide but the documents
carrying it are not, so a tag *name* is itself disclosure: "Project Falcon" in an
autocomplete tells a user the project exists even when every document under it is
closed to them. Same stance the duplicate-upload policy already takes.

Integration tests -- require a live Postgres (run inside the stack):
    docker compose exec -T api pytest -q tests/test_tags.py
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete as sql_delete, select

from app.core.db import AsyncSessionLocal
from app.core.security import hash_password
from app.models.constants import PERM_READ, ROLE_ADMIN, ROLE_VIEWER
from app.models.document import Document, DocumentPermission
from app.models.tag import Tag
from app.models.tenant import User
from app.services import tags as tag_service

# No module-level `pytestmark` — this file mixes sync and async tests, and
# pytest.ini already sets asyncio_mode=auto so async defs need no marker.


async def _mk_user(db, tenant_id, email, role=ROLE_VIEWER):
    u = User(tenant_id=tenant_id, email=email, hashed_password=hash_password("x" * 10), role=role)
    db.add(u)
    await db.flush()
    return u


async def _mk_doc(db, tenant_id, owner, name):
    d = Document(tenant_id=tenant_id, owner_id=owner.id, name=name, file_type="txt",
                 current_version=1, processing_status="processed")
    db.add(d)
    await db.flush()
    return d


# ------------------------------------------------------------- pure logic
def test_normalize_collapses_whitespace_and_trims():
    assert tag_service.normalize("  urgent   review ") == "urgent review"


def test_normalize_rejects_empty_and_overlong():
    for bad in ("", "   ", "\t\n"):
        with pytest.raises(tag_service.TagError):
            tag_service.normalize(bad)
    with pytest.raises(tag_service.TagError):
        tag_service.normalize("x" * (tag_service.MAX_NAME_LEN + 1))


def test_normalize_many_drops_case_insensitive_duplicates_keeping_first():
    # "Urgent"/"urgent"/"URGENT" are one tag -- and the first spelling wins, so
    # the user's own capitalization is what gets stored.
    assert tag_service.normalize_many(["Urgent", "urgent", "URGENT", "Legal"]) == ["Urgent", "Legal"]


def test_normalize_many_caps_tags_per_document():
    with pytest.raises(tag_service.TagError):
        tag_service.normalize_many([f"t{i}" for i in range(tag_service.MAX_TAGS_PER_DOCUMENT + 1)])


# ------------------------------------------------------------- fixture
@pytest.fixture
async def scenario():
    """Owner has two documents; `other` can read only the second one.

    tenant_id is a plain UUID (no local Tenant row) -- same convention as
    test_permissions.py, since a tenant's identity is its own database in the
    real app.
    """
    async with AsyncSessionLocal() as db:
        tenant_id = uuid.uuid4()
        owner = await _mk_user(db, tenant_id, f"owner-{uuid.uuid4().hex[:6]}@t.test")
        other = await _mk_user(db, tenant_id, f"other-{uuid.uuid4().hex[:6]}@t.test")
        admin = await _mk_user(db, tenant_id, f"admin-{uuid.uuid4().hex[:6]}@t.test", role=ROLE_ADMIN)

        secret = await _mk_doc(db, tenant_id, owner, f"secret-{uuid.uuid4().hex[:6]}.txt")
        shared = await _mk_doc(db, tenant_id, owner, f"shared-{uuid.uuid4().hex[:6]}.txt")
        db.add(DocumentPermission(tenant_id=tenant_id, document_id=shared.id,
                                  user_id=other.id, level=PERM_READ, granted_by=owner.id))

        falcon = await tag_service.get_or_create(db, tenant_id=tenant_id, name="Project Falcon",
                                                 created_by=owner.id)
        legal = await tag_service.get_or_create(db, tenant_id=tenant_id, name="Legal",
                                                created_by=owner.id)
        await tag_service.apply_changes(db, user=owner, document_id=secret.id, add=[falcon, legal])
        await tag_service.apply_changes(db, user=owner, document_id=shared.id, add=[legal])
        await db.commit()

        ids = {"tenant_id": tenant_id, "secret": secret.id, "shared": shared.id,
               "owner_id": owner.id, "other_id": other.id, "admin_id": admin.id,
               "falcon_id": falcon.id, "legal_id": legal.id}
        try:
            yield ids
        finally:
            async with AsyncSessionLocal() as cleanup:
                # Documents first (owner_id is ON DELETE RESTRICT); document_tags
                # rows go with them via the database's cascade. Core DELETEs, not
                # session.delete(), so the cascades actually run.
                await cleanup.execute(sql_delete(Document).where(Document.tenant_id == tenant_id))
                await cleanup.execute(sql_delete(Tag).where(Tag.tenant_id == tenant_id))
                await cleanup.execute(sql_delete(User).where(User.tenant_id == tenant_id))
                await cleanup.commit()


# ------------------------------------------------------------- disclosure
async def test_listing_hides_a_tag_whose_documents_are_all_unreadable(scenario):
    async with AsyncSessionLocal() as db:
        other = await db.get(User, scenario["other_id"])
        names = {t["name"] for t in await tag_service.list_with_counts(db, other)}
        assert "Project Falcon" not in names, \
            "a tag name alone discloses that the thing exists"
        assert "Legal" in names, "tags on readable documents must still appear"


async def test_counts_are_per_caller(scenario):
    async with AsyncSessionLocal() as db:
        owner = await db.get(User, scenario["owner_id"])
        other = await db.get(User, scenario["other_id"])
        owner_counts = {t["name"]: t["document_count"] for t in await tag_service.list_with_counts(db, owner)}
        other_counts = {t["name"]: t["document_count"] for t in await tag_service.list_with_counts(db, other)}
        assert owner_counts["Legal"] == 2
        assert other_counts["Legal"] == 1, "the count must reflect what this caller can read"


async def test_include_empty_shows_unused_tags_for_admin(scenario):
    async with AsyncSessionLocal() as db:
        admin = await db.get(User, scenario["admin_id"])
        orphan = await tag_service.get_or_create(
            db, tenant_id=scenario["tenant_id"], name="Unused", created_by=admin.id)
        await db.commit()

        default = {t["name"] for t in await tag_service.list_with_counts(db, admin)}
        with_empty = {t["name"] for t in await tag_service.list_with_counts(db, admin, include_empty=True)}
        assert "Unused" not in default, "a tag no document carries is noise in the picker"
        assert "Unused" in with_empty, "taxonomy management needs to see it to delete it"
        assert orphan.id is not None


async def test_autocomplete_filter_narrows_by_substring(scenario):
    async with AsyncSessionLocal() as db:
        owner = await db.get(User, scenario["owner_id"])
        names = {t["name"] for t in await tag_service.list_with_counts(db, owner, q="falc")}
        assert names == {"Project Falcon"}


# ------------------------------------------------------------- filtering
async def _filter(db, tag_ids, *, match_all):
    cond = tag_service.documents_with_tags_condition(tag_ids, match_all=match_all)
    rows = await db.execute(select(Document.id).where(cond))
    return {r[0] for r in rows.all()}


async def test_match_any_returns_documents_carrying_either(scenario):
    async with AsyncSessionLocal() as db:
        got = await _filter(db, [scenario["falcon_id"], scenario["legal_id"]], match_all=False)
        assert got == {scenario["secret"], scenario["shared"]}


async def test_match_all_requires_every_tag(scenario):
    async with AsyncSessionLocal() as db:
        got = await _filter(db, [scenario["falcon_id"], scenario["legal_id"]], match_all=True)
        assert got == {scenario["secret"]}, "only the document carrying both"


async def test_empty_tag_ids_select_nothing_not_everything(scenario):
    """Fail closed. This function is only reached when a tag filter was asked
    for, so "none of the names resolved" must mean no documents — returning a
    true condition would hand back the whole corpus to someone narrowing it."""
    async with AsyncSessionLocal() as db:
        for match_all in (True, False):
            assert await _filter(db, [], match_all=match_all) == set()


async def test_unknown_tag_name_resolves_to_nothing(scenario):
    async with AsyncSessionLocal() as db:
        found = await tag_service.resolve_names(db, scenario["tenant_id"], ["legal", "nope"])
        # The router short-circuits on this: under match=all an unresolved name
        # means nothing can match, and counting only resolved ids would wrongly
        # let documents through.
        assert set(found) == {"legal"}


async def test_resolve_names_is_case_insensitive(scenario):
    async with AsyncSessionLocal() as db:
        found = await tag_service.resolve_names(db, scenario["tenant_id"], ["PROJECT FALCON"])
        assert found["project falcon"].id == scenario["falcon_id"]


# ------------------------------------------------------------- taxonomy
async def test_get_or_create_is_case_insensitive(scenario):
    async with AsyncSessionLocal() as db:
        again = await tag_service.get_or_create(
            db, tenant_id=scenario["tenant_id"], name="pROJECT fALCON", created_by=scenario["owner_id"])
        assert again.id == scenario["falcon_id"]
        assert again.name == "Project Falcon", "the original capitalization is kept"


async def test_rename_to_an_existing_name_is_refused(scenario):
    async with AsyncSessionLocal() as db:
        falcon = await db.get(Tag, scenario["falcon_id"])
        with pytest.raises(tag_service.TagError):
            await tag_service.rename(db, tag=falcon, new_name="legal")


async def test_rename_allows_a_pure_case_change(scenario):
    async with AsyncSessionLocal() as db:
        legal = await db.get(Tag, scenario["legal_id"])
        await tag_service.rename(db, tag=legal, new_name="LEGAL")
        await db.commit()
        assert (await db.get(Tag, scenario["legal_id"])).name == "LEGAL"


async def test_merge_moves_documents_and_deduplicates(scenario):
    async with AsyncSessionLocal() as db:
        falcon = await db.get(Tag, scenario["falcon_id"])
        legal = await db.get(Tag, scenario["legal_id"])
        # `secret` carries both already, so it must not gain a duplicate link.
        moved = await tag_service.merge(db, source=falcon, target=legal)
        await db.commit()
        assert moved == 0

        assert await db.get(Tag, scenario["falcon_id"]) is None, "source tag is gone"
        still = await tag_service.document_tag_ids(db, scenario["secret"])
        assert still == {scenario["legal_id"]}


async def test_merge_carries_documents_over(scenario):
    async with AsyncSessionLocal() as db:
        owner = await db.get(User, scenario["owner_id"])
        solo = await tag_service.get_or_create(
            db, tenant_id=scenario["tenant_id"], name="Solo", created_by=owner.id)
        await tag_service.apply_changes(db, user=owner, document_id=scenario["shared"], add=[solo])
        await db.commit()

        legal = await db.get(Tag, scenario["legal_id"])
        moved = await tag_service.merge(db, source=solo, target=legal)
        await db.commit()
        assert moved == 0, "shared already carried Legal"

        # And the other direction: a document carrying only the source moves.
        spare = await tag_service.get_or_create(
            db, tenant_id=scenario["tenant_id"], name="Spare", created_by=owner.id)
        target = await tag_service.get_or_create(
            db, tenant_id=scenario["tenant_id"], name="Target", created_by=owner.id)
        await tag_service.apply_changes(db, user=owner, document_id=scenario["secret"], add=[spare])
        await db.commit()
        moved = await tag_service.merge(db, source=spare, target=target)
        await db.commit()
        assert moved == 1
        assert target.id in await tag_service.document_tag_ids(db, scenario["secret"])


async def test_set_document_tags_replaces_the_whole_list(scenario):
    async with AsyncSessionLocal() as db:
        owner = await db.get(User, scenario["owner_id"])
        await tag_service.set_document_tags(
            db, user=owner, document_id=scenario["secret"], names=["Legal", "Fresh"])
        await db.commit()
        names = {(await db.get(Tag, tid)).name
                 for tid in await tag_service.document_tag_ids(db, scenario["secret"])}
        assert names == {"Legal", "Fresh"}, "Project Falcon was dropped by omission"


async def test_updatable_document_ids_excludes_read_only_grants(scenario):
    async with AsyncSessionLocal() as db:
        other = await db.get(User, scenario["other_id"])
        allowed = await tag_service.updatable_document_ids(
            db, other, [scenario["secret"], scenario["shared"]])
        assert allowed == set(), "a read grant does not authorize tagging"

        owner = await db.get(User, scenario["owner_id"])
        allowed = await tag_service.updatable_document_ids(
            db, owner, [scenario["secret"], scenario["shared"]])
        assert allowed == {scenario["secret"], scenario["shared"]}
