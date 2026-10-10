"""Phase 2 acceptance: citations resolve, revoked access redacts, unsupported claims surface.

These are the three properties that make an answer trustworthy rather than
merely fluent, so each is a test rather than a hope:

  (a) a stored citation is re-checked against live permissions on every read,
  (b) an answer's sentences are read back against the passages they cite, and
      a check that could not run never reports "nothing wrong",
  (c) a follow-up is resolved into a standalone query before retrieval.

No real provider calls: the LLM is faked throughout.

Integration tests -- require a live Postgres (run inside the stack):
    docker compose exec -T api pytest -q tests/test_answer_trust.py
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete as sql_delete, select

from app.core.db import AsyncSessionLocal
from app.core.security import hash_password
from app.models.chat import Conversation, Message, QAAuditLog
from app.models.constants import PERM_READ, ROLE_VIEWER
from app.models.document import Document, DocumentPermission
from app.models.tenant import User
from app.services import answer_check, chat

# No module-level `pytestmark` -- this file mixes sync and async tests, and
# pytest.ini already sets asyncio_mode=auto so async defs need no marker.


class _FakeLLM:
    """Canned complete_json; records what it was asked."""

    def __init__(self, payload: dict | Exception):
        self.payload = payload
        self.calls: list[list] = []

    def complete_json(self, messages, *, model, **kw):
        self.calls.append(messages)
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload, {"tokens": 0}


# --------------------------------------------------------------- pure logic
def test_cited_indices_deduplicates_in_order():
    assert answer_check.cited_indices("A [2] and B [1] and again [2].") == [2, 1]
    assert answer_check.cited_indices("nothing here") == []


def test_split_sentences_keeps_citation_markers_attached():
    out = answer_check.split_sentences("Rent is 20,000 [1]. Notice is 60 days [2].")
    assert out == ["Rent is 20,000 [1].", "Notice is 60 days [2]."]


def test_short_and_hedging_sentences_are_not_treated_as_claims():
    assert answer_check.is_claim("Yes.") is False
    assert answer_check.is_claim("I couldn't find anything about the renewal terms.") is False
    assert answer_check.is_claim("The agreement runs for eleven months from 1 April 2026.") is True


def test_table_rows_are_checkable_claims():
    """Regression: a comparison answer is a table, and a table row carries a
    claim with no sentence-ending punctuation. Splitting on punctuation alone
    let whole tabular answers past the checker unexamined."""
    answer = (
        "Here is the comparison:\n"
        "| Location | Rent |\n"
        "|----------|------|\n"
        "| Hyderabad | Rs. 30,000 [1] |\n"
        "| Mumbai | Rs. 30,000 |\n"
    )
    units = answer_check.split_sentences(answer)
    assert "|----------|------|" not in units, "the rule row is not a claim"
    assert "| Location | Rent |" not in units, "the header row labels columns, it asserts nothing"

    flagged = answer_check.uncited_claims(answer)
    sentences = [c.sentence for c in flagged]
    assert "| Mumbai | Rs. 30,000 |" in sentences, "an uncited value row must be flagged"
    assert "| Hyderabad | Rs. 30,000 [1] |" not in sentences
    assert "| Location | Rent |" not in sentences, "a header row asserts nothing"


def test_rows_that_only_report_absence_are_not_claims():
    """A row saying "not found" asserts nothing. Note the label cell is a
    filename full of digits — an earlier "has a digit" rule called that data."""
    assert answer_check.row_asserts_value("| 24082399900590.pdf | Mumbai | not found |") is True
    assert answer_check.row_asserts_value("| 24082399900590.pdf | not found | n/a |") is False
    assert answer_check.row_asserts_value("| 24082399900590.pdf | — | |") is False
    assert answer_check.is_claim("| 24082399900590.pdf | not found | not found |") is False


async def test_absence_reports_are_recorded_but_not_raised(monkeypatch):
    """Filling the warning panel with honest "not found" rows teaches the
    reader to ignore the warnings that matter."""
    fake = _FakeLLM({"claims": [
        {"sentence": "24082399900590.pdf: Mumbai: not found", "cited": [1],
         "supported": False, "why": "passages say nothing about the rent"},
        {"sentence": "The rent in Hyderabad is Rs. 99,000 [1].", "cited": [1],
         "supported": False, "why": "passage says 30,000"},
    ]})
    monkeypatch.setattr(answer_check, "is_offline", lambda: False)
    monkeypatch.setattr(answer_check, "get_llm", lambda: fake)

    out = await answer_check.check("irrelevant", "ctx", 1)
    assert len(out["claims"]) == 2, "both stay on the record"
    assert len(out["unsupported"]) == 1, "only the wrong value is raised"
    assert "99,000" in out["unsupported"][0]["sentence"]


def test_bullet_lines_are_checkable_claims():
    answer = "Findings:\n- The rent is Rs. 43,000 per month in Mumbai\n- The deposit is Rs. 125,000 [2]\n"
    flagged = [c.sentence for c in answer_check.uncited_claims(answer)]
    assert any("43,000" in s for s in flagged)
    assert not any("125,000" in s for s in flagged)


def test_uncited_claims_flags_a_confident_sentence_with_no_source():
    answer = ("The monthly rent is 20,000 rupees [1]. "
              "The landlord also agreed to repaint the flat before handover.")
    flagged = answer_check.uncited_claims(answer)
    assert len(flagged) == 1
    assert "repaint" in flagged[0].sentence
    assert flagged[0].supported is False


# ------------------------------------------------------------ answer_check
async def test_check_reports_unchecked_when_offline(monkeypatch):
    """The distinction that matters: "nothing was found wrong" and "nothing was
    looked at" must not render the same way."""
    monkeypatch.setattr(answer_check, "is_offline", lambda: True)
    out = await answer_check.check("A fact with no citation at all in this sentence.", "ctx", 3)
    assert out["checked"] is False
    assert out["source"] == "heuristic"
    assert len(out["unsupported"]) == 1


