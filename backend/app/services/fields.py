"""Document-type field definitions: validation, JSON Schema compilation, flattening.

A Document Type owns a *versioned tree* of field definitions (stored as JSONB).
Each node has a stable `key` — display names can change without orphaning stored
values. Supported data types:

    string | number | integer | boolean | date | time | datetime | currency
    object  -> has `fields` (nested definitions)
    list    -> has `item`   (any node, including object)

Three jobs live here:
  * validate/normalize an author-supplied tree (Pydantic, with depth/size caps),
  * compile it to a JSON Schema for provider structured outputs, wrapping every
    leaf as {value, confidence, quote} so provenance survives inside lists,
  * flatten a model's response back into flat rows carrying a `field_path`
    such as `work_experience[0].organization`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# Provider structured-output limits bite well before these, but they also keep
# prompt cost and the review queue sane.
MAX_DEPTH = 5
MAX_FIELDS = 100

SCALAR_TYPES = {"string", "number", "integer", "boolean", "date", "time", "datetime", "currency"}
CONTAINER_TYPES = {"object", "list"}
DATA_TYPES = SCALAR_TYPES | CONTAINER_TYPES

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class SchemaError(ValueError):
    pass


def slugify_key(name: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    if not key:
        key = "field"
    if not key[0].isalpha():
        key = f"f_{key}"
    return key[:63]


class FieldDef(BaseModel):
    """One node of a document type's field tree."""

    key: str
    name: str
    description: str = ""
    data_type: Literal[
        "string", "number", "integer", "boolean", "date", "time", "datetime",
        "currency", "object", "list",
    ]
    required: bool = False
    # Per-field auto-accept override; falls back to the tenant/global threshold.
    threshold: float | None = None
    fields: list["FieldDef"] = Field(default_factory=list)  # object children
    item: "FieldDef | None" = None  # list element definition

    @field_validator("key")
    @classmethod
    def _valid_key(cls, v: str) -> str:
        v = (v or "").strip()
        if not _KEY_RE.match(v):
            raise ValueError(
                f"invalid field key {v!r}: use lowercase letters, digits and underscores")
        return v

    @model_validator(mode="after")
    def _shape(self) -> "FieldDef":
        if self.data_type == "object":
            if not self.fields:
                raise ValueError(f"object field {self.key!r} needs at least one child field")
            if self.item is not None:
                raise ValueError(f"object field {self.key!r} must not define `item`")
            dupes = {f.key for f in self.fields}
            if len(dupes) != len(self.fields):
                raise ValueError(f"duplicate child keys in object {self.key!r}")
        elif self.data_type == "list":
            if self.item is None:
                raise ValueError(f"list field {self.key!r} needs an `item` definition")
            if self.fields:
                raise ValueError(f"list field {self.key!r} must not define `fields`")
        else:
            if self.fields or self.item is not None:
                raise ValueError(f"scalar field {self.key!r} cannot have children")
        return self


FieldDef.model_rebuild()


def validate_schema(nodes: list[dict] | list[FieldDef]) -> list[FieldDef]:
    """Validate an author-supplied field tree; raises SchemaError."""
    try:
        defs = [n if isinstance(n, FieldDef) else FieldDef.model_validate(n) for n in nodes]
    except Exception as e:  # noqa: BLE001
        raise SchemaError(str(e)) from e
    if not defs:
        raise SchemaError("a document type needs at least one field")
    keys = [d.key for d in defs]
    if len(set(keys)) != len(keys):
        raise SchemaError("duplicate top-level field keys")

    total = 0
    def walk(node: FieldDef, depth: int) -> None:
        nonlocal total
        total += 1
        if depth > MAX_DEPTH:
            raise SchemaError(f"field nesting deeper than {MAX_DEPTH} levels ({node.key})")
        if total > MAX_FIELDS:
            raise SchemaError(f"more than {MAX_FIELDS} fields in one document type")
        for child in node.fields:
            walk(child, depth + 1)
        if node.item is not None:
            walk(node.item, depth + 1)

    for d in defs:
        walk(d, 1)
    return defs


def dump_schema(defs: list[FieldDef]) -> list[dict]:
    return [d.model_dump(exclude_none=True) for d in defs]


# --------------------------------------------------------------- JSON Schema
def _leaf_schema(node: FieldDef) -> dict:
    """Every leaf carries its own provenance so highlighting works at any depth.

    Values come back as strings regardless of data type — our normalizer turns
    them into typed values and we keep the raw text too (the spec's
    'store the value twice' rule).
    """
    hint = f"{node.name}: {node.description}".strip().rstrip(":")
    return {
        "type": "object",
        "description": hint,
        "properties": {
            "value": {"type": ["string", "null"],
                      "description": f"Verbatim value for '{node.name}', or null if absent"},
            "confidence": {"type": "number", "description": "0..1 confidence in this value"},
            "quote": {"type": ["string", "null"],
                      "description": "Exact snippet from the document supporting the value"},
        },
        "required": ["value", "confidence", "quote"],
        "additionalProperties": False,
    }


