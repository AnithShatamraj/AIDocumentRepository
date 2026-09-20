"""LLM calls behind the AI-assisted document-type builder.

Pure functions (no database): each takes plain values and returns plain values
plus the LLM usage, so the workflow in `services/type_builder.py` decides what
to persist. Every function has an offline fallback so the app still works with
no API key -- the AI just does less (see each function's docstring).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from app.ai.registry import get_llm, is_offline
from app.ai.types import LLMResult, Message
from app.core.config import settings
from app.core.logging import get_logger
from app.services.fields import SchemaError, dump_schema, ensure_keys, validate_schema

log = get_logger(__name__)

MAX_DESCRIPTION_CHARS = 900
MAX_NAME_CHARS = 60


class BuilderError(Exception):
    """The model couldn't produce something usable (after retry)."""


def _model() -> str:
    return settings.llm_builder_model or settings.llm_model


def _call(system: str, user: str, *, history: list[tuple[str, str]] | None = None,
          temperature: float = 0.2, max_tokens: int = 2048) -> tuple[dict, LLMResult]:
    msgs = [Message("system", system)]
    for role, text in history or []:
        msgs.append(Message("assistant" if role == "assistant" else "user", text))
    msgs.append(Message("user", user))
    return get_llm().complete_json(msgs, model=_model(), temperature=temperature, max_tokens=max_tokens)


# ------------------------------------------------------------------ description
DESCRIBE_SYSTEM = f"""You help an administrator define a new DOCUMENT TYPE for an AI document repository. \
The AI will later use your description to recognise documents of this type and to decide which fields to extract.

In each turn:
1. Read the conversation and the administrator's latest message.
2. Write or revise the description of the document type: clear, well-structured plain text of 2-5 sentences \
(at most {MAX_DESCRIPTION_CHARS} characters, no markdown) covering what the document is, what it is used for, \
what it typically contains, and which information matters most. Stay faithful to what the administrator said and \
do not invent details. When they give feedback, apply it and keep the parts they did not object to.
3. If the latest message clearly approves the current description as final (for example "yes", "looks good", \
"that's right, go ahead"), set "confirmed" to true and return the current description unchanged. Otherwise set it to false.
4. Write a short conversational "reply" (1-3 sentences): say what you captured or changed, then ask whether the \
description is right or what to adjust. If confirming, just acknowledge briefly.

Return STRICT JSON: {{"description": "<text>", "reply": "<text>", "confirmed": <true|false>}}"""

# Appended when the administrator has uploaded sample documents.
DESCRIBE_SAMPLES_GUIDE = """

The administrator also uploaded sample documents (excerpts are in their message). Treat them as evidence of what \
this kind of document contains, but describe the document TYPE, not the individual samples: generalise, never quote \
sample-specific values (names, amounts, dates, reference numbers), and mention typical variations you can infer. \
If the administrator has not described the type in their own words, derive the whole description from the samples."""


@dataclass
class DescriptionResult:
    description: str
    reply: str
    confirmed: bool
    usage: list[LLMResult] = field(default_factory=list)


def refine_description(history: list[tuple[str, str]], current: str, message: str,
                       samples: list[tuple[str, str]] | None = None) -> DescriptionResult:
    """Restructure the admin's words into a description and fold in feedback.

    With `samples` (filename, text) the description is grounded in them -- and
    if the admin never described the type, derived from them alone.

    Offline: keeps the admin's text as-is (first message replaces, later ones
    are appended as additions) and says so.
    """
    if is_offline():
        desc = message.strip() if not current.strip() else f"{current.strip()} {message.strip()}"
        return DescriptionResult(
            description=desc[:MAX_DESCRIPTION_CHARS],
            reply="The AI provider isn't configured, so I've kept your wording as-is instead of rewriting it. "
                  "Is this description right, or would you like to change it?",
            confirmed=False)
    user = f"Current description (may be empty):\n{current or '(none yet)'}\n\nAdministrator's latest message:\n{message}"
    if samples:
        user += "\n\nSample documents (excerpts):\n" + sample_block(samples, budget=18000)
    system = DESCRIBE_SYSTEM + (DESCRIBE_SAMPLES_GUIDE if samples else "")
    data, res = _call(system, user, history=history[-10:], temperature=0.3)
    desc = str(data.get("description") or "").strip()[:MAX_DESCRIPTION_CHARS]
    reply = str(data.get("reply") or "").strip()
    if not desc:
        raise BuilderError("The model did not return a description.")
    return DescriptionResult(description=desc, reply=reply, confirmed=bool(data.get("confirmed")), usage=[res])


