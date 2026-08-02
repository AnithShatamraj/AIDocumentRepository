"""Schema-driven extraction against a document type's nested field tree.

Uses provider structured outputs (strict JSON Schema) so the response shape is
guaranteed rather than hoped for — important once schemas contain objects and
lists, where free-form JSON goes wrong in far more ways.

Returns the raw nested payload; `services/fields.flatten_response` turns it into
rows. Falls back to plain JSON mode if the provider rejects the schema.
"""
from __future__ import annotations

import json

from app.ai import prompts
from app.ai.types import LLMResult, Message
from app.core.config import settings
from app.core.logging import get_logger
from app.services.fields import FieldDef, compile_json_schema, describe_for_prompt

log = get_logger(__name__)
_TEXT_LIMIT = 14000

EXTRACT_SYSTEM = """You extract structured data from a {type_name} document.

Fields to extract:
{outline}

Rules:
- Return every field defined by the schema; use null for anything not present.
- `value` must be copied verbatim from the document (do not reformat dates or numbers).
- `quote` must be the exact snippet of document text that supports the value — this
  is used to highlight the source, so it must appear in the document character for character.
- `confidence` is your 0..1 certainty for that individual value.
- For lists, return one entry per real occurrence; return [] when there are none.
- Never invent values."""


def extract_structured(
    text: str, defs: list[FieldDef], type_name: str
) -> tuple[dict, LLMResult]:
    """Run schema-bound extraction. Returns (nested payload, usage)."""
    from openai import OpenAI

    schema = compile_json_schema(defs, f"{type_name}_extraction")
    system = EXTRACT_SYSTEM.format(type_name=type_name, outline=describe_for_prompt(defs))
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Document text:\n\n{text[:_TEXT_LIMIT]}"},
    ]
    kwargs = {"api_key": settings.openai_api_key}
    if settings.openai_base_url:
        kwargs["base_url"] = settings.openai_base_url
    client = OpenAI(**kwargs)
    model = settings.llm_extract_model

    try:
        resp = client.chat.completions.create(
            model=model, messages=messages, temperature=0,
            response_format={"type": "json_schema", "json_schema": schema},
        )
    except Exception as e:  # noqa: BLE001
        # Older models / gateways may not support json_schema — fall back to
        # plain JSON mode with the shape described in the prompt.
        log.warning("structured_output_unsupported_fallback_json", error=str(e)[:200])
        messages[0]["content"] += (
            "\n\nReturn a single JSON object. Each scalar field must be an object "
            '{"value": ..., "confidence": ..., "quote": ...}; lists must be arrays.')
        resp = client.chat.completions.create(
            model=model, messages=messages, temperature=0,
            response_format={"type": "json_object"},
        )

    raw = resp.choices[0].message.content or "{}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("structured_extract_unparseable")
        payload = {}

    usage = resp.usage
    pt = usage.prompt_tokens if usage else 0
    ct = usage.completion_tokens if usage else 0
    from app.ai.pricing import llm_cost

    result = LLMResult(text=raw, provider="openai", model=model, prompt_tokens=pt,
                       completion_tokens=ct, cost_usd=llm_cost(model, pt, ct))
    return payload, result


DISCOVER_SYSTEM = """You are reviewing a {type_name} document that has already had these
fields extracted: {known}.

Find up to {limit} ADDITIONAL facts that a reader of this document type would care about
and that are NOT already covered above. Return STRICT JSON:
{{"entities": [{{"name": "<short label>", "value": "<verbatim value>",
               "confidence": <0..1>, "quote": "<exact supporting snippet>"}}]}}
Only include facts actually stated in the document. Return an empty list if there is nothing worth adding."""


def discover_entities(text: str, known_names: list[str], type_name: str, limit: int = 8):
    """Second pass: salient facts outside the schema ('Entities')."""
    from app.ai.registry import get_llm

    llm = get_llm()
    msgs = [
        Message("system", DISCOVER_SYSTEM.format(
            type_name=type_name, known=", ".join(known_names) or "none", limit=limit)),
        Message("user", f"Document text:\n\n{text[:_TEXT_LIMIT]}"),
    ]
    data, res = llm.complete_json(msgs, model=settings.llm_extract_model)
    items = []
    for e in (data.get("entities") or [])[:limit]:
        name = (e.get("name") or "").strip()
        value = e.get("value")
        if not name or value in (None, ""):
            continue
        items.append({
            "name": name[:120], "value": str(value),
            "confidence": float(e.get("confidence") or 0.0),
            "quote": e.get("quote"),
        })
    return items, res
