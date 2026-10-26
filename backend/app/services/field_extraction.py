"""Targeted extraction: read only some of a type's fields from one document.

The pipeline extracts every field of a type, and re-running it deletes all of a
document's values first -- including ones a person has reviewed or corrected.
That is the wrong tool when a field has just been added to a type: only that
field is missing. This reads just those fields from the text already stored for
the document and returns rows to add, leaving everything else alone.

Same extractor, same row builder and same auto-accept rule as the pipeline.
The extractor is a blocking call, so it runs in a worker thread.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import prompts
from app.ai.agents.structured import extract_structured
from app.ai.registry import is_offline
from app.core.config import settings
from app.models.catalog import DocumentType, TypeSchema
from app.models.constants import STAGE_METADATA
from app.models.content import FieldValue
from app.models.document import Document, DocumentVersion
from app.models.ops import CostRecord
from app.services import extraction_rows
from app.services.fields import FieldDef, FlatValue, flatten_response


class FieldExtractionError(Exception):
    """Why a targeted extraction couldn't run; `status` is the HTTP code to answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


async def extract_fields(
    db: AsyncSession, doc: Document, dt: DocumentType, defs: list[FieldDef], schema_version: int | None,
) -> extraction_rows.BuiltRows:
    """Rows for `defs`, read from `doc`'s current text. Adds a cost record; the
    caller adds the rows and owns the transaction."""
    if not defs:
        return extraction_rows.BuiltRows()
    if is_offline():
        raise FieldExtractionError(400, "Reading fields from the document needs an AI provider, and none is configured.")

    version = (await db.execute(select(DocumentVersion).where(
        DocumentVersion.document_id == doc.id, DocumentVersion.version == doc.current_version))).scalars().first()
    text = (version.extracted_text if version else "") or ""
    if not text.strip():
        raise FieldExtractionError(409, "This document has no extracted text to read the field from.")
    layout_units = ((version.understanding if version else None) or {}).get("units", [])

    try:
        payload, usage = await asyncio.to_thread(extract_structured, text, defs, dt.name)
    except Exception as e:  # noqa: BLE001  provider outage, bad key, ...
        raise FieldExtractionError(502, f"The AI provider couldn't read the document just now ({type(e).__name__}). "
                                        "Nothing was changed; please try again.")
    db.add(CostRecord(
        tenant_id=doc.tenant_id, document_id=doc.id, stage=STAGE_METADATA,
        provider=settings.effective_ai_provider, model=usage.model, prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens, cost_usd=usage.cost_usd))

    return extraction_rows.build_rows(
        flatten_response(defs, payload), tenant_id=doc.tenant_id, document_id=doc.id,
        document_version=doc.current_version, doc_type_id=dt.id, doc_type_name=dt.name,
        schema_version=schema_version, model=usage.model, prompt_version=prompts.PROMPT_VERSIONS["extract"],
        layout_units=layout_units, autoaccept=settings.extract_autoaccept_threshold)


async def new_fields_for_document(db: AsyncSession, doc: Document, dt: DocumentType) -> tuple[list[dict], TypeSchema | None]:
    """Top-level fields `dt` gained after this document was extracted, that it has no values for.

    A document's values record the schema version they were read under. The oldest
    one marks when its extraction happened, so a field is "new" for the document if
    it is in the type's current schema, wasn't in that older version, and has no
    value yet. (Fields that exist but came back empty are not new -- they were
    asked for already -- so this never offers to re-read them.)
    """
    schema = await db.get(TypeSchema, dt.active_schema_id) if dt.active_schema_id else None
    if schema is None or not schema.fields:
        return [], schema
    scope = (FieldValue.document_id == doc.id, FieldValue.document_version == doc.current_version,
             FieldValue.document_type_id == dt.id, FieldValue.is_discovered.is_(False))
    oldest = (await db.execute(select(func.min(FieldValue.schema_version)).where(*scope))).scalar()
    if oldest is None:
        return [], schema  # never extracted under this type: the pipeline's job, not ours
    have = set((await db.execute(
        select(FieldValue.field_key).where(*scope, FieldValue.parent_id.is_(None)))).scalars().all())
    base = await db.execute(select(TypeSchema).where(
        TypeSchema.document_type_id == dt.id, TypeSchema.version == oldest))
    base_schema = base.scalars().first()
    base_keys = {f["key"] for f in base_schema.fields} if base_schema else {f["key"] for f in schema.fields}
    return [f for f in schema.fields if f["key"] not in base_keys and f["key"] not in have], schema


def empty_containers(defs: list[FieldDef], built: extraction_rows.BuiltRows) -> list[FlatValue]:
    """Containers for list/group fields the model returned nothing for.

    A list with no entries produces no rows, which would make the field look
    "new" again forever. An empty container records that it was asked for and
    found empty (the page shows "None found.")."""
    present = {f.field_path for f in built.fields}
    return [FlatValue(field_key=d.key, field_path=d.key, node_kind=d.data_type, data_type=d.data_type, name=d.name)
            for d in defs if d.data_type in ("list", "object") and d.key not in present]
