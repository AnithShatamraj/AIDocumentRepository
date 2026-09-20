"""Workflow for the AI-assisted document-type builder.

A draft moves through fixed stages -- describe -> name -> samples -> fields --
and every user turn is either an explicit *action* (a button in the chat) or
free text interpreted according to the current stage:

    describe  free text -> AI restructures/refines the description; the admin
                           confirms it (button, or an approving message).
                           Alternatively the admin uploads sample documents
                           first and the AI writes the description from them.
    name      the AI suggests one; the admin accepts, asks for another, or
                           types their own
    samples   the admin uploads sample documents or skips (skipped entirely
                           when samples were already provided in `describe`)
    fields    the AI derives the field tree (from the description and any
                           samples); free text refines it, and the admin can
                           test it against a sample before publishing

Button actions are the deterministic path; free text is a convenience layered
on top (stage-aware, mostly heuristics -- the LLM is only called for the
description and the fields, where its judgement is actually needed).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import re
import uuid
from collections import Counter
from contextlib import asynccontextmanager

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai import type_builder as ai
from app.ai.registry import is_offline
from app.ai.types import LLMResult
from app.core.logging import get_logger
from app.core.tenant_db import get_tenant_async_engine
from app.models.catalog import DocumentType
from app.models.constants import AUDIT_CONFIG_CHANGE
from app.models.ops import CostRecord
from app.models.type_draft import (
    DRAFT_ACTIVE,
    DRAFT_PUBLISHED,
    SAMPLE_FAILED,
    SAMPLE_PROCESSING,
    SAMPLE_READY,
    STAGE_DESCRIBE,
    STAGE_FIELDS,
    STAGE_NAME,
    STAGE_SAMPLES,
    DocumentTypeDraft,
    DocumentTypeDraftMessage,
    DocumentTypeDraftSample,
)
from app.services import audit, parsing
from app.services import fields as fieldsvc
from app.services.type_catalog import NameTaken, create_document_type, name_exists

log = get_logger(__name__)

MAX_SAMPLES = 5
MAX_SAMPLE_CHARS = 200_000
SAMPLE_STUCK_AFTER = dt.timedelta(minutes=10)
LOW_CONFIDENCE = 0.6
MAX_DESCRIPTION_STORED = 2048  # DocumentType.description is String(2048)


class DraftError(Exception):
    """A request the workflow refuses; the router turns it into an HTTP error."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


# ------------------------------------------------------------------ wording
# Actions POST /messages accepts, and the text shown as the admin's own chat
# bubble when they click the matching button.
ACTION_LABELS = {
    "describe_from_samples": "Write the description from my samples",
    "confirm_samples": "They're the same type — continue",
    "confirm_description": "Confirm description",
    "accept_name": "Use this name",
    "suggest_name": "Suggest another name",
    "skip_samples": "No samples — skip",
    "derive_fields": "Derive fields from my samples",
}
# Every button the assistant can offer under a message. upload_samples,
# validate and publish are performed by the browser (file picker / their own
# endpoints) rather than sent to /messages.
BUTTONS = {
    "describe_from_samples": "Write the description from my samples",
    "confirm_samples": "✓ They're the same type — continue",
    "confirm_description": "✓ Confirm description",
    "suggest_name": "↻ Suggest another name",
    "skip_samples": "No samples — skip",
    "derive_fields": "Derive fields from my samples",
    "upload_samples": "📎 Upload samples",
    "validate": "▶ Test on sample",
    "publish": "Publish document type",
}

WELCOME = (
    "Hi! I'll help you set up a new document type.\n\n"
    "**Describe the kind of document** you want the AI to handle — what it is, what it's used for, "
    "and which information matters most. A few sentences is plenty.\n\n"
    "Or, if you have **sample documents**, upload a few (two or more is best) and I'll work the description "
    "out from those."
)
SINGLE_SAMPLE_NOTE = (
    "\n\nI only have one sample to go on, so I've kept this general — adding another example would help me "
    "avoid over-fitting to this one."
)
SAMPLES_PROMPT = (
    "Do you have any **sample documents** of this type? A few real examples help me design fields that match "
    "what your documents actually contain, and let you test the extraction before publishing. "
    "(PDF, Word, Excel, text and image files work.)"
)
FIELDS_HINT = (
    "\n\nEdit any field directly in the panel on the right, or tell me what to change "
    "(for example “split address into street and city” or “add a due date”)."
)


def _actions(*names: str) -> list[dict]:
    return [{"action": n, "label": BUTTONS[n]} for n in names]