async def test_check_reports_unchecked_when_the_model_fails(monkeypatch):
    monkeypatch.setattr(answer_check, "is_offline", lambda: False)
    monkeypatch.setattr(answer_check, "get_llm", lambda: _FakeLLM(RuntimeError("provider down")))
    out = await answer_check.check("The rent is 20,000 rupees per month [1].", "ctx", 1)
    assert out["checked"] is False, "a failed check must never read as a clean bill of health"


async def test_check_surfaces_unsupported_claims(monkeypatch):
    fake = _FakeLLM({"claims": [
        {"sentence": "The rent is 20,000 [1].", "cited": [1], "supported": True, "why": "stated"},
        {"sentence": "The lease auto-renews [1].", "cited": [1], "supported": False,
         "why": "passage says nothing about renewal"},
    ]})
    monkeypatch.setattr(answer_check, "is_offline", lambda: False)
    monkeypatch.setattr(answer_check, "get_llm", lambda: fake)

    out = await answer_check.check("The rent is 20,000 [1]. The lease auto-renews [1].", "ctx", 1)
    assert out["checked"] is True and out["source"] == "model"
    assert len(out["unsupported"]) == 1
    assert "auto-renews" in out["unsupported"][0]["sentence"]


async def test_missing_supported_flag_counts_as_unsupported(monkeypatch):
    """A model that garbles or omits the flag should not earn a pass."""
    fake = _FakeLLM({"claims": [{"sentence": "Something confident [1].", "cited": [1]}]})
    monkeypatch.setattr(answer_check, "is_offline", lambda: False)
    monkeypatch.setattr(answer_check, "get_llm", lambda: fake)
    out = await answer_check.check("Something confident [1].", "ctx", 1)
    assert out["unsupported"] and out["unsupported"][0]["supported"] is False


async def test_citation_indices_outside_the_passage_range_are_dropped(monkeypatch):
    fake = _FakeLLM({"claims": [
        {"sentence": "Claim [9].", "cited": [9], "supported": True, "why": ""}]})
    monkeypatch.setattr(answer_check, "is_offline", lambda: False)
    monkeypatch.setattr(answer_check, "get_llm", lambda: fake)
    out = await answer_check.check("Claim [9].", "ctx", passage_count=3)
    assert out["claims"][0]["cited"] == [], "there is no passage 9 to cite"


