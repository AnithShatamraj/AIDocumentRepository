"""AI-assisted document type builder.

The pure helpers are unit-tested directly. The guided workflow is tested
end-to-end against a real database (like test_permissions.py) with the LLM
layer replaced by canned answers -- these tests must never call a model.
"""
from __future__ import annotations

import datetime as dt
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import delete as sql_delete
from sqlalchemy import select

from app.ai import type_builder as ai
from app.ai.types import LLMResult
from app.core.db import AsyncSessionLocal
from app.core.security import hash_password
from app.models.catalog import DocumentType
from app.models.constants import ROLE_ADMIN
from app.models.ops import AuditLog, CostRecord
from app.models.tenant import User
from app.models.type_draft import DocumentTypeDraft, DocumentTypeDraftSample
from app.services import fields as fieldsvc
from app.services import type_builder as tb

# --------------------------------------------------------------- heuristics


@pytest.mark.parametrize("text", ["yes", "Yes!", "looks good", "That's right", "ok", "LGTM", "yes please",
                                  "Yep, that works"])
def test_affirmative(text):
    assert tb.is_affirmative(text)


@pytest.mark.parametrize("text", ["yes but also mention taxes", "add a due date", "no", "change the name",
                                  "I think it should cover credit notes as well"])
def test_not_affirmative(text):
    assert not tb.is_affirmative(text)


@pytest.mark.parametrize("text", ["no", "No thanks", "skip", "I don't have any", "no samples right now"])
def test_negative(text):
    assert tb.is_negative(text)


def test_not_negative():
    assert not tb.is_negative("yes I have a few")
    assert not tb.is_negative("Notice of Termination")  # starts with "no" but isn't a refusal


def test_custom_name_extraction():
    assert tb.extract_custom_name("Call it Purchase Order") == "Purchase Order"
    assert tb.extract_custom_name("let's name it 'Vendor Invoice'.") == "Vendor Invoice"
    assert tb.extract_custom_name("Residential Rent Agreement") == "Residential Rent Agreement"
    assert tb.wants_another_name("suggest another one")
    assert not tb.wants_another_name("Purchase Order")


@pytest.mark.parametrize("text,expected", [
    ("Actually, call it Residential Lease Deed", "Residential Lease Deed"),
    ("I'd like to call it Lease Deed", "Lease Deed"),
    ("Well, let's go with Rental Contract", "Rental Contract"),
    ('Please go with "Lease Deed" for now', "Lease Deed"),
    ("the name is Tenancy Agreement", "Tenancy Agreement"),
    # real names that merely start with a filler-ish word must survive untouched
    ("Well Inspection Report", "Well Inspection Report"),
    ("No Objection Certificate", "No Objection Certificate"),
    ("Land Use Permit", "Land Use Permit"),
    ("Use Permit", "Use Permit"),
])
def test_custom_name_tolerates_chatter_but_not_real_names(text, expected):
    assert tb.extract_custom_name(text) == expected


# ------------------------------------------------------------- field repair
def test_repair_fills_missing_list_item_name_and_validates():
    raw = [{"name": "Line Items", "description": "rows", "data_type": "list",
            "item": {"data_type": "object", "fields": [
                {"name": "Qty", "data_type": "integer"}, {"name": "Amount", "data_type": "currency"}]}},
           {"name": "Skills", "data_type": "list", "item": {"data_type": "string"}}]
    out = ai._try_validate(raw)
    assert out[0]["item"]["name"] == "Line Items item"
    assert [f["key"] for f in out[0]["item"]["fields"]] == ["qty", "amount"]
    assert out[1]["item"]["data_type"] == "string"


def test_repair_does_not_paper_over_real_errors():
    with pytest.raises(fieldsvc.SchemaError):
        ai._try_validate([{"name": "Empty group", "data_type": "object", "fields": []}])
    with pytest.raises(fieldsvc.SchemaError):
        ai._try_validate([])