# ---------------------------------------------------------------- heuristics
_AFFIRM = {
    "yes", "y", "yep", "yeah", "yup", "ok", "okay", "sure", "confirm", "confirmed", "approve", "approved",
    "accept", "correct", "right", "perfect", "great", "good", "fine", "lgtm", "looks good", "looks great",
    "looks right", "looks correct", "that works", "thats right", "that's right", "that is right", "go ahead",
    "sounds good", "sounds right", "all good", "yes please", "yes confirm", "confirm it", "use it", "use that",
    "done", "yes it does", "yes that works", "yes thats right", "yes that's right",
}
_CONTRAST = {"but", "except", "however", "although", "change", "add", "remove", "not", "no", "instead", "also"}


def _norm(text: str) -> str:
    t = (text or "").lower().replace("’", "'")
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def is_affirmative(text: str) -> bool:
    n = _norm(text)
    if n in _AFFIRM:
        return True
    words = n.split()
    return bool(words) and words[0] in {"yes", "yep", "yeah", "yup"} and len(words) <= 4 \
        and not (set(words) & _CONTRAST)


_CONTRAST_STRICT = _CONTRAST | {"different", "wrong", "remove", "delete", "mistake", "oops", "never", "unrelated"}


def confirms_samples(text: str) -> bool:
    """A reply to "do these belong together?". Looser than is_affirmative -- the
    admin is answering that exact question, so "yes they are the same" counts --
    but any contrast or negation ("not the same", "yes but remove one") does not."""
    n = _norm(text)
    words = n.split()
    if not words or len(words) > 10 or set(words) & _CONTRAST_STRICT:
        return False
    return words[0] in {"yes", "yep", "yeah", "yup", "ok", "okay", "sure", "confirm", "confirmed", "correct",
                        "right", "continue", "proceed"} or is_affirmative(text) or "same" in words


def is_negative(text: str) -> bool:
    n = _norm(text)
    if n in {"no", "nope", "nah", "none", "skip", "not now", "later", "no thanks", "no thank you", "skip it",
             "no samples", "no sample"}:
        return True
    return (n.startswith("no ") and len(n.split()) <= 6) or n.startswith("skip") or "no sample" in n \
        or any(p in n for p in ("dont have", "don't have", "do not have", "have none", "have no "))


_ANOTHER = re.compile(
    r"\b(another|different|other|new)\b.*\b(name|one|suggestion)\b|\bsuggest\b|\btry again\b|"
    r"\bregenerate\b|\bsomething else\b|\bnot (that|this)\b", re.I)
# Leading chatter before the naming phrase ("Actually, call it ..."). A closed
# list, and only stripped when a naming phrase follows it -- so a real name
# that merely starts with one of these words ("Well Inspection Report",
# "Land Use Permit", "No Objection Certificate") is left alone.
_FILLER = (r"(?:(?:actually|well|hmm+|ok(?:ay)?|rather|instead|maybe|please|then|let'?s|we should|"
           r"i(?:'d| would| want| think)(?: like)?(?: to)?)[,:]?\s+)*")
_NAME_PHRASE = (r"(?:(?:call|name|title)\s+(?:it|this|the\s+(?:document\s+)?type)?\s*(?:as\s+)?:?\s*"
                r"|go\s+with\s+|how\s+about\s+|make\s+it\s+|it\s+should\s+be\s+|(?:its|the)\s+name\s+is\s+"
                r"|name\s+is\s+|name\s*:\s*)")
_NAME_PREFIX = re.compile(r"^" + _FILLER + _NAME_PHRASE, re.I)
_QUOTED = re.compile(r"[\"“”]([^\"“”]{2,80})[\"“”]")


def wants_another_name(text: str) -> bool:
    return bool(_ANOTHER.search(text or ""))


def extract_custom_name(text: str) -> str:
    text = (text or "").strip()
    quoted = _QUOTED.search(text)  # an explicitly quoted name beats any parsing
    if quoted:
        return ai.clean_name(quoted.group(1))
    return ai.clean_name(_NAME_PREFIX.sub("", text, count=1))


# ----------------------------------------------------------------- persistence
def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def add_message(db: AsyncSession, draft: DocumentTypeDraft, role: str, content: str,
                      payload: dict | None = None) -> DocumentTypeDraftMessage:
    m = DocumentTypeDraftMessage(draft_id=draft.id, role=role, content=content, payload=payload)
    db.add(m)
    touch(draft)  # the draft row is otherwise untouched by a chat turn
    await db.flush()
    return m


async def list_messages(db: AsyncSession, draft_id) -> list[DocumentTypeDraftMessage]:
    return list((await db.execute(
        select(DocumentTypeDraftMessage).where(DocumentTypeDraftMessage.draft_id == draft_id)
        .order_by(DocumentTypeDraftMessage.seq))).scalars().all())


async def list_samples(db: AsyncSession, draft_id) -> list[DocumentTypeDraftSample]:
    return list((await db.execute(
        select(DocumentTypeDraftSample).where(DocumentTypeDraftSample.draft_id == draft_id)
        .order_by(DocumentTypeDraftSample.created_at))).scalars().all())