# ---------------------------------------------------------- sample consistency
REVIEW_SYSTEM = """You check whether sample documents uploaded to define ONE document type really are the same \
KIND of document. The administrator will build a single set of extraction fields from them, so mixing kinds (for \
example a rental agreement and a resume, or an invoice and a purchase order) would produce a bad definition.

For each sample, say in at most 12 words what it is: the kind of document and, when clear, who or what it concerns \
(for example "Purchase order from a steel buyer"). Then group the samples: samples that are the same kind of \
document share a group number (1, 2, ...). Different senders, customers, layouts or templates of the SAME kind are \
one group; documents a business would treat as different kinds are different groups (invoice vs purchase order, \
contract vs resume).

Return STRICT JSON: {"documents": [{"sample": <the sample's number as given>, "kind": "<what it is>", \
"group": <group number>}], "summary": "<one sentence on what you found>"}"""

_REVIEW_EXCERPT_CHARS = 2500


@dataclass
class SampleDoc:
    kind: str
    group: int


@dataclass
class SampleReview:
    related: bool  # all samples are one kind of document
    summary: str
    documents: list[SampleDoc]  # same order as the samples passed in
    usage: list[LLMResult] = field(default_factory=list)


def review_samples(samples: list[tuple[str, str]]) -> SampleReview:
    """Say what each sample is and whether they're all the same kind of document.

    `related` comes from the grouping the model returned (one group = related)
    rather than from a yes/no it might contradict itself on. Offline: assumes
    related -- there's no model to ask, and this is a safety net, not a gate.
    """
    if is_offline() or len(samples) < 2:
        return SampleReview(related=True, summary="", documents=[SampleDoc("", 1) for _ in samples])
    body = "\n\n".join(f"### Sample {i} ({name})\n{text[:_REVIEW_EXCERPT_CHARS]}"
                       for i, (name, text) in enumerate(samples, 1))
    data, res = _call(REVIEW_SYSTEM, body, temperature=0.0, max_tokens=1200)
    by_number: dict[int, SampleDoc] = {}
    for d in data.get("documents") or []:
        try:
            by_number[int(d.get("sample"))] = SampleDoc(
                kind=str(d.get("kind") or "").strip()[:120], group=int(d.get("group") or 1))
        except (TypeError, ValueError):
            continue
    docs = [by_number.get(i, SampleDoc("", 0)) for i in range(1, len(samples) + 1)]
    groups = {d.group for d in docs if d.group}
    return SampleReview(related=len(groups) <= 1, summary=str(data.get("summary") or "").strip(),
                        documents=docs, usage=[res])


# ------------------------------------------------------------------------ name
NAME_SYSTEM =f"""You name document types for a document repository. Suggest one concise name for the document type \
described by the user. Rules: Title Case, 1-4 words, a singular noun phrase naming the document (for example \
"Purchase Order", "Residential Rent Agreement", "Employee Resume"), at most {MAX_NAME_CHARS} characters, no quotes or \
trailing punctuation. It must not duplicate (ignoring case) any name in the "taken" list.

Return STRICT JSON: {{"name": "<name>"}}"""


def clean_name(raw: str, limit: int = 120) -> str:
    name = re.sub(r"\s+", " ", (raw or "").strip())
    return name.strip(" \"'`“”‘’.,;:!")[:limit]


