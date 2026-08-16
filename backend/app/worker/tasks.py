"""The document processing pipeline as a Celery chain of idempotent stage tasks.

Design invariants (from the spec):
  * per-stage state persisted in Postgres, resumable from the failed stage,
  * idempotent: reprocessing yields identical chunk/embedding/metadata state,
  * low-confidence classification -> Uncategorized + review, extraction skipped,
  * version-aware indexing: new chunks written, prior version's deleted in one tx,
  * every AI output records model + prompt version; per-stage cost tracked,
  * dead-letter: exhausted retries land the doc in a visible failed state.
"""
from __future__ import annotations

import datetime as dt
import traceback
import uuid

from celery import chain
from sqlalchemy import delete, select

from app.ai import ingestion, prompts
from app.ai.registry import get_embedder
from app.core.config import settings
from app.core.db import SyncSessionLocal
from app.core.logging import correlation_id, get_logger
from app.core.redis import publish_doc_event
from app.models.catalog import DocumentType, TypeSchema
from app.models.constants import (
    DOC_FAILED,
    DOC_NEEDS_REVIEW,
    DOC_PROCESSED,
    DOC_PROCESSING,
    NOTIFY_PROCESSING_DONE,
    NOTIFY_PROCESSING_FAILED,
    NOTIFY_REVIEW_PENDING,
    PIPELINE_STAGES,
    REVIEW_CLASSIFICATION,
    REVIEW_EXTRACTION,
    RV_AUTO_ACCEPTED,
    RV_PENDING,
    ST_FAILED,
    ST_PENDING,
    ST_RUNNING,
    ST_SKIPPED,
    ST_SUCCEEDED,
    STAGE_CHUNK,
    STAGE_CLASSIFY,
    STAGE_EMBED,
    STAGE_EXTRACT,
    STAGE_METADATA,
    STAGE_SUMMARIZE,
    STAGE_UNDERSTAND,
    VT_TEXT,
)
from app.models.content import Chunk, Classification, Embedding, FieldValue, Summary
from app.models.document import (
    DocumentTypeDefaultPermission,
    Document,
    DocumentPermission,
    DocumentVersion,
    document_type_links,
)
from app.models.ops import CostRecord, Notification
from app.models.pipeline import PipelineRun, PipelineStage
from app.models.review import ReviewItem
from app.services import bbox_locator, chunking, normalize, parsing
from app.storage import get_storage
from app.worker.celery_app import celery_app

log = get_logger(__name__)
MAX_RETRIES = 2


# --------------------------------------------------------------------- helpers
def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _get_stage(db, run_id, name) -> PipelineStage:
    return db.execute(
        select(PipelineStage).where(PipelineStage.run_id == run_id, PipelineStage.name == name)
    ).scalar_one()


def _latest_version(db, document_id, version) -> DocumentVersion:
    return db.execute(
        select(DocumentVersion).where(
            DocumentVersion.document_id == document_id, DocumentVersion.version == version
        )
    ).scalar_one()


def _publish(run: PipelineRun, stage: str, status: str, extra: dict | None = None) -> None:
    publish_doc_event(
        run.document_id,
        {"type": "stage", "document_id": str(run.document_id), "run_id": str(run.id),
         "stage": stage, "status": status, "ts": _now().isoformat(), **(extra or {})},
    )


def _record_cost(db, run: PipelineRun, stage: str, out) -> None:
    tokens = getattr(out, "prompt_tokens", 0) + getattr(out, "completion_tokens", 0)
    cost = getattr(out, "cost_usd", 0.0)
    if tokens or cost:
        db.add(CostRecord(
            tenant_id=run.tenant_id, document_id=run.document_id, stage=stage,
            provider=settings.effective_ai_provider, model=getattr(out, "model", ""),
            prompt_tokens=getattr(out, "prompt_tokens", 0),
            completion_tokens=getattr(out, "completion_tokens", 0), cost_usd=cost,
        ))
        run.total_tokens += tokens
        run.total_cost_usd += cost
    return tokens, cost