async def _ready_samples(db: AsyncSession, draft: DocumentTypeDraft) -> list[DocumentTypeDraftSample]:
    return [s for s in await list_samples(db, draft.id) if _sample_status(s) == SAMPLE_READY]


def _sample_status(s: DocumentTypeDraftSample) -> str:
    """A sample still 'processing' long after upload means its parse job died
    (e.g. an API restart) -- surface that instead of spinning forever."""
    if s.status == SAMPLE_PROCESSING and _now() - s.created_at > SAMPLE_STUCK_AFTER:
        return SAMPLE_FAILED
    return s.status


# ------------------------------------------------- do the samples belong together?
def _sample_key(samples) -> str:
    """Identity of a set of samples: a changed set invalidates any earlier verdict."""
    return hashlib.sha1("|".join(sorted(str(s.id) for s in samples)).encode()).hexdigest()[:16]


def current_sample_check(draft: DocumentTypeDraft, ready) -> dict | None:
    """The stored verdict, only if it was made about exactly this set of samples."""
    chk = draft.sample_check or {}
    return chk if chk and chk.get("key") == _sample_key(ready) and len(ready) >= 2 else None


def _mark_outliers(docs: list[dict]) -> None:
    """Flag the documents that aren't in the biggest group (ties: the lowest-numbered one wins)."""
    counts = Counter(d["group"] for d in docs if d["group"])
    if not counts:
        return
    top = max(counts.values())
    majority = min(g for g, c in counts.items() if c == top)
    for d in docs:
        d["outlier"] = bool(d["group"]) and d["group"] != majority


def _mismatch_text(chk: dict, lead: str) -> str:
    lines = "\n".join(
        f"• {d['filename']} — {d['kind'] or 'unclear'}" + ("  (looks different)" if d["outlier"] else "")
        for d in chk["documents"])
    summary = f"\n\n{chk['summary']}" if chk.get("summary") else ""
    return (f"{lead}These samples don't look like the same kind of document, so I've paused before using them."
            f"{summary}\n\nHere's what I see:\n{lines}\n\n"
            "Please re-check: remove any file that doesn't belong (✕ in the panel on the right) and I'll check "
            "again, or confirm they really are the same type of document if the differences are just variation.")


async def _review_samples(db: AsyncSession, draft: DocumentTypeDraft, samples, lead: str = "") -> bool:
    """May the AI use these samples? True unless there are two or more that don't
    look like one kind of document and the admin hasn't confirmed them -- in which
    case this posts a message saying what each document appears to be and returns
    False so the caller stops. The verdict is computed once per distinct set."""
    if len(samples) < 2 or is_offline():
        return True
    chk = current_sample_check(draft, samples)
    if chk is None:
        try:
            res = await asyncio.to_thread(ai.review_samples, [(s.filename, s.text) for s in samples])
        except Exception:  # noqa: BLE001 -- a safety net; never block the admin on our own failure
            log.warning("type_builder_sample_review_failed", exc_info=True)
            return True
        _record_cost(db, draft.tenant_id, res.usage)
        docs = [{"sample_id": str(s.id), "filename": s.filename, "kind": d.kind, "group": d.group, "outlier": False}
                for s, d in zip(samples, res.documents)]
        _mark_outliers(docs)
        chk = {"key": _sample_key(samples), "related": res.related, "summary": res.summary,
               "confirmed": False, "documents": docs}
        draft.sample_check = chk
    if chk["related"] or chk.get("confirmed"):
        return True
    await add_message(db, draft, "assistant", _mismatch_text(chk, lead),
                      {"kind": "sample_mismatch", "documents": chk["documents"],
                       "actions": _actions("confirm_samples", "upload_samples")})
    return False


async def _grounding_samples(db: AsyncSession, draft: DocumentTypeDraft) -> list[DocumentTypeDraftSample]:
    """The samples the AI is allowed to read: every ready one, unless there are
    two or more that haven't passed the consistency check (or been confirmed)."""
    ready = await _ready_samples(db, draft)
    if len(ready) < 2 or is_offline():
        return ready
    chk = current_sample_check(draft, ready)
    return ready if chk and (chk["related"] or chk.get("confirmed")) else []


def sample_out(s: DocumentTypeDraftSample) -> dict:
    status = _sample_status(s)
    return {
        "id": str(s.id), "filename": s.filename, "file_type": s.file_type, "file_size": s.file_size,
        "status": status, "page_count": s.page_count, "char_count": len(s.text or ""),
        "error": s.error or ("Processing timed out — remove it and upload again." if status != s.status else None),
    }


def message_out(m: DocumentTypeDraftMessage) -> dict:
    return {"id": str(m.id), "role": m.role, "content": m.content, "payload": m.payload,
            "created_at": m.created_at.isoformat()}