# --------------------------------------------------- follow-up rewriting
async def test_standalone_question_passes_through_without_history(monkeypatch):
    monkeypatch.setattr(chat, "is_offline", lambda: False)
    monkeypatch.setattr(chat, "get_llm", lambda: _FakeLLM({"query": "should not be used"}))
    assert await chat.standalone_question([], "What is the rent?") == "What is the rent?"


async def test_standalone_question_resolves_a_follow_up(monkeypatch):
    fake = _FakeLLM({"query": "notice period in the Sugavi Nivas rent agreement"})
    monkeypatch.setattr(chat, "is_offline", lambda: False)
    monkeypatch.setattr(chat, "get_llm", lambda: fake)

    history = [Message(role="user", content="Tell me about the Sugavi Nivas rent agreement"),
               Message(role="assistant", content="It is an eleven-month residential lease.")]
    out = await chat.standalone_question(history, "what about the notice period?")

    assert out == "notice period in the Sugavi Nivas rent agreement"
    sent = "\n".join(m.content for m in fake.calls[0])
    assert "Sugavi Nivas" in sent, "the rewriter must see the conversation"


async def test_standalone_question_falls_back_on_a_degenerate_rewrite(monkeypatch):
    """A bad rewrite is worse than none: it silently retrieves for a different
    question than the one that was asked."""
    history = [Message(role="user", content="earlier"), Message(role="assistant", content="reply")]
    monkeypatch.setattr(chat, "is_offline", lambda: False)

    for payload in ({"query": ""}, {"query": "x" * 600}, {}, RuntimeError("boom")):
        monkeypatch.setattr(chat, "get_llm", lambda p=payload: _FakeLLM(p))
        assert await chat.standalone_question(history, "and the rent?") == "and the rent?"


# ------------------------------------------------------------ fixture
@pytest.fixture
async def conversation():
    """A conversation citing two documents; the reader can read only one."""
    async with AsyncSessionLocal() as db:
        tenant_id = uuid.uuid4()
        owner = User(tenant_id=tenant_id, email=f"owner-{uuid.uuid4().hex[:6]}@t.test",
                     hashed_password=hash_password("x" * 10), role=ROLE_VIEWER)
        db.add(owner)
        await db.flush()

        open_doc = Document(tenant_id=tenant_id, owner_id=owner.id,
                            name=f"open-{uuid.uuid4().hex[:4]}.txt", file_type="txt",
                            current_version=1, processing_status="processed")
        closed_doc = Document(tenant_id=tenant_id, owner_id=owner.id,
                              name=f"closed-{uuid.uuid4().hex[:4]}.txt", file_type="txt",
                              current_version=1, processing_status="processed")
        db.add_all([open_doc, closed_doc])
        await db.flush()

        reader = User(tenant_id=tenant_id, email=f"reader-{uuid.uuid4().hex[:6]}@t.test",
                      hashed_password=hash_password("x" * 10), role=ROLE_VIEWER)
        db.add(reader)
        await db.flush()
        # The reader is granted BOTH at first; one grant is revoked mid-test.
        for d in (open_doc, closed_doc):
            db.add(DocumentPermission(tenant_id=tenant_id, document_id=d.id, user_id=reader.id,
                                      level=PERM_READ, granted_by=owner.id))

        conv = Conversation(tenant_id=tenant_id, user_id=reader.id, title="Rent questions")
        db.add(conv)
        await db.flush()
        db.add(Message(conversation_id=conv.id, role="user", content="what is the rent?", citations=[]))
        answer = Message(
            conversation_id=conv.id, role="assistant",
            content="Rent is 20,000 [1] and the deposit is 100,000 [2].",
            citations=[
                {"index": 1, "document_id": str(open_doc.id), "document_name": open_doc.name,
                 "chunk_id": str(uuid.uuid4()), "page": 2, "snippet": "rent 20,000"},
                {"index": 2, "document_id": str(closed_doc.id), "document_name": closed_doc.name,
                 "chunk_id": str(uuid.uuid4()), "page": 5, "snippet": "deposit 100,000"},
            ],
            meta={"coverage": {"documents_searched": 9, "documents_cited": 2, "passages_used": 2}},
        )
        db.add(answer)
        await db.flush()
        db.add(QAAuditLog(tenant_id=tenant_id, user_id=reader.id, conversation_id=conv.id,
                          message_id=answer.id, question="what is the rent?",
                          standalone_question="rent amount", retrieved_chunk_ids=[],
                          answer=answer.content, refused=False, tokens=10))
        await db.commit()

        state = {"tenant_id": tenant_id, "conv_id": conv.id, "reader_id": reader.id,
                 "answer_id": answer.id, "closed_id": closed_doc.id, "open_id": open_doc.id}
        try:
            yield state
        finally:
            async with AsyncSessionLocal() as cleanup:
                await cleanup.execute(sql_delete(QAAuditLog).where(QAAuditLog.tenant_id == tenant_id))
                await cleanup.execute(sql_delete(Conversation).where(Conversation.tenant_id == tenant_id))
                await cleanup.execute(sql_delete(Document).where(Document.tenant_id == tenant_id))
                await cleanup.execute(sql_delete(User).where(User.tenant_id == tenant_id))
                await cleanup.commit()