def _run_stage(task, run_id: str, name: str, work):
    """Shared stage lifecycle: timing, status, checkpoint, retry, dead-letter."""
    db = SyncSessionLocal()
    try:
        run = db.get(PipelineRun, uuid.UUID(run_id))
        if run.correlation_id:
            correlation_id.set(run.correlation_id)
        stage = _get_stage(db, run.id, name)
        stage.status = ST_RUNNING
        stage.started_at = _now()
        stage.retries = task.request.retries
        db.commit()
        _publish(run, name, ST_RUNNING)

        started = _now()
        try:
            output = work(db, run, stage) or {}
            # Preserve a status the work explicitly set (e.g. SKIPPED); default to SUCCEEDED.
            if stage.status == ST_RUNNING:
                stage.status = ST_SUCCEEDED
            stage.finished_at = _now()
            stage.latency_ms = int((stage.finished_at - started).total_seconds() * 1000)
            stage.output = output
            db.commit()
            _publish(run, name, ST_SUCCEEDED, {"output": output})
        except Exception as e:  # noqa: BLE001
            db.rollback()
            try:
                stage = _get_stage(db, run.id, name)
                stage.status = ST_FAILED
                stage.error_class = type(e).__name__
                stage.error_message = str(e)[:2000]
                stage.traceback_ref = traceback.format_exc()[-4000:]
                stage.finished_at = _now()
                db.commit()
            except Exception:
                # Recording the failure must never itself leave the stage
                # wedged at 'running' forever — fall back to the bare minimum
                # status flip if the rich error details can't be persisted
                # (e.g. an unexpected column-size or serialization issue).
                db.rollback()
                log.error("stage_failure_bookkeeping_failed", stage=name, run_id=run_id,
                          exc_info=True)
                stage = _get_stage(db, run.id, name)
                stage.status = ST_FAILED
                stage.finished_at = _now()
                db.commit()
            _publish(run, name, ST_FAILED, {"error": str(e)})
            log.error("stage_failed", stage=name, run_id=run_id, error=str(e))

            if task.request.retries < MAX_RETRIES:
                raise task.retry(exc=e, countdown=5 * (task.request.retries + 1))
            _dead_letter(db, run, name, e)
            raise
        return run_id
    finally:
        db.close()


def _dead_letter(db, run: PipelineRun, stage: str, exc: Exception) -> None:
    run.status = ST_FAILED
    run.finished_at = _now()
    doc = db.get(Document, run.document_id)
    if doc:
        doc.processing_status = DOC_FAILED
        db.add(Notification(
            tenant_id=doc.tenant_id, user_id=doc.owner_id, kind=NOTIFY_PROCESSING_FAILED,
            title="Processing failed", body=f"'{doc.name}' failed at {stage}: {exc}",
            link=f"/documents/{doc.id}",
        ))
    db.commit()
    _publish(run, stage, "dead_letter", {"error": str(exc)})


# ------------------------------------------------------------- orchestration
@celery_app.task(name="pipeline.start")
def start_processing(document_id: str, from_stage: str | None = None, corr_id: str | None = None) -> str:
    db = SyncSessionLocal()
    try:
        doc = db.get(Document, uuid.UUID(document_id))
        if doc is None:
            return ""
        run = PipelineRun(
            tenant_id=doc.tenant_id, document_id=doc.id, document_version=doc.current_version,
            status=ST_RUNNING, started_from_stage=from_stage, correlation_id=corr_id or str(uuid.uuid4()),
            started_at=_now(),
        )
        db.add(run)
        db.flush()

        # This run regenerates the document's AI outputs, so any still-pending
        # review items from earlier runs are superseded — drop them or the queue
        # accumulates stale entries and the document never leaves needs_review.
        # (Already-resolved items are kept as the audit/learning record.)
        db.execute(delete(ReviewItem).where(
            ReviewItem.document_id == doc.id, ReviewItem.status == RV_PENDING))

        start_index = PIPELINE_STAGES.index(from_stage) if from_stage else 0
        for i, name in enumerate(PIPELINE_STAGES):
            db.add(PipelineStage(
                run_id=run.id, ordinal=i, name=name,
                status=ST_PENDING if i >= start_index else ST_SKIPPED,
            ))
        doc.processing_status = DOC_PROCESSING
        db.commit()
        run_id = str(run.id)
    finally:
        db.close()

    tasks_by_name = {
        STAGE_EXTRACT: stage_text_extraction, STAGE_UNDERSTAND: stage_understanding,
        STAGE_CLASSIFY: stage_classification, STAGE_SUMMARIZE: stage_summarization,
        STAGE_METADATA: stage_metadata, STAGE_CHUNK: stage_chunking, STAGE_EMBED: stage_embedding,
    }
    sig = chain(*[tasks_by_name[name].si(run_id) for name in PIPELINE_STAGES[start_index:]])
    sig.apply_async()
    return run_id