# ------------------------------------------------------------- summarising
def test_summarize_counts_found_missing_and_low_confidence():
    defs = fieldsvc.validate_schema(fieldsvc.ensure_keys([
        {"name": "Invoice Number", "data_type": "string"},
        {"name": "Due Date", "data_type": "date"},
        {"name": "Items", "data_type": "list", "item": {"name": "Item", "data_type": "object", "fields": [
            {"name": "Desc", "data_type": "string"}]}},
    ]))
    payload = {"invoice_number": {"value": "INV-1", "confidence": 0.95, "quote": "INV-1"},
               "due_date": {"value": None, "confidence": 0.0, "quote": None},
               "items": [{"desc": {"value": "Bolts", "confidence": 0.4, "quote": "Bolts"}}]}
    r = tb._summarize(defs, fieldsvc.flatten_response(defs, payload), "a.txt", "sid")
    assert (r["found"], r["total"]) == (2, 3)
    assert r["missing"] == ["Due Date"]
    assert r["low_confidence"] == ["Desc"]


def test_a_stuck_sample_is_reported_as_failed():
    s = DocumentTypeDraftSample(id=uuid.uuid4(), draft_id=uuid.uuid4(), filename="x.pdf", file_type="pdf",
                                file_size=1, status="processing", text="")
    s.created_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)
    out = tb.sample_out(s)
    assert out["status"] == "failed" and "timed out" in out["error"]


# ---------------------------------------------------------------- workflow
def _usage() -> list[LLMResult]:
    return [LLMResult(text="{}", provider="fake", model="fake", prompt_tokens=1, completion_tokens=1)]


FIELDS = [{"key": "invoice_number", "name": "Invoice Number", "description": "", "data_type": "string",
           "required": False, "fields": []}]


@pytest.fixture
def fake_ai(monkeypatch):
    """Canned answers; records what the workflow asked the AI."""
    calls: dict = {"describe": [], "name": [], "fields": [], "review": [], "unrelated": False}

    def refine(history, current, message, samples=None):
        calls["describe"].append((current, message, [s[0] for s in samples or []]))
        return ai.DescriptionResult(description=f"Refined: {message}", reply="Here's the description.",
                                    confirmed=False, usage=_usage())

    def suggest(description, taken):
        calls["name"].append(list(taken))
        n = 1
        while f"Suggested {n}" in taken:
            n += 1
        return f"Suggested {n}", _usage()

    def design(*, name, description, current, samples, request=None, validation_hint=""):
        calls["fields"].append({"request": request, "samples": [s[0] for s in samples], "current": current})
        return ai.FieldsResult(fields=FIELDS, reply="Done.", usage=_usage())

    def review(samples):
        """Related by default; set calls['unrelated'] to make every sample its own kind
        (the last sample is a different kind from the rest when there are >= 3)."""
        calls["review"].append([s[0] for s in samples])
        if not calls["unrelated"]:
            docs = [ai.SampleDoc(kind="a purchase order", group=1) for _ in samples]
            return ai.SampleReview(related=True, summary="", documents=docs, usage=_usage())
        docs = [ai.SampleDoc(kind=f"a {name} document", group=1 if i < len(samples) - 1 else 2)
                for i, (name, _) in enumerate(samples)]
        return ai.SampleReview(related=False, summary="One of these is a different kind of document.",
                               documents=docs, usage=_usage())

    monkeypatch.setattr(tb.ai, "refine_description", refine)
    monkeypatch.setattr(tb.ai, "suggest_name", suggest)
    monkeypatch.setattr(tb.ai, "design_fields", design)
    monkeypatch.setattr(tb.ai, "review_samples", review)
    return calls


@pytest.fixture
async def admin():
    tenant_id = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        u = User(tenant_id=tenant_id, email=f"adm-{uuid.uuid4().hex[:6]}@t.test",
                 hashed_password=hash_password("x" * 10), role=ROLE_ADMIN)
        db.add(u)
        await db.commit()
    try:
        yield u
    finally:
        async with AsyncSessionLocal() as db:
            # Core DELETEs so Postgres ON DELETE CASCADE removes messages/samples.
            for model in (DocumentTypeDraft, CostRecord, AuditLog, DocumentType):
                await db.execute(sql_delete(model).where(model.tenant_id == tenant_id))
            await db.execute(sql_delete(User).where(User.id == u.id))
            await db.commit()