def draft_summary(d: DocumentTypeDraft, sample_count: int = 0) -> dict:
    return {
        "id": str(d.id), "status": d.status, "stage": d.stage,
        "title": d.name.strip() or (d.description.strip()[:60] or "Untitled draft"),
        "name": d.name, "field_count": len(d.fields or []), "sample_count": sample_count,
        "created_by": str(d.created_by) if d.created_by else None,
        "updated_at": d.updated_at.isoformat(), "created_at": d.created_at.isoformat(),
        "published_type_id": str(d.published_type_id) if d.published_type_id else None,
    }


async def draft_detail(db: AsyncSession, d: DocumentTypeDraft) -> dict:
    samples = await list_samples(db, d.id)
    ready = [s for s in samples if _sample_status(s) == SAMPLE_READY]
    return {
        **draft_summary(d, len(samples)),
        "name_confirmed": d.name_confirmed, "description": d.description,
        "description_confirmed": d.description_confirmed, "fields": d.fields or [],
        "last_validation": d.last_validation or {},
        "samples": [sample_out(s) for s in samples],
        # null unless the verdict is about exactly the samples currently there
        "sample_check": current_sample_check(d, ready),
        "messages": [message_out(m) for m in await list_messages(db, d.id)],
        "ai_available": not is_offline(),
    }


async def create_draft(db: AsyncSession, user) -> DocumentTypeDraft:
    d = DocumentTypeDraft(tenant_id=user.tenant_id, created_by=user.id, fields=[], last_validation={})
    db.add(d)
    await db.flush()
    await add_message(db, d, "assistant", WELCOME,
                      {"kind": "prompt_describe", "actions": _actions("upload_samples")})
    # created_at/updated_at are server defaults: load them now, because a lazy
    # load while serializing would fail in async context.
    await db.refresh(d)
    return d


def touch(draft: DocumentTypeDraft) -> None:
    """Stamp updated_at in Python. The column's onupdate is a SQL expression,
    which leaves the attribute expired after a flush -- and reading an expired
    attribute in async code raises. Setting it ourselves avoids that."""
    draft.updated_at = _now()


def _record_cost(db: AsyncSession, tenant_id, usage: list[LLMResult]) -> None:
    for u in usage:
        db.add(CostRecord(tenant_id=tenant_id, stage="type_builder", provider=u.provider, model=u.model,
                          prompt_tokens=u.prompt_tokens, completion_tokens=u.completion_tokens,
                          cost_usd=u.cost_usd))


# --------------------------------------------------------------------- turns
async def handle_turn(db: AsyncSession, draft: DocumentTypeDraft, user, *, content: str | None,
                      action: str | None, label: str | None) -> None:
    """Persist the admin's turn, then the assistant's reply. Caller returns the
    refreshed draft; nothing here raises for an AI failure -- that becomes a
    chat message so the admin can simply retry."""
    if draft.status != DRAFT_ACTIVE:
        raise DraftError(409, "This draft has already been published.")
    content = (content or "").strip()
    if action and action not in ACTION_LABELS:
        raise DraftError(400, f"Unknown action '{action}'")
    if not content and not action:
        raise DraftError(400, "Send a message or an action.")

    prior = await list_messages(db, draft.id)
    history = [(m.role, m.content) for m in prior]
    last_kind = (prior[-1].payload or {}).get("kind") if prior else None
    shown = (label or "").strip() or content or (
        f"Use “{draft.name}”" if action == "accept_name" and draft.name else ACTION_LABELS[action])
    await add_message(db, draft, "user", shown, {"kind": "action", "action": action} if action else None)
    await db.commit()  # keep the admin's turn even if the AI step below fails

    try:
        if action:
            await _do_action(db, draft, user, action, history)
        else:
            await _do_text(db, draft, user, content, history, last_kind)
    except ai.BuilderError as e:
        await add_message(db, draft, "assistant", f"I couldn't do that: {e} Please try again or rephrase.",
                          {"kind": "error", "actions": await _retry_actions(db, draft)})
    except Exception:  # noqa: BLE001 -- e.g. provider outage; never a 500 for the chat
        log.exception("type_builder_turn_failed", draft_id=str(draft.id), stage=draft.stage)
        await add_message(db, draft, "assistant", "Something went wrong while contacting the AI. Please try again.",
                          {"kind": "error", "actions": await _retry_actions(db, draft)})
    await db.commit()


async def _retry_actions(db: AsyncSession, draft: DocumentTypeDraft) -> list[dict]:
    """After a failure, offer the current step's buttons again so the admin
    isn't left with a message and nothing to click."""
    if draft.stage == STAGE_DESCRIBE:
        if draft.description.strip():
            return _actions("confirm_description")
        return _actions(*(["describe_from_samples"] if await _ready_samples(db, draft) else []), "upload_samples")
    if draft.stage == STAGE_NAME:
        return _actions("suggest_name")
    if draft.stage == STAGE_SAMPLES:
        ready = bool(await _ready_samples(db, draft))
        return _actions(*(["derive_fields"] if ready else []), "upload_samples", "skip_samples")
    return _actions("publish")