# ------------------------------------------------------------------- stages
@celery_app.task(bind=True, name="pipeline.text_extraction", max_retries=MAX_RETRIES)
def stage_text_extraction(self, run_id: str) -> str:
    def work(db, run, stage):
        version = _latest_version(db, run.document_id, run.document_version)
        data = get_storage().get_bytes(version.storage_key)
        doc = db.get(Document, run.document_id)
        parsed = parsing.parse(data, doc.file_type)
        text = parsed.get("text", "")
        if doc.file_type == "pdf" and not text.strip():
            # A PDF that yields no text after docling(+OCR)+fallback is a real
            # failure — fail loudly (dead-letter) instead of running the AI
            # stages on empty input. (Images legitimately yield no text until
            # their OCR path is enabled, so this guard is PDF-only.)
            raise RuntimeError(
                f"PDF text extraction produced no text ({parsed.get('warning') or 'no parser output'})")
        version.extracted_text = text
        version.understanding = parsed
        db.add(version)
        return {"chars": len(text), "units": len(parsed.get("units", [])),
                "anchor_type": parsed.get("anchor_type"), "warning": parsed.get("warning"),
                "parser": parsed.get("parser"), "ocr_used": parsed.get("ocr_used")}
    return _run_stage(self, run_id, STAGE_EXTRACT, work)


@celery_app.task(bind=True, name="pipeline.document_understanding", max_retries=MAX_RETRIES)
def stage_understanding(self, run_id: str) -> str:
    def work(db, run, stage):
        version = _latest_version(db, run.document_id, run.document_version)
        u = version.understanding or {}
        units = u.get("units", [])
        return {"anchor_type": u.get("anchor_type"), "unit_count": len(units),
                "has_positional": any("words" in x for x in units)}
    return _run_stage(self, run_id, STAGE_UNDERSTAND, work)