# ------------------------------------------------ permission re-check
async def test_every_citation_resolves_to_a_readable_document(conversation):
    async with AsyncSessionLocal() as db:
        reader = await db.get(User, conversation["reader_id"])
        conv = await db.get(Conversation, conversation["conv_id"])
        msgs = await chat.readable_history(db, reader, conv)
    answer = next(m for m in msgs if m["role"] == "assistant")
    assert len(answer["citations"]) == 2
    assert answer["citations_redacted"] == 0


async def test_revoked_access_redacts_citations_from_reopened_history(conversation):
    """A citation carries a document name, page and snippet. Access revoked
    after the answer was written must take those with it."""
    async with AsyncSessionLocal() as db:
        await db.execute(sql_delete(DocumentPermission).where(
            DocumentPermission.document_id == conversation["closed_id"]))
        await db.commit()

    async with AsyncSessionLocal() as db:
        reader = await db.get(User, conversation["reader_id"])
        conv = await db.get(Conversation, conversation["conv_id"])
        msgs = await chat.readable_history(db, reader, conv)

    answer = next(m for m in msgs if m["role"] == "assistant")
    assert len(answer["citations"]) == 1
    assert answer["citations"][0]["document_id"] == str(conversation["open_id"])
    assert answer["citations_redacted"] == 1, "the reader must see that something was removed"
    names = [c["document_name"] for c in answer["citations"]]
    assert all("closed-" not in n for n in names)


async def test_stored_caveats_survive_a_reopen(conversation):
    """Coverage travels with the message, so history doesn't read more
    confidently than the answer did when it was given."""
    async with AsyncSessionLocal() as db:
        reader = await db.get(User, conversation["reader_id"])
        conv = await db.get(Conversation, conversation["conv_id"])
        msgs = await chat.readable_history(db, reader, conv)
    answer = next(m for m in msgs if m["role"] == "assistant")
    assert answer["meta"]["coverage"]["documents_searched"] == 9


# ------------------------------------------------------------ feedback
async def test_feedback_lands_on_the_audit_row_for_that_answer(conversation):
    """Feedback belongs on the row that already holds the question, the query
    retrieval used and the chunks it returned -- everything needed to act on
    "this answer was wrong"."""
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(QAAuditLog).where(
            QAAuditLog.message_id == conversation["answer_id"]))).scalar_one()
        assert row.feedback is None
        assert row.standalone_question == "rent amount"
        row.feedback = "down"
        row.feedback_reason = "the deposit figure is from the wrong agreement"
        await db.commit()

    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(QAAuditLog).where(
            QAAuditLog.message_id == conversation["answer_id"]))).scalar_one()
        assert row.feedback == "down"
        assert "wrong agreement" in row.feedback_reason


async def test_deleting_a_conversation_keeps_the_audit_trail(conversation):
    """History tidy-up must not erase the security record of what was asked
    and retrieved: conversation_id is SET NULL, not CASCADE."""
    async with AsyncSessionLocal() as db:
        conv = await db.get(Conversation, conversation["conv_id"])
        await db.delete(conv)
        await db.commit()

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(QAAuditLog).where(
            QAAuditLog.tenant_id == conversation["tenant_id"]))).scalars().all()
        assert len(rows) == 1
        assert rows[0].conversation_id is None
        assert rows[0].question == "what is the rent?"