def suggest_name(description: str, taken: list[str]) -> tuple[str, list[LLMResult]]:
    """Offline: the first few meaningful words of the description, Title Cased."""
    taken_lc = {t.lower() for t in taken}
    if is_offline():
        words = [w for w in re.findall(r"[A-Za-z][A-Za-z\-']+", description)
                 if w.lower() not in _NAME_STOPWORDS][:3]
        base = " ".join(w.capitalize() for w in words) or "New Document Type"
        return _dedupe(base, taken_lc), []
    user = f"Document type description:\n{description}\n\ntaken: {json.dumps(taken)}"
    data, res = _call(NAME_SYSTEM, user, temperature=0.4, max_tokens=100)
    name = clean_name(str(data.get("name") or ""), MAX_NAME_CHARS)
    if not name:
        raise BuilderError("The model did not return a name.")
    return _dedupe(name, taken_lc), [res]


def _dedupe(name: str, taken_lc: set[str]) -> str:
    if name.lower() not in taken_lc:
        return name
    n = 2
    while f"{name} {n}".lower() in taken_lc:
        n += 1
    return f"{name} {n}"


_NAME_STOPWORDS = frozenset(
    "a an the this that these those and or of to in on for with from is are was were be it its as by "
    "document documents type types file files about which used use uses using typically usually".split())


# ----------------------------------------------------------------------- fields
FIELD_SPEC = """A field definition is a JSON object:
- "name": the human label, e.g. "Invoice Number"
- "description": an INSTRUCTION to an extraction AI -- what to look for, where it usually appears, and the expected \
format. Be specific ("Total payable including tax, as printed on the last page"), not a restatement of the name.
- "data_type": one of string, number, integer, boolean, date, time, datetime, currency, object, list
- "required": true only if essentially every document of this type has it
- "fields": [...]  ONLY for data_type "object": its child field definitions (at least one)
- "item": {...}    ONLY for data_type "list": ONE definition describing each element, with its own "name" (e.g. \
"Line Item"), "description" and "data_type". Use data_type "object" with "fields" for rows with several columns \
(e.g. invoice line items), or a scalar type for a plain list of values (e.g. skills).

Design rules:
- Group related fields with "object" (e.g. Seller -> name, address, tax id). Use "list" for anything that repeats \
(line items, parties, work experience, payments).
- Use "currency" for money, "date" for dates, "integer"/"number" for quantities, "boolean" for yes/no, "string" otherwise.
- Do NOT include "key" (it is generated). Sibling names must be unique. Nest at most 4 levels deep.
- 5-40 fields in total is typical. Only include fields this kind of document really contains and a reader would care \
about; do not pad."""

FIELDS_SYSTEM = f"""You design the extraction schema for a document type in an AI document repository: the list of \
fields an AI will extract from every document of this type.

{FIELD_SPEC}

Return STRICT JSON: {{"fields": [<field definition>, ...], "reply": "<1-3 sentences>"}}
"reply" tells the administrator what you did (the main groups/lists, anything you were unsure about) and invites them \
to adjust it. Do not state how many fields there are. Always return the COMPLETE field list, not just the changes."""


@dataclass
class FieldsResult:
    fields: list[dict]  # validated, with keys
    reply: str
    usage: list[LLMResult] = field(default_factory=list)


def _slim(nodes: list[dict] | None) -> list[dict]:
    """Drop keys/noise so the model sees (and we re-derive) just the design."""
    out = []
    for n in nodes or []:
        s: dict = {"name": n.get("name", ""), "description": n.get("description", ""),
                   "data_type": n.get("data_type", "string")}
        if n.get("required"):
            s["required"] = True
        if n.get("fields"):
            s["fields"] = _slim(n["fields"])
        if n.get("item"):
            s["item"] = _slim([n["item"]])[0]
        out.append(s)
    return out