async def _taken_names(db: AsyncSession, tenant_id, history_msgs: list[DocumentTypeDraftMessage]) -> list[str]:
    existing = list((await db.execute(
        select(DocumentType.name).where(DocumentType.tenant_id == tenant_id))).scalars().all())
    suggested = [m.payload.get("name") for m in history_msgs
                 if m.payload and m.payload.get("kind") == "name_proposal" and m.payload.get("name")]
    return existing + suggested


async def _propose_name(db: AsyncSession, draft: DocumentTypeDraft, lead: str) -> None:
    msgs = await list_messages(db, draft.id)
    taken = await _taken_names(db, draft.tenant_id, msgs)
    try:
        name, usage = await asyncio.to_thread(ai.suggest_name, draft.description, taken)
    except Exception:  # noqa: BLE001 -- naming is a nicety; never block the flow on it
        log.warning("type_builder_name_failed", exc_info=True)
        draft.name = ""
        await add_message(db, draft, "assistant",
                          f"{lead}What would you like to call this document type? Just type the name.",
                          {"kind": "name_prompt"})
        return
    _record_cost(db, draft.tenant_id, usage)
    draft.name, draft.name_confirmed = name, False
    await add_message(
        db, draft, "assistant",
        f"{lead}I suggest naming it **{name}**. Use it, ask for another, or just type a name of your own.",
        {"kind": "name_proposal", "name": name, "actions": [
            {"action": "accept_name", "label": f"✓ Use “{name}”"}, *_actions("suggest_name")]})


async def _ask_samples(db: AsyncSession, draft: DocumentTypeDraft, lead: str = "") -> None:
    draft.stage = STAGE_SAMPLES
    await add_message(db, draft, "assistant", lead + SAMPLES_PROMPT,
                      {"kind": "samples_prompt", "actions": _actions("upload_samples", "skip_samples")})


async def _confirm_description(db: AsyncSession, draft: DocumentTypeDraft) -> None:
    draft.description_confirmed, draft.stage = True, STAGE_NAME
    await _propose_name(db, draft, "Description confirmed. ")


async def _accept_name(db: AsyncSession, draft: DocumentTypeDraft, name: str) -> None:
    name = ai.clean_name(name)
    if not name:
        await add_message(db, draft, "assistant", "Type the name you'd like to use for this document type.",
                          {"kind": "name_prompt"})
        return
    if await name_exists(db, draft.tenant_id, name):
        await add_message(
            db, draft, "assistant",
            f"A document type named **{name}** already exists. Type a different name, or ask me to suggest one.",
            {"kind": "name_conflict", "actions": _actions("suggest_name")})
        return
    draft.name, draft.name_confirmed = name, True
    lead = f"Named **{name}**. "
    samples = await _ready_samples(db, draft)
    if not samples:
        await _ask_samples(db, draft, lead)
    elif await _review_samples(db, draft, samples, lead):
        # Samples were already provided (samples-first flow): nothing to ask.
        await _design_fields(db, draft, request=None,
                             lead=lead + "I already have your samples, so I'll design the fields from your "
                                         "description and those. ")
    else:
        # They don't look like one kind of document: hold at the samples step
        # (where they can be removed or confirmed) instead of designing on them.
        draft.stage = STAGE_SAMPLES


def _validation_hint(draft: DocumentTypeDraft) -> str:
    lines = []
    for v in (draft.last_validation or {}).values():
        bits = []
        if v.get("missing"):
            bits.append("not found: " + ", ".join(v["missing"][:15]))
        if v.get("low_confidence"):
            bits.append("low confidence: " + ", ".join(v["low_confidence"][:15]))
        if bits:
            lines.append(f"- {v.get('filename')}: " + "; ".join(bits))
    return "\n".join(lines)


async def _describe_from_samples(db: AsyncSession, draft: DocumentTypeDraft, samples, history) -> None:
    """Samples-first entry: write (or improve) the description from the samples.
    The admin still confirms it before the name and fields are built on it."""
    if is_offline():
        await add_message(db, draft, "assistant",
                          "I can't read sample documents without an AI provider. Describe the document type in "
                          "your own words instead.", {"kind": "info"})
        return
    message = ("Improve the current description using the sample documents I uploaded."
               if draft.description.strip() else
               "I haven't described the document type myself — please write the description from the sample "
               "documents I uploaded.")
    res = await asyncio.to_thread(ai.refine_description, history, draft.description, message,
                                  [(s.filename, s.text) for s in samples])
    _record_cost(db, draft.tenant_id, res.usage)
    draft.description = res.description  # never auto-confirmed on this path
    body = res.reply or "Here's the description I worked out from your samples."
    await add_message(
        db, draft, "assistant",
        "I've read your samples. " + body + (SINGLE_SAMPLE_NOTE if len(samples) == 1 else ""),
        {"kind": "description_proposal", "description": res.description,
         "actions": _actions("confirm_description")})