async def _turn(db, draft, admin, **kw):
    await tb.handle_turn(db, draft, admin, content=kw.get("content"), action=kw.get("action"),
                         label=kw.get("label"))


async def _last(db, draft):
    return (await tb.list_messages(db, draft.id))[-1]


@pytest.mark.asyncio
async def test_guided_flow_end_to_end(admin, fake_ai):
    async with AsyncSessionLocal() as db:
        d = await tb.create_draft(db, admin)
        await db.commit()
        assert d.stage == "describe"
        assert (await tb.list_messages(db, d.id))[0].role == "assistant"

        # describe -> AI proposes a description; it is not confirmed yet
        await _turn(db, d, admin, content="Vendor invoices")
        assert d.description == "Refined: Vendor invoices" and not d.description_confirmed
        assert (await _last(db, d)).payload["kind"] == "description_proposal"

        # feedback refines it (the AI sees the current text)
        await _turn(db, d, admin, content="also credit notes")
        assert fake_ai["describe"][-1][0] == "Refined: Vendor invoices"

        # confirm -> a name is proposed
        await _turn(db, d, admin, action="confirm_description")
        assert d.stage == "name" and d.description_confirmed and d.name == "Suggested 1"
        assert (await _last(db, d)).payload["kind"] == "name_proposal"

        # ask for another, then override with the admin's own name
        await _turn(db, d, admin, action="suggest_name")
        assert d.name == "Suggested 2" and "Suggested 1" in fake_ai["name"][-1]  # doesn't repeat itself
        await _turn(db, d, admin, content="Call it Vendor Invoice")
        assert d.stage == "samples" and d.name == "Vendor Invoice" and d.name_confirmed

        # no samples -> fields are derived from the description alone
        await _turn(db, d, admin, content="no")
        assert d.stage == "fields" and d.fields == FIELDS
        assert fake_ai["fields"][-1]["samples"] == []

        # free text now refines the fields, passing the admin's request through
        await _turn(db, d, admin, content="add a due date")
        assert fake_ai["fields"][-1]["request"] == "add a due date"

        dt_ = await tb.publish(db, d, admin)
        assert dt_.name == "Vendor Invoice" and dt_.active_schema_id
        assert d.status == "published" and d.published_type_id == dt_.id
        row = (await db.execute(select(DocumentType).where(DocumentType.id == dt_.id))).scalar_one()
        assert row.description == d.description

    async with AsyncSessionLocal() as db:  # AI usage was recorded for cost reporting
        recs = (await db.execute(select(CostRecord).where(CostRecord.tenant_id == admin.tenant_id))).scalars().all()
        assert recs and all(c.stage == "type_builder" for c in recs)


@pytest.mark.asyncio
async def test_approving_message_confirms_description_without_calling_the_ai(admin, fake_ai):
    async with AsyncSessionLocal() as db:
        d = await tb.create_draft(db, admin)
        await _turn(db, d, admin, content="Purchase orders")
        calls = len(fake_ai["describe"])
        await _turn(db, d, admin, content="looks good")
        assert d.stage == "name" and len(fake_ai["describe"]) == calls


@pytest.mark.asyncio
async def test_name_conflict_keeps_the_admin_on_the_name_step(admin, fake_ai):
    async with AsyncSessionLocal() as db:
        db.add(DocumentType(tenant_id=admin.tenant_id, name="Contracts", description=""))
        await db.commit()
        d = await tb.create_draft(db, admin)
        await _turn(db, d, admin, content="Agreements")
        await _turn(db, d, admin, action="confirm_description")
        await _turn(db, d, admin, content="contracts")  # same name, different case
        assert d.stage == "name" and not d.name_confirmed
        assert (await _last(db, d)).payload["kind"] == "name_conflict"
        assert "Contracts" in fake_ai["name"][0]  # existing names are kept out of suggestions