@celery_app.task(bind=True, name="pipeline.classification", max_retries=MAX_RETRIES)
def stage_classification(self, run_id: str) -> str:
    def work(db, run, stage):
        version = _latest_version(db, run.document_id, run.document_version)
        text = version.extracted_text or ""
        doc = db.get(Document, run.document_id)

        # Idempotent: clear prior AI classifications + type links for this doc.
        db.execute(delete(Classification).where(
            Classification.document_id == run.document_id, Classification.source == "ai"))
        db.execute(document_type_links.delete().where(
            document_type_links.c.document_id == run.document_id))

        # The uploader pinned a type -> honour it and skip prediction entirely.
        if doc.requested_type_id:
            chosen = db.get(DocumentType, doc.requested_type_id)
            if chosen is not None:
                db.add(Classification(
                    tenant_id=run.tenant_id, document_id=run.document_id,
                    document_type_id=chosen.id, label=chosen.name, confidence=1.0,
                    source="human", is_override=True))
                db.execute(document_type_links.insert().values(
                    document_id=run.document_id, document_type_id=chosen.id))
                _apply_type_defaults(db, run.tenant_id, run.document_id, chosen.id)
                return {"labels": [{"category": chosen.name, "confidence": 1.0}],
                        "assigned": [chosen.name], "skip_extraction": False,
                        "strategy": "user_selected"}

        cats = db.execute(
            select(DocumentType).where(DocumentType.tenant_id == run.tenant_id, DocumentType.is_enabled.is_(True))
        ).scalars().all()
        cat_by_name = {c.name: c for c in cats}
        result = ingestion.classify_document(text, list(cat_by_name.keys()))

        threshold = settings.classify_confidence_threshold
        assigned = []
        for lab in result.labels:
            cat = cat_by_name.get(lab.category)
            db.add(Classification(
                tenant_id=run.tenant_id, document_id=run.document_id,
                document_type_id=cat.id if cat else None, label=lab.category, confidence=lab.confidence,
                source="ai", model_version=result.model, prompt_version=result.prompt_version))
            if cat and lab.confidence >= threshold:
                db.execute(document_type_links.insert().values(
                    document_id=run.document_id, document_type_id=cat.id))
                _apply_type_defaults(db, run.tenant_id, run.document_id, cat.id)
                assigned.append(lab.category)

        stage.model_version = result.model
        stage.prompt_version = result.prompt_version
        _record_cost(db, run, STAGE_CLASSIFY, result)

        skip_extraction = len(assigned) == 0
        if skip_extraction:
            doc = db.get(Document, run.document_id)
            doc.processing_status = DOC_NEEDS_REVIEW
            db.add(ReviewItem(
                tenant_id=run.tenant_id, document_id=run.document_id, kind=REVIEW_CLASSIFICATION,
                confidence=max((l.confidence for l in result.labels), default=0.0),
                status=RV_PENDING,
                payload={"predicted": [l.__dict__ for l in result.labels], "reason": "below_threshold"}))
            _notify_reviewers(db, run.tenant_id, run.document_id, "New document needs categorization")
        return {"labels": [l.__dict__ for l in result.labels], "assigned": assigned,
                "skip_extraction": skip_extraction, "strategy": result.strategy}
    return _run_stage(self, run_id, STAGE_CLASSIFY, work)


@celery_app.task(bind=True, name="pipeline.summarization", max_retries=MAX_RETRIES)
def stage_summarization(self, run_id: str) -> str:
    def work(db, run, stage):
        version = _latest_version(db, run.document_id, run.document_version)
        result = ingestion.summarize_document(version.extracted_text or "")
        db.execute(delete(Summary).where(
            Summary.document_id == run.document_id, Summary.document_version == run.document_version))
        db.add(Summary(
            tenant_id=run.tenant_id, document_id=run.document_id, document_version=run.document_version,
            executive_summary=result.executive_summary, highlights=result.highlights,
            model_version=result.model, prompt_version=result.prompt_version))
        stage.model_version = result.model
        stage.prompt_version = result.prompt_version
        _record_cost(db, run, STAGE_SUMMARIZE, result)
        return {"strategy": result.strategy, "highlights": len(result.highlights)}
    return _run_stage(self, run_id, STAGE_SUMMARIZE, work)


@celery_app.task(bind=True, name="pipeline.metadata_extraction", max_retries=MAX_RETRIES)
def stage_metadata(self, run_id: str) -> str:
    def work(db, run, stage):
        version = _latest_version(db, run.document_id, run.document_version)
        text = version.extracted_text or ""
        layout_units = (version.understanding or {}).get("units", [])
        assigned_cats = db.execute(
            select(DocumentType).join(document_type_links, document_type_links.c.document_type_id == DocumentType.id)
            .where(document_type_links.c.document_id == run.document_id)
        ).scalars().all()

        if not assigned_cats:  # low-confidence path: extraction skipped
            stage.status = ST_SKIPPED
            return {"skipped": True, "reason": "uncategorized"}

        db.execute(delete(FieldValue).where(
            FieldValue.document_id == run.document_id, FieldValue.document_version == run.document_version))

        total_fields, review_items = 0, 0
        for cat in assigned_cats:
            schema = db.get(TypeSchema, cat.active_schema_id) if cat.active_schema_id else None
            if not schema or not schema.fields:
                continue
            counts = _extract_for_type(db, run, stage, cat, schema, text, layout_units)
            total_fields += counts[0]
            review_items += counts[1]

        if review_items:
            doc = db.get(Document, run.document_id)
            if doc.processing_status != DOC_FAILED:
                doc.processing_status = DOC_NEEDS_REVIEW
            _notify_reviewers(db, run.tenant_id, run.document_id,
                              f"{review_items} extracted field(s) need review")
        return {"fields": total_fields, "pending_review": review_items}
    return _run_stage(self, run_id, STAGE_METADATA, work)