async def _design_fields(db: AsyncSession, draft: DocumentTypeDraft, *, request: str | None,
                         lead: str = "", use_samples: bool = True) -> None:
    # Only samples that passed the consistency check are shown to the AI; "skip"
    # passes use_samples=False and means it.
    grounding = await _grounding_samples(db, draft) if use_samples else []
    res = await asyncio.to_thread(
        ai.design_fields, name=draft.name, description=draft.description, current=draft.fields or [],
        samples=[(s.filename, s.text) for s in grounding], request=request,
        validation_hint=_validation_hint(draft))
    _record_cost(db, draft.tenant_id, res.usage)
    changed = res.fields != (draft.fields or [])
    draft.fields, draft.stage = res.fields, STAGE_FIELDS
    body = (res.reply or ("Here are the fields I'd extract." if changed else "The fields are unchanged.")).strip()
    text = lead + body + (FIELDS_HINT if changed or not request else "")
    # Testing works per sample, so it's offered whenever any sample is readable.
    names = ["validate"] if await _ready_samples(db, draft) else []
    await add_message(db, draft, "assistant", text,
                      {"kind": "fields_updated", "changed": changed, "actions": _actions(*names, "publish")})


async def _step_describe_from_samples(db: AsyncSession, draft: DocumentTypeDraft, history) -> None:
    samples = await _ready_samples(db, draft)
    if not samples:
        await add_message(db, draft, "assistant",
                          "None of your samples has finished processing yet. Give it a moment, or upload "
                          "another — or just describe the document type in words.",
                          {"kind": "info", "actions": _actions("upload_samples")})
    elif await _review_samples(db, draft, samples):
        await _describe_from_samples(db, draft, samples, history)


async def _step_derive_fields(db: AsyncSession, draft: DocumentTypeDraft) -> None:
    samples = await _ready_samples(db, draft)
    if not samples:
        await add_message(db, draft, "assistant",
                          "None of your samples has finished processing yet. Give it a moment, or upload another.",
                          {"kind": "samples_prompt", "actions": _actions("upload_samples", "skip_samples")})
    elif await _review_samples(db, draft, samples):
        await _design_fields(db, draft, request=None, lead="I've read your samples. ")


async def _do_action(db: AsyncSession, draft: DocumentTypeDraft, user, action: str, history) -> None:
    if action == "describe_from_samples":
        if draft.stage != STAGE_DESCRIBE:
            await _restate(db, draft, "The description is already settled.")
        else:
            await _step_describe_from_samples(db, draft, history)
    elif action == "confirm_samples":
        # The admin says the flagged samples really are one kind of document.
        ready = await _ready_samples(db, draft)
        chk = current_sample_check(draft, ready)
        if chk is not None:
            draft.sample_check = {**chk, "confirmed": True}
        # ...so carry on with whatever step the warning was holding up.
        if draft.stage == STAGE_DESCRIBE:
            await _step_describe_from_samples(db, draft, history)
        elif draft.stage in (STAGE_SAMPLES, STAGE_FIELDS):
            await _step_derive_fields(db, draft)
        else:
            await _restate(db, draft, "There's nothing waiting on the samples right now.")
    elif action == "confirm_description":
        if draft.stage != STAGE_DESCRIBE or not draft.description.strip():
            await _restate(db, draft, "There's no description to confirm right now.")
        else:
            await _confirm_description(db, draft)
    elif action == "accept_name":
        if draft.stage != STAGE_NAME:
            await _restate(db, draft, "The name has already been chosen.")
        else:
            await _accept_name(db, draft, draft.name)
    elif action == "suggest_name":
        if draft.stage != STAGE_NAME:
            await _restate(db, draft, "The name has already been chosen.")
        else:
            await _propose_name(db, draft, "")
    elif action == "skip_samples":
        if draft.stage != STAGE_SAMPLES:
            await _restate(db, draft, "That step is already done.")
        else:
            await _design_fields(db, draft, request=None, use_samples=False,
                                 lead="No problem — working from your description. ")
    elif action == "derive_fields":
        if draft.stage not in (STAGE_SAMPLES, STAGE_FIELDS):
            await _restate(db, draft, "Let's finish the earlier steps first.")
        else:
            await _step_derive_fields(db, draft)