@pytest.mark.asyncio
async def test_stale_and_invalid_actions_are_handled_gracefully(admin, fake_ai):
    async with AsyncSessionLocal() as db:
        d = await tb.create_draft(db, admin)
        await _turn(db, d, admin, action="accept_name")  # nothing to accept yet
        assert d.stage == "describe" and (await _last(db, d)).payload["kind"] == "info"
        await _turn(db, d, admin, action="confirm_description")  # no description yet
        assert d.stage == "describe"
        with pytest.raises(tb.DraftError) as e:
            await _turn(db, d, admin, action="drop_tables")
        assert e.value.status_code == 400
        with pytest.raises(tb.DraftError):
            await _turn(db, d, admin)  # neither text nor action


@pytest.mark.asyncio
async def test_ai_failure_becomes_a_chat_message_not_an_error(admin, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(tb.ai, "refine_description", boom)
    monkeypatch.setattr(tb.ai, "design_fields", boom)
    async with AsyncSessionLocal() as db:
        d = await tb.create_draft(db, admin)
        await _turn(db, d, admin, content="Invoices")
        last = await _last(db, d)
        assert last.role == "assistant" and last.payload["kind"] == "error"
        msgs = await tb.list_messages(db, d.id)
        assert msgs[-2].role == "user"  # the admin's turn was kept

        # a failure in the samples step still leaves buttons to retry with
        d.stage = "samples"
        db.add(DocumentTypeDraftSample(draft_id=d.id, filename="a.txt", file_type="txt", file_size=1,
                                       status="ready", text="x"))
        await db.commit()
        await _turn(db, d, admin, action="derive_fields")
        last = await _last(db, d)
        assert last.payload["kind"] == "error" and d.stage == "samples"
        assert [a["action"] for a in last.payload["actions"]] == ["derive_fields", "upload_samples", "skip_samples"]


@pytest.mark.asyncio
async def test_publish_is_blocked_until_the_guided_steps_are_done(admin, fake_ai):
    async with AsyncSessionLocal() as db:
        d = await tb.create_draft(db, admin)
        d.name, d.fields = "Early", FIELDS
        with pytest.raises(tb.DraftError) as e:
            await tb.publish(db, d, admin)
        assert e.value.status_code == 409


@pytest.mark.asyncio
async def test_publish_rejects_invalid_fields_and_taken_names(admin, fake_ai):
    async with AsyncSessionLocal() as db:
        d = await tb.create_draft(db, admin)
        d.stage, d.name = "fields", "Broken"
        d.fields = [{"name": "Group", "data_type": "object", "fields": []}]
        with pytest.raises(tb.DraftError) as e:
            await tb.publish(db, d, admin)
        assert e.value.status_code == 422

        d.fields = FIELDS
        db.add(DocumentType(tenant_id=admin.tenant_id, name="broken", description=""))
        await db.commit()
        with pytest.raises(tb.DraftError) as e:
            await tb.publish(db, d, admin)
        assert e.value.status_code == 409 and d.status == "draft"


@pytest.mark.asyncio
async def test_samples_are_parsed_and_feed_field_design(admin, fake_ai, monkeypatch):
    @asynccontextmanager
    async def own_session(_tenant):
        async with AsyncSessionLocal() as s:
            yield s

    monkeypatch.setattr(tb, "_tenant_session", own_session)
    async with AsyncSessionLocal() as db:
        d = await tb.create_draft(db, admin)
        d.stage = "samples"
        good = DocumentTypeDraftSample(draft_id=d.id, filename="a.txt", file_type="txt", file_size=11)
        empty = DocumentTypeDraftSample(draft_id=d.id, filename="b.txt", file_type="txt", file_size=1)
        db.add_all([good, empty])
        await db.commit()

        await tb.process_sample(None, good.id, b"invoice 123", "txt")
        await tb.process_sample(None, empty.id, b" ", "txt")
        await db.refresh(good)
        await db.refresh(empty)
        assert good.status == "ready" and good.text == "invoice 123"
        assert empty.status == "failed" and empty.error

        await _turn(db, d, admin, action="derive_fields")
        assert d.stage == "fields"
        assert fake_ai["fields"][-1]["samples"] == ["a.txt"]  # only the sample that parsed


async def _add_sample(db, draft, name="a.txt", status="ready", text="sample text"):
    s = DocumentTypeDraftSample(draft_id=draft.id, filename=name, file_type="txt", file_size=len(text),
                                status=status, text=text if status == "ready" else "")
    db.add(s)
    await db.commit()
    return s


@pytest.mark.asyncio
async def test_samples_first_flow_skips_the_samples_question(admin, fake_ai):
    """Upload in the very first step -> description from the samples -> confirm ->
    name -> fields, with the 'do you have samples?' step never asked."""
    async with AsyncSessionLocal() as db:
        d = await tb.create_draft(db, admin)
        await _add_sample(db, d, "a.txt")
        await _add_sample(db, d, "b.txt")

        await _turn(db, d, admin, action="describe_from_samples")
        assert d.stage == "describe" and d.description.startswith("Refined:")
        assert not d.description_confirmed  # the admin still confirms it
        assert sorted(fake_ai["describe"][-1][2]) == ["a.txt", "b.txt"]
        last = await _last(db, d)
        assert last.payload["kind"] == "description_proposal"
        assert [a["action"] for a in last.payload["actions"]] == ["confirm_description"]
        assert "only have one sample" not in last.content  # two samples: no over-fitting caveat

        await _turn(db, d, admin, action="confirm_description")
        assert d.stage == "name" and d.name == "Suggested 1"

        await _turn(db, d, admin, action="accept_name")
        assert d.stage == "fields" and d.name_confirmed and d.fields == FIELDS
        assert sorted(fake_ai["fields"][-1]["samples"]) == ["a.txt", "b.txt"]
        kinds = [m.payload["kind"] for m in await tb.list_messages(db, d.id) if m.payload]
        assert "samples_prompt" not in kinds


@pytest.mark.asyncio
async def test_a_single_sample_gets_an_overfitting_caveat(admin, fake_ai):
    async with AsyncSessionLocal() as db:
        d = await tb.create_draft(db, admin)
        await _add_sample(db, d)
        await _turn(db, d, admin, action="describe_from_samples")
        assert "only have one sample" in (await _last(db, d)).content


@pytest.mark.asyncio
async def test_describe_from_samples_needs_a_ready_sample(admin, fake_ai):
    async with AsyncSessionLocal() as db:
        d = await tb.create_draft(db, admin)
        await _add_sample(db, d, status="processing")
        await _turn(db, d, admin, action="describe_from_samples")
        assert not fake_ai["describe"] and not d.description and d.stage == "describe"
        assert (await _last(db, d)).payload["kind"] == "info"


@pytest.mark.asyncio
async def test_describe_from_samples_is_a_stale_action_after_the_description_step(admin, fake_ai):
    async with AsyncSessionLocal() as db:
        d = await tb.create_draft(db, admin)
        d.stage, d.description = "name", "x"
        await _add_sample(db, d)
        await _turn(db, d, admin, action="describe_from_samples")
        assert not fake_ai["describe"] and d.description == "x"


@pytest.mark.asyncio
async def test_description_feedback_stays_grounded_in_the_samples(admin, fake_ai):
    async with AsyncSessionLocal() as db:
        d = await tb.create_draft(db, admin)
        await _add_sample(db, d, "a.txt")
        await _turn(db, d, admin, action="describe_from_samples")
        await _turn(db, d, admin, content="also mention purchase orders")
        assert fake_ai["describe"][-1][1] == "also mention purchase orders"
        assert fake_ai["describe"][-1][2] == ["a.txt"]


@pytest.mark.asyncio
async def test_describe_from_samples_offline_asks_for_words(admin, fake_ai, monkeypatch):
    monkeypatch.setattr(tb, "is_offline", lambda: True)
    async with AsyncSessionLocal() as db:
        d = await tb.create_draft(db, admin)
        await _add_sample(db, d)
        await _turn(db, d, admin, action="describe_from_samples")
        assert not fake_ai["describe"] and not d.description
        assert "own words" in (await _last(db, d)).content


@pytest.mark.asyncio
async def test_a_failed_sample_does_not_skip_the_samples_question(admin, fake_ai):
    """Samples were uploaded but none could be read: the flow falls back to asking."""
    async with AsyncSessionLocal() as db:
        d = await tb.create_draft(db, admin)
        await _add_sample(db, d, status="failed")
        await _turn(db, d, admin, content="Invoices")
        await _turn(db, d, admin, action="confirm_description")
        await _turn(db, d, admin, action="accept_name")
        assert d.stage == "samples" and not fake_ai["fields"]


# ------------------------------------------------- samples that don't belong together
@pytest.mark.parametrize("text", ["yes", "Yes they are the same", "yep, same type", "they're all the same",
                                  "continue", "ok go ahead"])
def test_confirming_samples_in_words(text):
    assert tb.confirms_samples(text)


@pytest.mark.parametrize("text", ["no", "not the same", "yes but remove the resume", "they are different",
                                  "oops wrong file", "remove b.txt", "", "yes " + "really " * 12])
def test_not_confirming_samples(text):
    assert not tb.confirms_samples(text)


def test_outliers_are_the_documents_outside_the_biggest_group():
    docs = [{"group": 1, "outlier": False}, {"group": 1, "outlier": False}, {"group": 2, "outlier": False}]
    tb._mark_outliers(docs)
    assert [d["outlier"] for d in docs] == [False, False, True]
    tie = [{"group": 1, "outlier": False}, {"group": 2, "outlier": False}]
    tb._mark_outliers(tie)
    assert [d["outlier"] for d in tie] == [False, True]  # a tie: the lower-numbered group is the reference


@pytest.mark.asyncio
async def test_unrelated_samples_pause_the_description_step_and_explain_each_file(admin, fake_ai):
    fake_ai["unrelated"] = True
    async with AsyncSessionLocal() as db:
        d = await tb.create_draft(db, admin)
        for n in ("po.txt", "po2.txt", "resume.txt"):
            await _add_sample(db, d, n)

        await _turn(db, d, admin, action="describe_from_samples")

        assert not fake_ai["describe"] and not d.description and d.stage == "describe"  # paused, nothing written
        last = await _last(db, d)
        assert last.payload["kind"] == "sample_mismatch"
        assert [a["action"] for a in last.payload["actions"]] == ["confirm_samples", "upload_samples"]
        assert "don't look like the same kind" in last.content
        assert "• resume.txt — a resume.txt document  (looks different)" in last.content  # which one is what
        assert "• po.txt — a po.txt document\n" in last.content
        assert [x["outlier"] for x in last.payload["documents"]] == [False, False, True]
        assert d.sample_check["related"] is False and d.sample_check["confirmed"] is False


@pytest.mark.asyncio
async def test_confirming_unrelated_samples_carries_on_without_asking_again(admin, fake_ai):
    fake_ai["unrelated"] = True
    async with AsyncSessionLocal() as db:
        d = await tb.create_draft(db, admin)
        await _add_sample(db, d, "a.txt")
        await _add_sample(db, d, "b.txt")
        await _turn(db, d, admin, action="describe_from_samples")
        assert not fake_ai["describe"]

        await _turn(db, d, admin, action="confirm_samples")
        assert d.sample_check["confirmed"] is True
        assert d.description.startswith("Refined:")  # the paused step ran
        assert sorted(fake_ai["describe"][-1][2]) == ["a.txt", "b.txt"]
        assert len(fake_ai["review"]) == 1  # the verdict was kept, not recomputed

        # ...and the confirmation carries through to fields (same set of samples)
        await _turn(db, d, admin, action="confirm_description")
        await _turn(db, d, admin, action="accept_name")
        assert d.stage == "fields" and len(fake_ai["review"]) == 1
        assert sorted(fake_ai["fields"][-1]["samples"]) == ["a.txt", "b.txt"]


@pytest.mark.asyncio
async def test_typing_yes_after_the_warning_counts_as_confirming(admin, fake_ai):
    fake_ai["unrelated"] = True
    async with AsyncSessionLocal() as db:
        d = await tb.create_draft(db, admin)
        await _add_sample(db, d, "a.txt")
        await _add_sample(db, d, "b.txt")
        await _turn(db, d, admin, action="describe_from_samples")
        await _turn(db, d, admin, content="yes they are the same")
        assert d.sample_check["confirmed"] and d.description.startswith("Refined:")


@pytest.mark.asyncio
async def test_removing_the_odd_sample_out_lets_the_step_proceed(admin, fake_ai):
    fake_ai["unrelated"] = True
    async with AsyncSessionLocal() as db:
        d = await tb.create_draft(db, admin)
        await _add_sample(db, d, "a.txt")
        odd = await _add_sample(db, d, "odd.txt")
        await _turn(db, d, admin, action="describe_from_samples")
        assert not d.description

        await db.delete(odd)
        await db.commit()
        await _turn(db, d, admin, action="describe_from_samples")  # re-check: one sample can't be a mismatch
        assert d.description.startswith("Refined:") and len(fake_ai["review"]) == 1


@pytest.mark.asyncio
async def test_changing_the_sample_set_invalidates_an_earlier_confirmation(admin, fake_ai):
    async with AsyncSessionLocal() as db:
        d = await tb.create_draft(db, admin)
        await _add_sample(db, d, "a.txt")
        await _add_sample(db, d, "b.txt")
        fake_ai["unrelated"] = True
        await _turn(db, d, admin, action="describe_from_samples")
        await _turn(db, d, admin, action="confirm_samples")
        assert d.sample_check["confirmed"]

        await _add_sample(db, d, "c.txt")  # a new file: the earlier verdict said nothing about it
        d.stage, d.description = "samples", "x"
        await _turn(db, d, admin, action="derive_fields")
        assert len(fake_ai["review"]) == 2 and not d.sample_check["confirmed"]
        assert (await _last(db, d)).payload["kind"] == "sample_mismatch"


@pytest.mark.asyncio
async def test_unrelated_samples_uploaded_alongside_a_typed_description_are_caught_at_the_name_step(admin, fake_ai):
    """Description typed by hand, samples added without ever running the description-from-samples
    step: the first place the AI would read them (after the name) must still check them."""
    fake_ai["unrelated"] = True
    async with AsyncSessionLocal() as db:
        d = await tb.create_draft(db, admin)
        await _add_sample(db, d, "a.txt")
        await _add_sample(db, d, "b.txt")
        await _turn(db, d, admin, content="Purchase orders")
        await _turn(db, d, admin, action="confirm_description")
        await _turn(db, d, admin, action="accept_name")

        assert d.stage == "samples" and d.name_confirmed and not fake_ai["fields"]  # held, not designed
        last = await _last(db, d)
        assert last.payload["kind"] == "sample_mismatch" and last.content.startswith("Named **Suggested 1**.")

        await _turn(db, d, admin, action="confirm_samples")
        assert d.stage == "fields" and sorted(fake_ai["fields"][-1]["samples"]) == ["a.txt", "b.txt"]


@pytest.mark.asyncio
async def test_an_unchecked_or_flagged_set_is_never_fed_to_free_text_refinement(admin, fake_ai):
    async with AsyncSessionLocal() as db:
        d = await tb.create_draft(db, admin)
        await _add_sample(db, d, "a.txt")
        await _add_sample(db, d, "b.txt")
        await _turn(db, d, admin, content="Purchase orders")  # two samples, never checked
        assert fake_ai["describe"][-1][2] == []


@pytest.mark.asyncio
async def test_a_failing_review_never_blocks_the_admin(admin, fake_ai, monkeypatch):
    def boom(samples):
        raise RuntimeError("provider down")

    monkeypatch.setattr(tb.ai, "review_samples", boom)
    async with AsyncSessionLocal() as db:
        d = await tb.create_draft(db, admin)
        await _add_sample(db, d, "a.txt")
        await _add_sample(db, d, "b.txt")
        await _turn(db, d, admin, action="describe_from_samples")
        assert d.description.startswith("Refined:")  # proceeded


@pytest.mark.asyncio
async def test_skip_samples_really_ignores_the_samples(admin, fake_ai):
    async with AsyncSessionLocal() as db:
        d = await tb.create_draft(db, admin)
        d.stage, d.name, d.description = "samples", "T", "x"
        await _add_sample(db, d, "a.txt")
        await _turn(db, d, admin, action="skip_samples")
        assert d.stage == "fields" and fake_ai["fields"][-1]["samples"] == []


@pytest.mark.asyncio
async def test_draft_detail_only_reports_a_verdict_about_the_current_samples(admin, fake_ai):
    fake_ai["unrelated"] = True
    async with AsyncSessionLocal() as db:
        d = await tb.create_draft(db, admin)
        a = await _add_sample(db, d, "a.txt")
        await _add_sample(db, d, "b.txt")
        await _turn(db, d, admin, action="describe_from_samples")
        detail = await tb.draft_detail(db, d)
        assert detail["sample_check"]["related"] is False and len(detail["sample_check"]["documents"]) == 2

        await db.delete(a)
        await db.commit()
        assert (await tb.draft_detail(db, d))["sample_check"] is None  # stale: about a set that no longer exists


@pytest.mark.asyncio
async def test_derive_without_a_ready_sample_does_not_advance(admin, fake_ai):
    async with AsyncSessionLocal() as db:
        d = await tb.create_draft(db, admin)
        d.stage = "samples"
        db.add(DocumentTypeDraftSample(draft_id=d.id, filename="a.txt", file_type="txt", file_size=1))
        await db.commit()
        await _turn(db, d, admin, action="derive_fields")
        assert d.stage == "samples" and not fake_ai["fields"]


@pytest.mark.asyncio
async def test_validation_reports_missing_fields_and_saves_results(admin, fake_ai, monkeypatch):
    payload = {"invoice_number": {"value": None, "confidence": 0.0, "quote": None}}
    monkeypatch.setattr("app.ai.agents.structured.extract_structured",
                        lambda text, defs, name: (payload, _usage()[0]))
    monkeypatch.setattr(tb, "is_offline", lambda: False)
    async with AsyncSessionLocal() as db:
        d = await tb.create_draft(db, admin)
        d.stage, d.name, d.fields = "fields", "T", FIELDS
        s = DocumentTypeDraftSample(draft_id=d.id, filename="a.txt", file_type="txt", file_size=5,
                                    status="ready", text="hello")
        db.add(s)
        await db.commit()

        await tb.run_validation(db, d, admin, None)
        r = d.last_validation[str(s.id)]
        assert (r["found"], r["total"], r["missing"]) == (0, 1, ["Invoice Number"])
        assert "Not found: Invoice Number" in (await _last(db, d)).content

        d.fields = [{"name": "Group", "data_type": "object", "fields": []}]
        with pytest.raises(tb.DraftError) as e:
            await tb.run_validation(db, d, admin, None)
        assert e.value.status_code == 422


@pytest.mark.asyncio
async def test_validation_needs_an_ai_provider(admin, monkeypatch):
    monkeypatch.setattr(tb, "is_offline", lambda: True)
    async with AsyncSessionLocal() as db:
        d = await tb.create_draft(db, admin)
        with pytest.raises(tb.DraftError) as e:
            await tb.run_validation(db, d, admin, None)
        assert e.value.status_code == 400
