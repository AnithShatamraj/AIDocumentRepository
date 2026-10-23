"""Suggest the details of a new field when promoting a discovered fact into a
document type: its name, description, data type, home document type, and
whether it is really an existing field worded differently.

One LLM call covers every fact in the request. Offline (no API key), or if the
call fails or returns something unusable, a heuristic fills the same fields, so
the admin always gets a pre-filled form to review rather than an empty one. The
model only ever *suggests*: every value is validated against the real document
types and field types before it leaves this module, and the admin edits them.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.ai import prompts
from app.ai.registry import get_llm, is_offline
from app.ai.types import Message
from app.core.config import settings
from app.core.logging import get_logger
from app.services import normalize
from app.services.fields import SCALAR_TYPES, slugify_key

log = get_logger(__name__)

MAX_FACTS = 30
_CURRENCY = re.compile(r"^\s*(?:rs\.?|inr|usd|eur|gbp|[₹$€£¥])\s*[\d,]+(?:\.\d+)?\s*(?:/-)?\s*$", re.I)
_NUMBER = re.compile(r"^\s*-?[\d,]+(?:\.\d+)?\s*$")
_BOOL = {"yes", "no", "true", "false"}


@dataclass
class Fact:
    id: str
    name: str
    value: str
    quote: str
    current_type_id: str


@dataclass
class TypeInfo:
    id: str
    name: str
    description: str
    fields: list[dict]  # existing top-level fields: key, name, description, data_type


def infer_data_type(value: str | None) -> str:
    """Best guess at a value's type from its text alone."""
    v = (value or "").strip()
    if not v:
        return "string"
    if _CURRENCY.match(v):
        return "currency"
    if v.lower() in _BOOL:
        return "boolean"
    if _NUMBER.match(v):
        return "integer" if re.fullmatch(r"-?[\d,]+", v) else "number"
    if normalize._parse_date(v) is not None:  # noqa: SLF001  same parser the extractor uses
        return "date"
    return "string"


def _match_existing(name: str, fields: list[dict]) -> str | None:
    """An existing field whose key or name is the same label, ignoring case/punctuation."""
    slug = slugify_key(name)
    for f in fields:
        if f["key"] == slug or slugify_key(f["name"]) == slug:
            return f["key"]
    return None


def _fallback(fact: Fact, types: list[TypeInfo]) -> dict:
    home = next((t for t in types if t.id == fact.current_type_id), types[0] if types else None)
    return {
        "id": fact.id,
        "name": fact.name,
        "description": f"The {fact.name.lower()} stated in the document.",
        "data_type": infer_data_type(fact.value),
        "document_type_id": home.id if home else fact.current_type_id,
        "existing_key": _match_existing(fact.name, home.fields) if home else None,
    }


def _clean(raw: dict, fact: Fact, types: list[TypeInfo]) -> dict:
    """Make one model suggestion safe: unknown ids, bad types and invented keys
    fall back to the heuristic's answer for that field."""
    base = _fallback(fact, types)
    by_id = {t.id: t for t in types}
    type_id = raw.get("document_type_id")
    chosen = by_id.get(str(type_id)) if type_id else None
    if chosen is None:
        chosen = by_id.get(base["document_type_id"])

    name = re.sub(r"\s+", " ", str(raw.get("name") or "")).strip()[:80] or base["name"]
    desc = re.sub(r"\s+", " ", str(raw.get("description") or "")).strip()[:300] or base["description"]
    dtype = raw.get("data_type")
    if dtype not in SCALAR_TYPES:
        dtype = base["data_type"]

    existing = raw.get("existing_key")
    keys = {f["key"] for f in chosen.fields} if chosen else set()
    existing = existing if isinstance(existing, str) and existing in keys else None
    if existing is None and chosen is not None:
        existing = _match_existing(name, chosen.fields)

    return {"id": fact.id, "name": name, "description": desc, "data_type": dtype,
            "document_type_id": chosen.id if chosen else base["document_type_id"],
            "existing_key": existing}


def suggest(facts: list[Fact], types: list[TypeInfo]) -> tuple[list[dict], bool]:
    """Suggestions for `facts`, in the same order. The flag says whether an LLM
    produced them (False = heuristic fallback)."""
    facts = facts[:MAX_FACTS]
    if not facts:
        return [], False
    if is_offline() or not types:
        return [_fallback(f, types) for f in facts], False

    payload = {
        "facts": [{"id": f.id, "label": f.name, "value": f.value[:300], "quote": (f.quote or "")[:300],
                   "current_type_id": f.current_type_id} for f in facts],
        "document_types": [
            {"id": t.id, "name": t.name, "description": t.description[:300],
             "existing_fields": [{"key": x["key"], "name": x["name"], "data_type": x["data_type"]}
                                 for x in t.fields][:60]}
            for t in types],
    }
    try:
        data, _ = get_llm().complete_json(
            [Message("system", prompts.FIELD_SUGGEST_SYSTEM),
             Message("user", json.dumps(payload, ensure_ascii=False))],
            model=settings.llm_builder_model or settings.llm_model,
            temperature=0.1, max_tokens=2048)
        rows = {str(r.get("id")): r for r in (data.get("suggestions") or []) if isinstance(r, dict)}
    except Exception:  # noqa: BLE001
        log.warning("field_suggest_failed", exc_info=True)
        return [_fallback(f, types) for f in facts], False

    return [_clean(rows.get(f.id, {}), f, types) for f in facts], True