async def _restate(db: AsyncSession, draft: DocumentTypeDraft, lead: str) -> None:
    """A stale button click (or one that doesn't fit the stage): say where we are."""
    where = {
        STAGE_DESCRIBE: "We're still on the description — tell me about the document type.",
        STAGE_NAME: "We're choosing the name — use the suggestion or type your own.",
        STAGE_SAMPLES: "We're at the sample documents — upload some, or skip.",
        STAGE_FIELDS: "We're refining the fields — edit them on the right or tell me what to change.",
    }[draft.stage]
    await add_message(db, draft, "assistant", f"{lead} {where}", {"kind": "info"})


async def _do_text(db: AsyncSession, draft: DocumentTypeDraft, user, content: str, history,
                   last_kind: str | None = None) -> None:
    if last_kind == "sample_mismatch" and confirms_samples(content):
        # "yes, they're the same" typed instead of clicking the button
        await _do_action(db, draft, user, "confirm_samples", history)
        return
    if draft.stage == STAGE_DESCRIBE:
        if draft.description.strip() and is_affirmative(content):
            await _confirm_description(db, draft)
            return
        # Keep feedback grounded in the samples -- the ones that passed the check.
        samples = [(s.filename, s.text) for s in await _grounding_samples(db, draft)]
        res = await asyncio.to_thread(ai.refine_description, history, draft.description, content, samples or None)
        _record_cost(db, draft.tenant_id, res.usage)
        if res.confirmed and draft.description.strip():
            await _confirm_description(db, draft)
            return
        draft.description = res.description
        await add_message(db, draft, "assistant", res.reply or "Here's how I'd describe it.",
                          {"kind": "description_proposal", "description": res.description,
                           "actions": _actions("confirm_description")})

    elif draft.stage == STAGE_NAME:
        if draft.name.strip() and is_affirmative(content):
            await _accept_name(db, draft, draft.name)
        elif wants_another_name(content):
            await _propose_name(db, draft, "")
        else:
            name = extract_custom_name(content)
            if not name or len(name) > 80 or len(name.split()) > 10:
                await add_message(db, draft, "assistant",
                                  "That's a bit long for a name — give me a short one (a few words).",
                                  {"kind": "name_prompt"})
            else:
                await _accept_name(db, draft, name)

    elif draft.stage == STAGE_SAMPLES:
        if is_negative(content):
            await _design_fields(db, draft, request=None, use_samples=False,
                                 lead="No problem — working from your description. ")
        elif is_affirmative(content):
            await add_message(db, draft, "assistant",
                              "Great — use **Upload samples** below (you can pick several files).",
                              {"kind": "samples_prompt", "actions": _actions("upload_samples", "skip_samples")})
        else:
            await add_message(db, draft, "assistant",
                              "Do you have sample documents to share? Upload them, or skip to continue without.",
                              {"kind": "samples_prompt", "actions": _actions("upload_samples", "skip_samples")})

    else:  # STAGE_FIELDS
        if is_affirmative(content):
            has = bool(await _ready_samples(db, draft))
            await add_message(
                db, draft, "assistant",
                "Great. " + ("You can test the extraction on a sample first, or publish when you're happy."
                             if has else "Publish when you're happy with the fields."),
                {"kind": "info", "actions": _actions(*(["validate"] if has else []), "publish")})
        else:
            await _design_fields(db, draft, request=content)


# ------------------------------------------------------------------- samples
@asynccontextmanager
async def _tenant_session(tenant):
    Local = async_sessionmaker(get_tenant_async_engine(tenant), class_=AsyncSession,
                               expire_on_commit=False, autoflush=False)
    async with Local() as s:
        yield s


async def process_sample(tenant, sample_id: uuid.UUID, data: bytes, ext: str) -> None:
    """Background job: extract the sample's text and mark it ready/failed.
    Runs after the upload response is sent, so a slow OCR never blocks it."""
    text, pages, error = "", None, None
    try:
        result = await asyncio.to_thread(parsing.parse, data, ext)
        text = (result.get("text") or "").strip()
        pages = len(result.get("units") or []) or None
        if not text:
            error = result.get("warning") or "No text could be extracted from this file."
    except Exception as e:  # noqa: BLE001
        log.warning("type_builder_sample_parse_failed", error=str(e)[:300])
        error = f"Could not read this file: {str(e)[:200]}"
    async with _tenant_session(tenant) as db:
        s = await db.get(DocumentTypeDraftSample, sample_id)
        if s is None:  # removed / draft discarded while parsing
            return
        if error:
            s.status, s.error = SAMPLE_FAILED, error
        else:
            s.status, s.text, s.page_count = SAMPLE_READY, text[:MAX_SAMPLE_CHARS], pages
        await db.commit()


# ---------------------------------------------------------------- validation
def _strict_fields(draft: DocumentTypeDraft) -> list[fieldsvc.FieldDef]:
    try:
        return fieldsvc.validate_schema(fieldsvc.ensure_keys(draft.fields or []))
    except fieldsvc.SchemaError as e:
        raise DraftError(422, f"The fields aren't valid yet: {e}")


