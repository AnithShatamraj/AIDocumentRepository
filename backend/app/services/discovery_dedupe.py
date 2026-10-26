"""Find "new" (discovered) fields that only restate something already read.

The pipeline now keeps a type's discovery pass from proposing what another type
already extracted. Documents processed before that can still carry such rows --
a "Security Deposit Amount" suggestion beside the lease's own "Deposit Amount" --
and this finds and removes them with the same test the pipeline applies.

It only ever touches suggestions nobody has accepted (pending or rejected). A
discovered field a person verified or corrected is kept, and counts as known so
later duplicates of *it* are still caught.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.ai.agents.structured import matching_known
from app.models.constants import DOC_NEEDS_REVIEW, DOC_PROCESSED, RV_PENDING, RV_REJECTED
from app.models.content import FieldValue
from app.models.document import Document
from app.models.review import ReviewItem

PRUNABLE = {RV_PENDING, RV_REJECTED}


@dataclass
class Candidate:
    id: object
    name: str
    value: str
    status: str
    confidence: float = 0.0


@dataclass
class Redundant:
    candidate: Candidate
    restates: tuple[str, str]  # the (label, value) it duplicates


def plan_redundant(candidates: list[Candidate], known: list[tuple[str, str]]) -> list[Redundant]:
    """Which discovered rows to drop. `known` is what the document's types extracted.

    The most confident suggestion of several that say the same thing is the one kept."""
    kept: list[tuple[str, str]] = [(c.name, c.value) for c in candidates if c.status not in PRUNABLE]
    out: list[Redundant] = []
    for c in sorted((c for c in candidates if c.status in PRUNABLE), key=lambda c: (-c.confidence, c.name)):
        hit = matching_known(c.name, c.value, known + kept)
        if hit is None:
            kept.append((c.name, c.value))
        else:
            out.append(Redundant(c, hit))
    return out


def prune_document(db: Session, document_id, *, execute: bool) -> list[Redundant]:
    """Plan (and, with `execute`, remove) the redundant discovered rows of one document."""
    doc = db.get(Document, document_id)
    if doc is None:
        return []
    rows = db.execute(select(FieldValue).where(
        FieldValue.document_id == doc.id, FieldValue.document_version == doc.current_version,
        FieldValue.node_kind == "scalar", FieldValue.raw_value.is_not(None))).scalars().all()
    known = [(r.field_name, r.raw_value) for r in rows if not r.is_discovered and r.raw_value]
    candidates = [Candidate(r.id, r.field_name, r.raw_value, r.review_status, r.confidence or 0.0)
                  for r in rows if r.is_discovered]
    plan = plan_redundant(candidates, known)
    if execute and plan:
        db.execute(delete(FieldValue).where(FieldValue.id.in_([p.candidate.id for p in plan])))
        db.flush()
        # Removing its last pending suggestion can leave nothing to review.
        pending = db.execute(select(func.count()).select_from(ReviewItem).where(
            ReviewItem.document_id == doc.id, ReviewItem.status == RV_PENDING)).scalar() or 0
        if pending == 0 and doc.processing_status == DOC_NEEDS_REVIEW:
            doc.processing_status = DOC_PROCESSED
    return plan