@celery_app.task(bind=True, name="pipeline.chunking", max_retries=MAX_RETRIES)
def stage_chunking(self, run_id: str) -> str:
    def work(db, run, stage):
        version = _latest_version(db, run.document_id, run.document_version)
        units = (version.understanding or {}).get("units", [])
        drafts = chunking.chunk_units(units, settings.chunk_tokens, settings.chunk_overlap_tokens)

        # Idempotent upsert for the current version: delete + reinsert deterministically.
        db.execute(delete(Chunk).where(
            Chunk.document_id == run.document_id, Chunk.document_version == run.document_version))
        for d in drafts:
            cid = chunking.deterministic_chunk_id(run.document_id, run.document_version, d.ordinal, d.content)
            db.add(Chunk(
                id=cid, tenant_id=run.tenant_id, document_id=run.document_id,
                document_version=run.document_version, ordinal=d.ordinal, content=d.content,
                page=d.page, anchor=d.anchor, section_title=d.section_title, bbox=d.bbox))
        return {"chunks": len(drafts)}
    return _run_stage(self, run_id, STAGE_CHUNK, work)


@celery_app.task(bind=True, name="pipeline.embedding", max_retries=MAX_RETRIES)
def stage_embedding(self, run_id: str) -> str:
    def work(db, run, stage):
        chunks = db.execute(
            select(Chunk).where(
                Chunk.document_id == run.document_id, Chunk.document_version == run.document_version
            ).order_by(Chunk.ordinal)
        ).scalars().all()

        embedder = get_embedder()
        if chunks:
            texts = [c.content for c in chunks]
            # Batch to stay within provider limits.
            vectors: list[list[float]] = []
            model_name = settings.embedding_model
            for i in range(0, len(texts), 64):
                res = embedder.embed(texts[i:i + 64], model=model_name)
                vectors.extend(res.vectors)
                model_name = res.model
                run.total_cost_usd += res.cost_usd
                run.total_tokens += res.tokens
            db.execute(delete(Embedding).where(Embedding.document_id == run.document_id))
            for c, vec in zip(chunks, vectors):
                db.add(Embedding(
                    tenant_id=run.tenant_id, chunk_id=c.id, document_id=run.document_id,
                    vector=vec, embedding_model=model_name, embedding_dim=len(vec)))

        # Version-aware swap: drop prior-version chunks (cascades embeddings) so
        # retrieval flips atomically from old to new — never a mixed state.
        db.execute(delete(Chunk).where(
            Chunk.document_id == run.document_id, Chunk.document_version != run.document_version))

        # Finalize run + document status.
        run.status = ST_SUCCEEDED
        run.finished_at = _now()
        doc = db.get(Document, run.document_id)
        pending = db.execute(
            select(ReviewItem.id).where(ReviewItem.document_id == run.document_id,
                                        ReviewItem.status == RV_PENDING).limit(1)
        ).first()
        doc.processing_status = DOC_NEEDS_REVIEW if pending else DOC_PROCESSED
        db.add(Notification(
            tenant_id=doc.tenant_id, user_id=doc.owner_id, kind=NOTIFY_PROCESSING_DONE,
            title="Processing complete", body=f"'{doc.name}' finished processing.",
            link=f"/documents/{doc.id}"))
        _publish(run, "pipeline", "completed", {"status": doc.processing_status})
        return {"embedded": len(chunks)}
    return _run_stage(self, run_id, STAGE_EMBED, work)