def _node_schema(node: FieldDef) -> dict:
    if node.data_type == "object":
        props = {c.key: _node_schema(c) for c in node.fields}
        return {
            "type": "object",
            "description": f"{node.name}: {node.description}".strip().rstrip(":"),
            "properties": props,
            "required": list(props.keys()),
            "additionalProperties": False,
        }
    if node.data_type == "list":
        return {
            "type": "array",
            "description": f"{node.name}: {node.description}".strip().rstrip(":"),
            "items": _node_schema(node.item),  # type: ignore[arg-type]
        }
    return _leaf_schema(node)


def compile_json_schema(defs: list[FieldDef], schema_name: str = "extraction") -> dict:
    """Strict JSON Schema for provider structured outputs."""
    props = {d.key: _node_schema(d) for d in defs}
    return {
        "name": re.sub(r"[^a-zA-Z0-9_]", "_", schema_name)[:60] or "extraction",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": props,
            "required": list(props.keys()),
            "additionalProperties": False,
        },
    }


# ------------------------------------------------------------------ flatten
@dataclass
class FlatValue:
    """One row destined for `field_values`."""

    field_key: str
    field_path: str
    node_kind: str  # scalar | object | list
    data_type: str
    name: str
    parent_path: str | None = None
    ordinal: int | None = None
    raw_value: str | None = None
    confidence: float = 0.0
    quote: str | None = None
    threshold: float | None = None
    children: list["FlatValue"] = dc_field(default_factory=list)


def _coerce_leaf(payload: Any) -> tuple[str | None, float, str | None]:
    """Accept {value, confidence, quote} or a bare scalar (lenient providers)."""
    if isinstance(payload, dict):
        raw = payload.get("value")
        conf = payload.get("confidence", 0.0)
        quote = payload.get("quote")
    else:
        raw, conf, quote = payload, 0.0, None
    if raw is not None and not isinstance(raw, str):
        raw = str(raw)
    if isinstance(raw, str) and not raw.strip():
        raw = None
    try:
        conf = float(conf)
    except (TypeError, ValueError):
        conf = 0.0
    return raw, max(0.0, min(1.0, conf)), (quote if isinstance(quote, str) and quote.strip() else None)


def flatten_response(defs: list[FieldDef], data: dict, parent_path: str = "") -> list[FlatValue]:
    """Turn a model response into a tree of FlatValue rows keyed by field_path."""
    out: list[FlatValue] = []
    if not isinstance(data, dict):
        return out

    for node in defs:
        if node.key not in data:
            continue
        payload = data[node.key]
        path = f"{parent_path}.{node.key}" if parent_path else node.key

        if node.data_type == "object":
            if not isinstance(payload, dict):
                continue
            container = FlatValue(
                field_key=node.key, field_path=path, node_kind="object", data_type="object",
                name=node.name, parent_path=parent_path or None)
            container.children = flatten_response(node.fields, payload, path)
            if container.children:
                out.append(container)

        elif node.data_type == "list":
            if not isinstance(payload, list):
                continue
            container = FlatValue(
                field_key=node.key, field_path=path, node_kind="list", data_type="list",
                name=node.name, parent_path=parent_path or None)
            item_def = node.item  # type: ignore[assignment]
            for idx, element in enumerate(payload):
                item_path = f"{path}[{idx}]"
                if item_def.data_type == "object":
                    if not isinstance(element, dict):
                        continue
                    item = FlatValue(
                        field_key=item_def.key, field_path=item_path, node_kind="object",
                        data_type="object", name=f"{node.name} #{idx + 1}",
                        parent_path=path, ordinal=idx)
                    item.children = flatten_response(item_def.fields, element, item_path)
                    if item.children:
                        container.children.append(item)
                else:
                    raw, conf, quote = _coerce_leaf(element)
                    if raw is None:
                        continue
                    container.children.append(FlatValue(
                        field_key=item_def.key, field_path=item_path, node_kind="scalar",
                        data_type=item_def.data_type, name=f"{node.name} #{idx + 1}",
                        parent_path=path, ordinal=idx, raw_value=raw, confidence=conf,
                        quote=quote, threshold=item_def.threshold))
            if container.children:
                out.append(container)

        else:
            raw, conf, quote = _coerce_leaf(payload)
            out.append(FlatValue(
                field_key=node.key, field_path=path, node_kind="scalar", data_type=node.data_type,
                name=node.name, parent_path=parent_path or None, raw_value=raw,
                confidence=conf, quote=quote, threshold=node.threshold))
    return out


def iter_flat(values: list[FlatValue]):
    """Depth-first walk yielding every node (containers included)."""
    for v in values:
        yield v
        if v.children:
            yield from iter_flat(v.children)


def describe_for_prompt(defs: list[FieldDef], indent: int = 0) -> str:
    """Human-readable outline of the schema for the prompt/agent instructions."""
    lines = []
    pad = "  " * indent
    for d in defs:
        if d.data_type == "object":
            lines.append(f"{pad}- {d.name} ({d.key}) — object: {d.description}")
            lines.append(describe_for_prompt(d.fields, indent + 1))
        elif d.data_type == "list":
            item = d.item
            kind = "objects" if item.data_type == "object" else item.data_type
            lines.append(f"{pad}- {d.name} ({d.key}) — list of {kind}: {d.description}")
            if item.data_type == "object":
                lines.append(describe_for_prompt(item.fields, indent + 1))
        else:
            lines.append(f"{pad}- {d.name} ({d.key}) — {d.data_type}: {d.description}")
    return "\n".join(l for l in lines if l)
