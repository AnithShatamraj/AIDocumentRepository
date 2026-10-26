"""Turn an extracted field tree into `field_values` / `review_items` rows.

Shared by the pipeline's metadata stage (every field of a type, on a sync
session) and by targeted extraction (a few fields of one document, on the API's
async session), so a value is stored the same way whichever path produced it:
same ids, same normalisation, same auto-accept rule, same provenance.

Pure: it builds ORM objects and returns them. The caller adds them to its own
session. Row ids are deterministic (derived from document, version, type and
path), so a child can name its parent before anything is flushed, and
re-extracting the same field yields the same ids.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.models.constants import REVIEW_EXTRACTION, RV_AUTO_ACCEPTED, RV_PENDING, VT_TEXT
from app.models.content import FieldValue
from app.models.review import ReviewItem
from app.services import bbox_locator, normalize
from app.services.fields import FlatValue


@dataclass
class BuiltRows:
    fields: list[FieldValue] = field(default_factory=list)   # parents before children
    reviews: list[ReviewItem] = field(default_factory=list)  # add after `fields` are flushed
    leaves: int = 0                                          # scalar values written
    pending: int = 0                                         # of those, awaiting review


def row_id(document_id, document_version, doc_type_id, field_path: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{document_id}:{document_version}:{doc_type_id}:{field_path}")


def build_rows(
    tree: list[FlatValue], *, tenant_id, document_id, document_version, doc_type_id, doc_type_name: str,
    schema_version: int | None, model: str, prompt_version: str, layout_units, autoaccept: float,
) -> BuiltRows:
    out = BuiltRows()

    def persist(node: FlatValue, parent_id) -> None:
        is_leaf = node.node_kind == "scalar"
        norm = None
        conf = node.confidence
        status = RV_AUTO_ACCEPTED
        loc = None

        if is_leaf:
            norm = normalize.normalize(node.raw_value, normalize.NORMALIZER_TYPE.get(node.data_type, VT_TEXT))
            # A value we couldn't normalize is less trustworthy.
            conf = node.confidence * (1.0 if norm.ok else 0.6)
            threshold = node.threshold if node.threshold is not None else autoaccept
            status = RV_AUTO_ACCEPTED if (conf >= threshold and node.raw_value) else RV_PENDING
            # Per-leaf provenance: the model's quote first, the value as fallback.
            loc = bbox_locator.locate_any(layout_units, [node.quote, node.raw_value], None)

        row = FieldValue(
            id=row_id(document_id, document_version, doc_type_id, node.field_path),
            tenant_id=tenant_id, document_id=document_id, document_version=document_version,
            document_type_id=doc_type_id, parent_id=parent_id, field_key=node.field_key,
            field_path=node.field_path, field_name=node.name, node_kind=node.node_kind,
            ordinal=node.ordinal, data_type=node.data_type, raw_value=node.raw_value,
            value_type=(norm.value_type if norm else node.data_type),
            value_text=(norm.value_text if norm else None),
            value_number=(norm.value_number if norm else None),
            value_date=(norm.value_date if norm else None),
            value_currency=(norm.value_currency if norm else None),
            confidence=round(conf, 3),
            source_page=(loc.get("page") if loc else None),
            source_text=node.quote, source_bbox=loc,
            review_status=status, model_version=model, prompt_version=prompt_version,
            schema_version=schema_version)
        out.fields.append(row)

        if is_leaf:
            out.leaves += 1
            if status == RV_PENDING:
                out.pending += 1
                out.reviews.append(ReviewItem(
                    tenant_id=tenant_id, document_id=document_id, kind=REVIEW_EXTRACTION,
                    field_value_id=row.id, field_name=node.field_path, confidence=round(conf, 3),
                    status=RV_PENDING,
                    payload={"document_type": doc_type_name, "field_path": node.field_path,
                             "raw_value": node.raw_value,
                             "normalized_ok": bool(norm.ok) if norm else None}))

        for child in node.children:
            persist(child, row.id)

    for node in tree:
        persist(node, None)
    return out