# ------------------------------------------------------- schema-driven extract
# Legacy value_type names the normalizer understands, keyed by schema data_type.
_NORMALIZER_TYPE = {
    "string": VT_TEXT, "number": "numeric", "integer": "numeric", "currency": "currency",
    "date": "date", "datetime": "date", "time": VT_TEXT, "boolean": VT_TEXT,
}


def _threshold_for(field_key: str, default: float, overrides: dict) -> float:
    return float(overrides.get(field_key, default))


def _extract_for_type(db, run, stage, doc_type, schema, text, layout_units) -> tuple[int, int]:
    """Extract one document type's field tree into `field_values` rows."""
    from app.ai.agents.structured import discover_entities, extract_structured
    from app.services import fields as fieldsvc

    defs = fieldsvc.validate_schema(schema.fields)
    payload, usage = extract_structured(text, defs, doc_type.name)
    stage.model_version = usage.model
    stage.prompt_version = prompts.PROMPT_VERSIONS["extract"]
    _record_cost(db, run, STAGE_METADATA, usage)

    tree = fieldsvc.flatten_response(defs, payload)
    autoaccept = settings.extract_autoaccept_threshold
    written = review_items = 0

    def persist(node: fieldsvc.FlatValue, parent_id) -> None:
        nonlocal written, review_items
        is_leaf = node.node_kind == "scalar"
        norm = None
        conf = node.confidence
        status = RV_AUTO_ACCEPTED
        loc = None

        if is_leaf:
            norm = normalize.normalize(node.raw_value, _NORMALIZER_TYPE.get(node.data_type, VT_TEXT))
            # A value we couldn't normalize is less trustworthy.
            conf = node.confidence * (1.0 if norm.ok else 0.6)
            threshold = node.threshold if node.threshold is not None else autoaccept
            status = RV_AUTO_ACCEPTED if (conf >= threshold and node.raw_value) else RV_PENDING
            # Per-leaf provenance: the model's quote first, the value as fallback.
            loc = bbox_locator.locate_any(layout_units, [node.quote, node.raw_value], None)

        row = FieldValue(
            id=uuid.uuid5(uuid.NAMESPACE_URL,
                          f"{run.document_id}:{run.document_version}:{doc_type.id}:{node.field_path}"),
            tenant_id=run.tenant_id, document_id=run.document_id,
            document_version=run.document_version, document_type_id=doc_type.id,
            parent_id=parent_id, field_key=node.field_key, field_path=node.field_path,
            field_name=node.name, node_kind=node.node_kind, ordinal=node.ordinal,
            data_type=node.data_type,
            raw_value=node.raw_value,
            value_type=(norm.value_type if norm else node.data_type),
            value_text=(norm.value_text if norm else None),
            value_number=(norm.value_number if norm else None),
            value_date=(norm.value_date if norm else None),
            value_currency=(norm.value_currency if norm else None),
            confidence=round(conf, 3),
            source_page=(loc.get("page") if loc else None),
            source_text=node.quote, source_bbox=loc,
            review_status=status, model_version=usage.model,
            prompt_version=prompts.PROMPT_VERSIONS["extract"], schema_version=schema.version)
        db.add(row)
        db.flush()

        if is_leaf:
            written += 1
            if status == RV_PENDING:
                db.add(ReviewItem(
                    tenant_id=run.tenant_id, document_id=run.document_id, kind=REVIEW_EXTRACTION,
                    field_value_id=row.id, field_name=node.field_path, confidence=round(conf, 3),
                    status=RV_PENDING,
                    payload={"document_type": doc_type.name, "field_path": node.field_path,
                             "raw_value": node.raw_value,
                             "normalized_ok": bool(norm.ok) if norm else None}))
                review_items += 1

        for child in node.children:
            persist(child, row.id)

    for node in tree:
        persist(node, None)

    # --- discovery pass: salient facts the schema doesn't cover ("Entities") ---
    try:
        # Ground the model with every fact ALREADY extracted — label and real
        # value, including nested/list ones (e.g. "Tenants #2 PAN: ..."), not
        # just top-level field names. Without the actual values, the model has
        # no way to recognize a differently-worded restatement of a fact it
        # already found (e.g. "Refundable Deposit Amount" vs "Security
        # Deposit"), and duplicates it under a new name instead of skipping it.
        known = [
            (n.name, n.raw_value) for n in fieldsvc.iter_flat(tree)
            if n.node_kind == "scalar" and n.raw_value
        ][:60]
        entities, disc_usage = discover_entities(text, known, doc_type.name)
        _record_cost(db, run, STAGE_METADATA, disc_usage)
        seen_keys: set[str] = set()
        for i, ent in enumerate(entities):
            key = fieldsvc.slugify_key(ent["name"])
            # Two distinct entity names can still slugify to the same key
            # (or, on rare model repetition, discover_entities' own dedupe
            # can be bypassed) -- field_path/id are derived from `key`, so a
            # repeat here would otherwise hit the FieldValue primary key
            # constraint on commit and fail the whole stage. Skip rather
            # than crash: the first occurrence wins.
            if key in seen_keys:
                continue
            seen_keys.add(key)
            path = f"_discovered.{key}"
            loc = bbox_locator.locate_any(layout_units, [ent.get("quote"), ent["value"]], None)
            norm = normalize.normalize(ent["value"], VT_TEXT)
            db.add(FieldValue(
                id=uuid.uuid5(uuid.NAMESPACE_URL,
                              f"{run.document_id}:{run.document_version}:{doc_type.id}:{path}"),
                tenant_id=run.tenant_id, document_id=run.document_id,
                document_version=run.document_version, document_type_id=doc_type.id,
                field_key=key, field_path=path, field_name=ent["name"], node_kind="scalar",
                ordinal=i, data_type="string", raw_value=ent["value"], value_type=VT_TEXT,
                value_text=norm.value_text, confidence=round(float(ent["confidence"]), 3),
                source_page=(loc.get("page") if loc else None), source_text=ent.get("quote"),
                source_bbox=loc, review_status=RV_PENDING, model_version=disc_usage.model,
                prompt_version=prompts.PROMPT_VERSIONS["extract"], schema_version=schema.version,
                is_discovered=True))
    except Exception as e:  # noqa: BLE001  discovery is best-effort
        log.warning("entity_discovery_failed", error=str(e)[:200])

    return written, review_items