def sample_block(samples: list[tuple[str, str]], budget: int = 24000) -> str:
    """Excerpts of the sample documents, sharing `budget` characters."""
    if not samples:
        return ""
    per = max(1500, min(8000, budget // len(samples)))
    parts = [f"### Sample {i}: {name}\n{text[:per]}" for i, (name, text) in enumerate(samples, 1)]
    return "\n\n".join(parts)


def _repair(nodes: list, parent_name: str = "") -> list[dict]:
    """Fill in what models routinely leave out but the schema requires and the
    UI would default anyway: a list item's `name`, and `item`/`data_type` on a
    list. Anything structurally wrong beyond that still fails validation."""
    out = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        n = dict(n)
        n["name"] = str(n.get("name") or parent_name or "Field").strip()
        n.setdefault("description", "")
        dtype = n.get("data_type")
        if dtype == "object" and isinstance(n.get("fields"), list):
            n["fields"] = _repair(n["fields"])
        elif dtype == "list":
            item = n.get("item") if isinstance(n.get("item"), dict) else {}
            item = dict(item)
            item["data_type"] = item.get("data_type") or "string"
            item["name"] = str(item.get("name") or f"{n['name']} item").strip()
            item.setdefault("description", "")
            if item["data_type"] == "object":
                item["fields"] = _repair(item.get("fields") or [])
            else:
                item.pop("fields", None)
            n["item"] = item
            n.pop("fields", None)
        out.append(n)
    return out


def _try_validate(raw_fields) -> list[dict]:
    if not isinstance(raw_fields, list) or not raw_fields:
        raise SchemaError("`fields` must be a non-empty list")
    return dump_schema(validate_schema(ensure_keys(_repair(raw_fields))))


def design_fields(
    *, name: str, description: str, current: list[dict], samples: list[tuple[str, str]],
    request: str | None = None, validation_hint: str = "",
) -> FieldsResult:
    """Derive the initial field tree, or apply the admin's `request` to `current`.

    The output is validated against the real schema rules (`validate_schema`);
    if the model produced something invalid it gets one chance to fix it.
    Offline: returns `current` unchanged with an explanation.
    """
    if is_offline():
        return FieldsResult(
            fields=current,
            reply="The AI provider isn't configured, so I can't design fields automatically. "
                  "Add them directly in the panel on the right.")

    parts = [f"Document type: {name or '(unnamed)'}", f"Description: {description or '(none)'}"]
    slim = _slim(current)
    parts.append("Current fields (JSON):\n" + (json.dumps(slim, indent=1) if slim else "(none yet)"))
    block = sample_block(samples)
    if block:
        parts.append("Sample documents (excerpts). Derive fields that generalise across the whole document type, "
                     "not values that are specific to one sample:\n" + block)
    if validation_hint:
        parts.append("Result of the last test extraction on a sample:\n" + validation_hint)
    if request:
        parts.append(f"Administrator's request -- apply it to the current fields and return the full updated list:\n{request}")
    elif slim:
        parts.append("Task: improve the current fields using the description and the sample documents "
                     "(add what is missing, sharpen descriptions), keeping what already works.")
    else:
        parts.append("Task: derive the initial field list for this document type from the description"
                     + (" and the sample documents." if block else "."))
    user = "\n\n".join(parts)

    usage: list[LLMResult] = []
    error = ""
    for attempt in range(2):
        prompt = user if not error else (
            user + f"\n\nYour previous answer was rejected: {error}\nReturn corrected JSON.")
        data, res = _call(FIELDS_SYSTEM, prompt, temperature=0.2, max_tokens=4096)
        usage.append(res)
        try:
            fields = _try_validate(data.get("fields"))
        except SchemaError as e:
            error = " ".join(str(e).split())[:400]
            log.warning("type_builder_fields_invalid", attempt=attempt, error=error)
            continue
        return FieldsResult(fields=fields, reply=str(data.get("reply") or "").strip(), usage=usage)
    raise BuilderError("I couldn't produce a valid set of fields this time.")