def _summarize(defs, tree, filename: str, sample_id: str) -> dict:
    leaves = fieldsvc.list_leaf_paths(defs)
    rows = [{"path": v.field_path, "label": v.name, "data_type": v.data_type, "value": v.raw_value,
             "confidence": round(v.confidence, 2), "quote": v.quote}
            for v in fieldsvc.iter_flat(tree) if v.node_kind == "scalar"]
    found_patterns = {re.sub(r"\[\d+\]", "[]", r["path"]) for r in rows if r["value"]}
    missing = [lf.label for lf in leaves if lf.path_pattern not in found_patterns]
    low = [r["label"] for r in rows if r["value"] and r["confidence"] < LOW_CONFIDENCE]
    return {"sample_id": sample_id, "filename": filename, "total": len(leaves),
            "found": len(leaves) - len(missing), "missing": missing, "low_confidence": low, "rows": rows,
            "validated_at": _now().isoformat()}


async def run_validation(db: AsyncSession, draft: DocumentTypeDraft, user, sample_id: str | None) -> None:
    """Extract the draft's fields from sample(s) exactly as the real pipeline
    would (same extractor, same text limit) and report what was found."""
    if draft.status != DRAFT_ACTIVE:
        raise DraftError(409, "This draft has already been published.")
    if is_offline():
        raise DraftError(400, "Testing needs an AI provider — none is configured.")
    defs = _strict_fields(draft)
    samples = await _ready_samples(db, draft)
    if sample_id:
        samples = [s for s in samples if str(s.id) == sample_id]
    if not samples:
        raise DraftError(400, "Upload a sample document (and wait for it to finish processing) first.")

    from app.ai.agents.structured import extract_structured

    live_ids = {str(s.id) for s in await list_samples(db, draft.id)}
    results = {k: v for k, v in (draft.last_validation or {}).items() if k in live_ids}
    lines = []
    for s in samples:
        payload, usage = await asyncio.to_thread(extract_structured, s.text, defs, draft.name or "Document")
        _record_cost(db, draft.tenant_id, [usage])
        r = _summarize(defs, fieldsvc.flatten_response(defs, payload), s.filename, str(s.id))
        results[str(s.id)] = r
        line = f"**{s.filename}** — found {r['found']} of {r['total']} fields."
        if r["missing"]:
            line += " Not found: " + ", ".join(r["missing"][:8]) + ("…" if len(r["missing"]) > 8 else "") + "."
        lines.append(line)
    draft.last_validation = results  # new object so the JSONB change is detected

    label = "Test the fields on my sample" + ("s" if len(samples) > 1 else "")
    await add_message(db, draft, "user", label, {"kind": "action", "action": "validate"})
    if any(r["missing"] for r in results.values() if r["sample_id"] in {str(s.id) for s in samples}):
        tail = ("\n\nIf a missing field is really in the document, tell me what it's called or where it appears "
                "and I'll sharpen the field descriptions. Full results are in the panel on the right.")
    else:
        tail = "\n\nEvery field was found. Full results are in the panel on the right — publish when you're happy."
    await add_message(db, draft, "assistant", "\n".join(lines) + tail,
                      {"kind": "validation", "actions": _actions("validate", "publish")})
    await db.commit()


# ------------------------------------------------------------------- publish
async def publish(db: AsyncSession, draft: DocumentTypeDraft, user) -> DocumentType:
    if draft.status != DRAFT_ACTIVE:
        raise DraftError(409, "This draft has already been published.")
    if draft.stage != STAGE_FIELDS:
        raise DraftError(409, "Finish the guided steps (description, name, samples, fields) before publishing.")
    name = draft.name.strip()
    if not name:
        raise DraftError(422, "Give the document type a name.")
    fields = fieldsvc.dump_schema(_strict_fields(draft))
    try:
        dt_ = await create_document_type(
            db, tenant_id=draft.tenant_id, created_by=user.id, name=name,
            description=draft.description.strip()[:MAX_DESCRIPTION_STORED], fields=fields)
    except NameTaken:
        raise DraftError(409, f"A document type named '{name}' already exists — rename this one and publish again.")
    draft.status, draft.published_type_id, draft.name = DRAFT_PUBLISHED, dt_.id, name
    await add_message(db, draft, "assistant", f"Published **{name}**. It's ready to use on new documents.",
                      {"kind": "published"})
    await audit.log_event(db, tenant_id=draft.tenant_id, actor_id=user.id, event_type=AUDIT_CONFIG_CHANGE,
                          object_type="document_type", object_id=dt_.id,
                          detail={"action": "publish_ai_draft", "draft_id": str(draft.id), "name": name},
                          commit=False)
    await db.commit()
    return dt_