# ------------------------------------------------------ type default ACLs
def _apply_type_defaults(db, tenant_id, document_id, document_type_id) -> None:
    """Materialize type-level default grants onto the document so retrieval
    stays a single ACL join. Explicit per-document grants still override."""
    defaults = db.execute(
        select(DocumentTypeDefaultPermission).where(
            DocumentTypeDefaultPermission.document_type_id == document_type_id)
    ).scalars().all()
    for d in defaults:
        exists = db.execute(
            select(DocumentPermission.id).where(
                DocumentPermission.document_id == document_id,
                DocumentPermission.user_id == d.user_id,
                DocumentPermission.group_id == d.group_id,
                DocumentPermission.level == d.level,
            ).limit(1)
        ).first()
        if not exists:
            db.add(DocumentPermission(
                tenant_id=tenant_id, document_id=document_id, user_id=d.user_id,
                group_id=d.group_id, level=d.level))


# --------------------------------------------------------------- notifications
def _notify_reviewers(db, tenant_id, document_id, message: str) -> None:
    from app.models.constants import ROLE_ADMIN, ROLE_REVIEWER
    from app.models.tenant import User

    reviewers = db.execute(
        select(User).where(User.tenant_id == tenant_id, User.is_active.is_(True),
                           User.role.in_([ROLE_REVIEWER, ROLE_ADMIN]))
    ).scalars().all()
    for r in reviewers:
        db.add(Notification(
            tenant_id=tenant_id, user_id=r.id, kind=NOTIFY_REVIEW_PENDING,
            title="Items pending review", body=message, link="/review"))
