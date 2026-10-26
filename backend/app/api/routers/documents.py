"""Document library: upload, versioning, download (presigned), soft-delete, reprocess."""
from __future__ import annotations

import asyncio
import datetime as dt
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    client_ip, get_current_tenant, get_current_user, get_tenant_db, require_admin, require_reviewer,
)
from app.core.config import settings
from app.models.catalog import DocumentType, TypeSchema
from app.models.constants import (
    AUDIT_CONFIG_CHANGE,
    AUDIT_METADATA_CHANGE,
    AUDIT_DELETE,
    AUDIT_DOWNLOAD,
    AUDIT_RESTORE,
    AUDIT_REVIEW,
    AUDIT_TAG_CHANGE,
    AUDIT_UPLOAD,
    AUDIT_VIEW,
    DOC_FAILED,
    DOC_NEEDS_REVIEW,
    DOC_PENDING,
    DOC_PROCESSED,
    NOTIFY_DUPLICATE_UPLOAD,
    PERM_DELETE,
    PERM_READ,
    PERM_MANAGE,
    PERM_UPDATE,
    PIPELINE_STAGES,
    RV_CORRECTED,
    RV_PENDING,
    RV_REJECTED,
    RV_VERIFIED,
)
from app.models.content import Classification, FieldValue, Summary
from app.models.document import Document, DocumentVersion, document_type_links
from app.models.mgmt import Tenant as MgmtTenant
from app.models.review import ReviewItem
from app.models.tenant import User
from app.schemas import (
    BulkExtractionReview,
    BulkTagRequest,
    AddDocumentTypeRequest,
    BulkDeleteRequest,
    ExtractNewFieldsRequest,
    FieldSuggestRequest,
    PromoteFieldsRequest,
    DocumentDetail,
    DocumentListItem,
    DocumentOut,
    DocumentTagsSet,
    ExtractionEdit,
    ExtractionReview,
    PresignedUrl,
)
from app.ai import field_suggest
from app.services import (
    audit, extraction_rows, field_extraction, field_promotion, ingest_policy, mime, normalize, permissions,
)
from app.services.fields import ensure_keys, validate_schema
from app.services import tags as tag_service
from app.services.hashing import sha256_hex
from app.storage import get_storage

router = APIRouter(prefix="/documents", tags=["documents"])
RETENTION_DAYS = 30


def _storage_key(tenant_id, document_id, version, filename) -> str:
    return f"tenant/{tenant_id}/doc/{document_id}/v{version}/{filename}"


def _field_dict(e: FieldValue) -> dict:
    return {
        "id": str(e.id), "field_key": e.field_key, "field_path": e.field_path,
        "field_name": e.field_name, "node_kind": e.node_kind, "data_type": e.data_type,
        "ordinal": e.ordinal, "is_discovered": e.is_discovered, "is_user_edited": e.is_user_edited,
        "document_type_id": str(e.document_type_id) if e.document_type_id else None,
        "raw_value": e.raw_value, "value_type": e.value_type, "value_text": e.value_text,
        "value_number": e.value_number,
        "value_date": e.value_date.isoformat() if e.value_date else None,
        "value_datetime": e.value_datetime.isoformat() if e.value_datetime else None,
        "value_time": e.value_time.isoformat() if e.value_time else None,
        "value_boolean": e.value_boolean, "value_currency": e.value_currency,
        "confidence": e.confidence, "review_status": e.review_status,
        "source_page": e.source_page, "source_text": e.source_text, "source_bbox": e.source_bbox,
        "children": [],
    }


def _build_field_tree(rows: list[FieldValue]) -> list[dict]:
    """Assemble the nested tree from one flat SELECT (no recursive queries)."""
    nodes = {r.id: _field_dict(r) for r in rows}
    roots: list[dict] = []
    for r in rows:
        node = nodes[r.id]
        parent = nodes.get(r.parent_id) if r.parent_id else None
        (parent["children"] if parent else roots).append(node)
    def order(items: list[dict]) -> list[dict]:
        # Schema fields first (in schema order), discovered entities after them.
        items.sort(key=lambda n: (n["is_discovered"],
                                  n["ordinal"] if n["ordinal"] is not None else 0,
                                  n["field_path"]))
        for n in items:
            order(n["children"])
        return items
    return order(roots)


class NameConflict(Exception):
    def __init__(self, name: str, suggestion: str):
        self.name, self.suggestion = name, suggestion


async def _create_document(db, user, tenant, filename, data, description, *, name=None,
                           document_type_id=None) -> tuple[Document, dict]:
    """Create a document, honouring name uniqueness and the duplicate policy.

    Returns (document, meta). `meta["action"]` is one of:
      linked_existing  — content already present and readable; no new document
      cloned           — new document, derived artifacts copied (no AI cost)
      new              — fresh document, pipeline will run
    """
    ext, mime_type = mime.validate(filename, data, settings.max_upload_mb * 1024 * 1024)
    content_hash = sha256_hex(data)
    display_name = (name or filename).strip()

    if await ingest_policy.name_taken(db, user.tenant_id, display_name):
        raise NameConflict(display_name,
                           await ingest_policy.suggest_name(db, user.tenant_id, display_name))

    dup, readable = await ingest_policy.find_duplicate(db, user, content_hash)

    # Readable duplicate + duplicates disallowed -> just grant access, don't copy.
    if dup is not None and readable and not await ingest_policy.allow_duplicates(db, user.tenant_id):
        await _grant_read(db, user, dup)
        return dup, {"action": "linked_existing", "duplicate_of": str(dup.id),
                     "message": f"This file already exists as '{dup.name}'. You now have access to it."}

    doc = Document(
        tenant_id=user.tenant_id, owner_id=user.id, name=display_name, description=description,
        file_type=ext, mime_type=mime_type, file_size=len(data), content_hash=content_hash,
        current_version=1, processing_status=DOC_PENDING, requested_type_id=document_type_id)
    db.add(doc)
    await db.flush()

    key = _storage_key(user.tenant_id, doc.id, 1, filename)
    await asyncio.to_thread(get_storage(tenant).put_object, key, data, mime_type)
    db.add(DocumentVersion(
        tenant_id=user.tenant_id, document_id=doc.id, version=1, storage_key=key,
        file_size=len(data), content_hash=content_hash, mime_type=mime_type, uploaded_by=user.id))
    await db.flush()

    # Identical content already processed? Copy its results instead of paying to
    # redo them. Done for unreadable duplicates too — the uploader is never told
    # the other document exists, they simply get their own fully-processed copy.
    if dup is not None and dup.processing_status in (DOC_PROCESSED, DOC_NEEDS_REVIEW):
        counts = await ingest_policy.clone_derived_artifacts(db, dup, doc)
        await _clone_rendition(db, tenant, dup, doc)
        meta = {"action": "cloned", "reused": counts}
        if readable:
            meta["duplicate_of"] = str(dup.id)
            meta["message"] = f"Identical to '{dup.name}' — reused its processing results."
        return doc, meta

    return doc, {"action": "new", "duplicate_of": str(dup.id) if (dup and readable) else None}


async def _clone_rendition(db, tenant, source: Document, target: Document) -> None:
    """A clone owns its own copy of the PDF rendition, like it owns its chunks."""
    src, tgt = [(await db.execute(
        select(DocumentVersion).where(DocumentVersion.document_id == d.id,
                                      DocumentVersion.version == d.current_version)
    )).scalars().first() for d in (source, target)]
    if not (src and tgt and src.rendition_key):
        return
    storage = get_storage(tenant)
    key = f"{tgt.storage_key}.rendition.pdf"
    data = await asyncio.to_thread(storage.get_bytes, src.rendition_key)
    await asyncio.to_thread(storage.put_object, key, data, "application/pdf")
    tgt.rendition_key = key


async def _grant_read(db, user, doc: Document) -> None:
    from app.models.document import DocumentPermission

    exists = (await db.execute(
        select(DocumentPermission.id).where(
            DocumentPermission.document_id == doc.id, DocumentPermission.user_id == user.id).limit(1)
    )).first()
    if not exists and doc.owner_id != user.id:
        db.add(DocumentPermission(tenant_id=doc.tenant_id, document_id=doc.id,
                                  user_id=user.id, level=PERM_READ))
    # Tell the people responsible for the document — not every user, which would
    # disclose the document to people who cannot see it.
    from app.models.ops import Notification

    managers = {doc.owner_id}
    for (uid,) in (await db.execute(
        select(DocumentPermission.user_id).where(
            DocumentPermission.document_id == doc.id,
            DocumentPermission.level == PERM_MANAGE))).all():
        if uid:
            managers.add(uid)
    for uid in managers:
        if uid and uid != user.id:
            db.add(Notification(
                tenant_id=doc.tenant_id, user_id=uid, kind=NOTIFY_DUPLICATE_UPLOAD,
                title="Duplicate upload",
                body=f"{user.email} uploaded a file identical to '{doc.name}' and was granted access.",
                link=f"/documents/{doc.id}"))


def _trigger_pipeline(document_id, tenant_id, from_stage=None):
    from app.worker.tasks import start_processing

    start_processing.delay(str(document_id), str(tenant_id), from_stage, str(uuid.uuid4()))


@router.get("/name-available", summary="Check whether a document name is free")
async def name_available(name: str = Query(...), user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_tenant_db)):
    """Live check for the upload dialog -- names must be unique per tenant.
    Returns a suggested alternative when taken."""
    taken = await ingest_policy.name_taken(db, user.tenant_id, name)
    return {"name": name, "available": not taken,
            "suggestion": (await ingest_policy.suggest_name(db, user.tenant_id, name)) if taken else None}


@router.post("", response_model=dict, status_code=201, summary="Upload a document")
async def upload(
    request: Request,
    file: UploadFile = File(...),
    description: str = Form(""),
    name: str | None = Form(default=None),
    document_type_id: str | None = Form(default=None),  # omit / "auto" = auto-detect
    user: User = Depends(get_current_user),
    tenant: MgmtTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_tenant_db),
):
    """`multipart/form-data` upload. Triggers the async AI pipeline (text
    extraction -> classification -> summarization -> metadata extraction ->
    chunking -> embedding) automatically unless the file is a byte-identical
    duplicate of one already processed, in which case its results are reused
    (`action: "cloned"`) or access is simply granted (`action: "linked_existing"`)
    instead of paying to redo the work. Poll `GET /{document_id}/pipeline` or
    subscribe to `GET /{document_id}/events` (SSE) for progress."""
    data = await file.read()
    type_id = None
    if document_type_id and document_type_id != "auto":
        dt = await db.get(DocumentType, document_type_id)
        if not dt or dt.tenant_id != user.tenant_id:
            raise HTTPException(400, "Unknown document type")
        type_id = dt.id
    try:
        doc, meta = await _create_document(db, user, tenant, file.filename, data, description,
                                           name=name, document_type_id=type_id)
    except mime.UploadValidationError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    except NameConflict as e:
        raise HTTPException(status.HTTP_409_CONFLICT, {
            "code": "NAME_TAKEN",
            "message": f"A document named '{e.name}' already exists. Choose another name.",
            "suggestion": e.suggestion})

    await audit.log_event(db, tenant_id=user.tenant_id, actor_id=user.id, event_type=AUDIT_UPLOAD,
                          object_type="document", object_id=doc.id,
                          detail={"action": meta.get("action")}, ip_address=client_ip(request), commit=False)
    await db.commit()
    # Only fresh content needs the (paid) pipeline; clones and links reuse results.
    if meta.get("action") == "new":
        _trigger_pipeline(doc.id, tenant.id)
    # A document created in this request has never had its `tags` loaded, and the
    # response schema reads them: without this the upload succeeds, then the
    # response fails with MissingGreenlet and the client is told it failed.
    await db.refresh(doc)
    return {"document": DocumentOut.model_validate(doc).model_dump(), **meta}


@router.post("/bulk", response_model=dict, status_code=201, summary="Upload multiple documents at once")
async def bulk_upload(
    request: Request,
    files: list[UploadFile] = File(...),
    user: User = Depends(get_current_user),
    tenant: MgmtTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Same per-file rules as `POST /documents`, applied independently to each
    file -- one rejected or renamed file doesn't stop the others. A name clash
    is auto-resolved with a suggested name rather than rejected, since this
    is a batch flow with no per-file confirmation step."""
    results = []
    for f in files:
        data = await f.read()
        try:
            doc, meta = await _create_document(db, user, tenant, f.filename, data, "")
            await audit.log_event(db, tenant_id=user.tenant_id, actor_id=user.id, event_type=AUDIT_UPLOAD,
                                  object_type="document", object_id=doc.id,
                                  detail={"action": meta.get("action")}, commit=False)
            await db.commit()
            if meta.get("action") == "new":
                _trigger_pipeline(doc.id, tenant.id)
            results.append({"filename": f.filename, "status": "accepted", "document_id": str(doc.id),
                            "action": meta.get("action"), "duplicate_of": meta.get("duplicate_of"),
                            "message": meta.get("message")})
        except mime.UploadValidationError as e:
            await db.rollback()
            results.append({"filename": f.filename, "status": "rejected", "error": str(e)})
        except NameConflict as e:
            # Bulk upload shouldn't stall on a name clash — take the suggestion.
            await db.rollback()
            doc, meta = await _create_document(db, user, tenant, f.filename, data, "", name=e.suggestion)
            await db.commit()
            if meta.get("action") == "new":
                _trigger_pipeline(doc.id, tenant.id)
            results.append({"filename": f.filename, "status": "accepted", "document_id": str(doc.id),
                            "action": meta.get("action"), "renamed_to": e.suggestion})
    return {"results": results}


@router.get("", response_model=list[DocumentListItem], summary="List documents")
async def list_documents(
    q: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    tags: list[str] | None = Query(default=None, description="Tag names; repeat the parameter for several"),
    match: str = Query(default="all", pattern="^(all|any)$",
                       description="'all' = carries every listed tag, 'any' = at least one"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Only documents the caller can read (owned, individually shared, or
    shared via a group) -- optionally filtered by name substring (`q`),
    `processing_status`, and/or `tags`. Sorted by last modified, newest first,
    capped at 200."""
    group_ids = await permissions.user_group_ids(db, user.id)
    cond = permissions.readable_documents_condition(user, group_ids)
    stmt = select(Document).where(cond)
    if q:
        stmt = stmt.where(Document.name.ilike(f"%{q}%"))
    if status_filter:
        stmt = stmt.where(Document.processing_status == status_filter)
    if tags:
        wanted = [t for t in (t.strip() for t in tags) if t]
        found = await tag_service.resolve_names(db, user.tenant_id, wanted)
        # Under "all", a tag name that doesn't exist means nothing can match.
        # Counting only the resolved ids would quietly let documents through.
        if match == "all" and len(found) < len({t.lower() for t in wanted}):
            return []
        # And if none of them resolved, the answer is "nothing" under either
        # mode. An empty id list reads as "no tag filter at all" downstream,
        # which would hand back the whole corpus to someone who asked to
        # narrow it.
        if wanted and not found:
            return []
        stmt = stmt.where(tag_service.documents_with_tags_condition(
            [t.id for t in found.values()], match_all=(match == "all")))
    stmt = stmt.order_by(Document.updated_at.desc()).limit(200)
    docs = (await db.execute(stmt)).scalars().all()

    # Document types for the whole page in one query (a document can carry
    # several; the list shows all of them).
    types_by_doc: dict[uuid.UUID, list[dict]] = {}
    if docs:
        links = await db.execute(
            select(document_type_links.c.document_id, DocumentType.id, DocumentType.name)
            .join(DocumentType, DocumentType.id == document_type_links.c.document_type_id)
            .where(document_type_links.c.document_id.in_([d.id for d in docs]))
            .order_by(DocumentType.name))
        for doc_id, type_id, type_name in links.all():
            types_by_doc.setdefault(doc_id, []).append({"id": type_id, "name": type_name})

    return [
        DocumentListItem(**DocumentOut.model_validate(d).model_dump(),
                         document_types=types_by_doc.get(d.id, []))
        for d in docs
    ]


@router.put("/{document_id}/tags", summary="Replace a document's tags")
async def set_document_tags(
    document_id: str, body: DocumentTagsSet, request: Request,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_tenant_db),
):
    """Sets the document's complete tag list — anything omitted is removed.
    Names that don't exist yet are created. Needs `update` on the document."""
    doc = await db.get(Document, document_id)
    if not doc or doc.tenant_id != user.tenant_id or doc.is_deleted:
        raise HTTPException(404, "Document not found")
    if not await permissions.has_permission(db, user, doc.id, PERM_UPDATE):
        raise HTTPException(403, "You do not have permission to edit this document")
    try:
        applied = await tag_service.set_document_tags(
            db, user=user, document_id=doc.id, names=body.tags)
    except tag_service.TagError as e:
        raise HTTPException(422, str(e))
    await audit.log_event(
        db, tenant_id=user.tenant_id, actor_id=user.id, event_type=AUDIT_TAG_CHANGE,
        object_type="document", object_id=doc.id,
        detail={"action": "set", "tags": [t.name for t in applied]},
        ip_address=client_ip(request), commit=False)
    await db.commit()
    return {"tags": [{"id": str(t.id), "name": t.name} for t in applied]}


@router.post("/bulk-delete", summary="Soft-delete many documents")
async def bulk_delete(
    body: BulkDeleteRequest, request: Request,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_tenant_db),
):
    """Same rule as the single delete (recoverable for the retention window),
    applied to each listed document the caller may delete. Others are skipped
    rather than failing the call, since a selection can span a permission
    boundary. Returns `{deleted, skipped}`."""
    now = dt.datetime.now(dt.timezone.utc)
    deleted = 0
    for doc_id in dict.fromkeys(body.document_ids):  # de-duplicated, order kept
        doc = await db.get(Document, doc_id)
        if not doc or doc.is_deleted or not await permissions.has_permission(db, user, doc.id, PERM_DELETE):
            continue
        doc.is_deleted, doc.deleted_at = True, now
        await audit.log_event(db, tenant_id=user.tenant_id, actor_id=user.id, event_type=AUDIT_DELETE,
                              object_type="document", object_id=doc.id, detail={"bulk": True},
                              ip_address=client_ip(request), commit=False)
        deleted += 1
    await db.commit()
    return {"deleted": deleted, "skipped": len(set(body.document_ids)) - deleted}


@router.post("/bulk-tag", summary="Add or remove tags across many documents")
async def bulk_tag(
    body: BulkTagRequest, request: Request,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_tenant_db),
):
    """Applies `add` and `remove` to every listed document the caller may edit.

    Documents they cannot edit are skipped rather than failing the call — a
    bulk selection spanning a permission boundary is normal, and the response
    reports how many were left alone. Returns `{updated, skipped, ...}`.
    """
    try:
        add_names = tag_service.normalize_many(body.add)
        remove_names = tag_service.normalize_many(body.remove)
    except tag_service.TagError as e:
        raise HTTPException(422, str(e))
    if not add_names and not remove_names:
        raise HTTPException(422, "Provide at least one tag to add or remove")

    allowed = await tag_service.updatable_document_ids(db, user, list(body.document_ids))
    skipped = len(body.document_ids) - len(allowed)

    to_add = [await tag_service.get_or_create(
        db, tenant_id=user.tenant_id, name=n, created_by=user.id) for n in add_names]
    to_remove = await tag_service.resolve_names(db, user.tenant_id, remove_names)
    remove_ids = {t.id for t in to_remove.values()}

    added = removed = 0
    for doc_id in allowed:
        a, r = await tag_service.apply_changes(
            db, user=user, document_id=doc_id, add=to_add, remove_ids=remove_ids)
        added += a
        removed += r

    await audit.log_event(
        db, tenant_id=user.tenant_id, actor_id=user.id, event_type=AUDIT_TAG_CHANGE,
        object_type="document", object_id=None,
        detail={"action": "bulk", "documents": len(allowed), "skipped": skipped,
                "add": add_names, "remove": remove_names,
                "links_added": added, "links_removed": removed},
        ip_address=client_ip(request), commit=False)
    await db.commit()
    return {"updated": len(allowed), "skipped": skipped,
            "links_added": added, "links_removed": removed}


@router.get("/{document_id}", response_model=DocumentDetail, summary="Get a document's full detail")
async def get_document(document_id: str, request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_tenant_db)):
    """The single call the document review page is built on: summary,
    classifications, and the full extracted-data field tree (`fields`, plus a
    flat `extractions` alias of just the scalar leaves) for the document's
    current version, in one response."""
    doc = await db.get(Document, document_id)
    if not doc or not await permissions.can_read(db, user, doc.id):
        raise HTTPException(404, "Document not found")

    summary = (await db.execute(
        select(Summary).where(Summary.document_id == doc.id, Summary.document_version == doc.current_version)
    )).scalars().first()
    extractions = (await db.execute(
        select(FieldValue).where(FieldValue.document_id == doc.id, FieldValue.document_version == doc.current_version)
    )).scalars().all()
    classifications = (await db.execute(
        select(Classification).where(Classification.document_id == doc.id)
    )).scalars().all()
    await db.refresh(doc, attribute_names=["document_types"])
    await audit.log_event(db, tenant_id=user.tenant_id, actor_id=user.id, event_type=AUDIT_VIEW,
                          object_type="document", object_id=doc.id, ip_address=client_ip(request))

    detail = DocumentDetail.model_validate(doc).model_dump()
    current = (await db.execute(
        select(DocumentVersion.rendition_key).where(DocumentVersion.document_id == doc.id,
                                                    DocumentVersion.version == doc.current_version)
    )).scalar()
    detail["has_pdf"] = doc.file_type == "pdf" or bool(current)  # something the PDF viewer can show
    detail["document_types"] = []
    value_counts = dict((await db.execute(
        select(FieldValue.document_type_id, func.count()).where(
            FieldValue.document_id == doc.id, FieldValue.document_version == doc.current_version,
            FieldValue.node_kind == "scalar").group_by(FieldValue.document_type_id))).all())
    for c in doc.document_types:
        # Fields the type gained after this document was read; the page offers to read just those.
        new_fields, _ = await field_extraction.new_fields_for_document(db, doc, c)
        detail["document_types"].append({
            "id": str(c.id), "name": c.name, "description": c.description, "is_enabled": c.is_enabled,
            "active_schema_id": str(c.active_schema_id) if c.active_schema_id else None,
            "value_count": value_counts.get(c.id, 0),  # what removing this type would delete
            "new_fields": [{"key": f["key"], "name": f["name"], "data_type": f["data_type"]} for f in new_fields]})
    detail["summary"] = ({"executive_summary": summary.executive_summary, "highlights": summary.highlights,
                          "model_version": summary.model_version} if summary else None)
    detail["fields"] = _build_field_tree(extractions)
    # Back-compat alias for older clients that read a flat list.
    detail["extractions"] = [_field_dict(e) for e in extractions if e.node_kind == "scalar"]
    detail["classifications"] = [{"label": c.label, "confidence": c.confidence, "source": c.source,
                                  "is_override": c.is_override} for c in classifications]
    return detail


@router.get("/{document_id}/download", response_model=PresignedUrl, summary="Get a presigned download URL")
async def download(document_id: str, request: Request, version: int | None = Query(default=None),
                   disposition: str = Query(default="attachment"),
                   user: User = Depends(get_current_user), tenant: MgmtTenant = Depends(get_current_tenant),
                   db: AsyncSession = Depends(get_tenant_db)):
    """Returns a short-lived (15 min) presigned URL to the file in the
    tenant's storage container -- the API itself never proxies the bytes.
    `disposition=inline` omits Content-Disposition so a browser can render it
    (e.g. the PDF viewer) instead of downloading it."""
    doc = await db.get(Document, document_id)
    if not doc or not await permissions.can_read(db, user, doc.id):
        raise HTTPException(404, "Document not found")
    v = version or doc.current_version
    dv = (await db.execute(
        select(DocumentVersion).where(DocumentVersion.document_id == doc.id, DocumentVersion.version == v)
    )).scalars().first()
    if not dv:
        raise HTTPException(404, "Version not found")
    # inline => no Content-Disposition header, so the viewer can render it in-browser.
    # The viewer shows the PDF rendition of a converted file; a download is the original.
    filename = None if disposition == "inline" else doc.name
    key = dv.rendition_key if (disposition == "inline" and dv.rendition_key) else dv.storage_key
    url = await asyncio.to_thread(get_storage(tenant).presigned_get_url, key, 900, filename)
    await audit.log_event(db, tenant_id=user.tenant_id, actor_id=user.id, event_type=AUDIT_DOWNLOAD,
                          object_type="document", object_id=doc.id,
                          detail={"version": v, "disposition": disposition}, ip_address=client_ip(request))
    return PresignedUrl(url=url, expires_in=900)


_PDF_CACHE: dict[str, bytes] = {}  # tiny in-process cache: storage_key -> pdf bytes
_PDF_CACHE_MAX = 4


def _render_page_png(data: bytes, page_no: int, scale: float) -> bytes:
    """Rasterize one PDF page with pdfium (handles heavy vector PDFs that
    choke pdf.js in the browser)."""
    import io

    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    try:
        if page_no < 1 or page_no > len(pdf):
            raise IndexError(f"page {page_no} out of range 1..{len(pdf)}")
        page = pdf[page_no - 1]
        bitmap = page.render(scale=scale)
        pil = bitmap.to_pil()
        buf = io.BytesIO()
        pil.save(buf, format="PNG")
        return buf.getvalue()
    finally:
        pdf.close()


@router.get("/{document_id}/pages/{page_no}/image", summary="Render one PDF page as a PNG")
async def page_image(document_id: str, page_no: int, version: int | None = Query(default=None),
                     scale: float = Query(default=2.0, ge=0.5, le=4.0),
                     user: User = Depends(get_current_user), tenant: MgmtTenant = Depends(get_current_tenant),
                     db: AsyncSession = Depends(get_tenant_db)):
    """Server-rendered page raster — viewer fallback for PDFs pdf.js can't paint."""
    from fastapi import Response

    doc = await db.get(Document, document_id)
    if not doc or not await permissions.can_read(db, user, doc.id):
        raise HTTPException(404, "Document not found")
    v = version or doc.current_version
    dv = (await db.execute(
        select(DocumentVersion).where(DocumentVersion.document_id == doc.id, DocumentVersion.version == v)
    )).scalars().first()
    if not dv:
        raise HTTPException(404, "Version not found")

    pdf_key = dv.rendition_key or dv.storage_key
    data = _PDF_CACHE.get(pdf_key)
    if data is None:
        data = await asyncio.to_thread(get_storage(tenant).get_bytes, pdf_key)
        if len(_PDF_CACHE) >= _PDF_CACHE_MAX:
            _PDF_CACHE.pop(next(iter(_PDF_CACHE)))
        _PDF_CACHE[pdf_key] = data
    try:
        png = await asyncio.to_thread(_render_page_png, data, page_no, scale)
    except IndexError as e:
        raise HTTPException(404, str(e))
    return Response(png, media_type="image/png",
                    headers={"Cache-Control": "private, max-age=3600"})


@router.post("/{document_id}/extractions/{field_value_id}/review", summary="Accept or reject one extracted field")
async def review_extraction(
    document_id: str,
    field_value_id: str,
    body: ExtractionReview,
    user: User = Depends(require_reviewer),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Accept/reject a single extracted value from the document viewer.

    Keeps the review queue in sync: the linked ReviewItem is resolved too, and
    once nothing is pending the document leaves the `needs_review` state.
    """
    if body.action not in ("accept", "reject"):
        raise HTTPException(400, "action must be accept or reject")

    doc = await db.get(Document, document_id)
    if not doc or not await permissions.can_read(db, user, doc.id):
        raise HTTPException(404, "Document not found")
    ext = await db.get(FieldValue, field_value_id)
    if not ext or str(ext.document_id) != str(doc.id):
        raise HTTPException(404, "FieldValue not found")

    ext.review_status = RV_VERIFIED if body.action == "accept" else RV_REJECTED

    # Resolve the queue item that was raised for this field, if any.
    item = (await db.execute(
        select(ReviewItem).where(ReviewItem.field_value_id == ext.id, ReviewItem.status == RV_PENDING)
    )).scalars().first()
    now = dt.datetime.now(dt.timezone.utc)
    if item:
        item.status = ext.review_status
        item.resolved_by = user.id
        item.resolved_at = now
        item.notes = body.notes
        # Learning-loop feedstock: (input, prediction, correction).
        item.payload = {**(item.payload or {}), "action": body.action, "correction": None}

    remaining = await _flip_doc_status_on_pending_change(db, doc)

    await audit.log_event(
        db, tenant_id=user.tenant_id, actor_id=user.id, event_type=AUDIT_REVIEW,
        object_type="extraction", object_id=ext.id,
        detail={"action": body.action, "field": ext.field_name, "inline": True}, commit=False)
    await db.commit()
    return {
        "id": str(ext.id), "field_name": ext.field_name, "review_status": ext.review_status,
        "document_status": doc.processing_status, "pending_remaining": remaining,
    }


async def _flip_doc_status_on_pending_change(db: AsyncSession, doc: Document) -> int:
    """Recompute the document's remaining-pending count and flip
    processing_status between needs_review/processed to match. Shared by
    every endpoint that resolves one or more ReviewItems -- flush first so
    the count sees this call's own status changes, not stale ones."""
    await db.flush()
    remaining = (await db.execute(
        select(func.count()).select_from(ReviewItem).where(
            ReviewItem.document_id == doc.id, ReviewItem.status == RV_PENDING)
    )).scalar() or 0
    if remaining == 0 and doc.processing_status in (DOC_NEEDS_REVIEW, DOC_PROCESSED):
        doc.processing_status = DOC_PROCESSED
    elif remaining and doc.processing_status not in (DOC_FAILED,):
        doc.processing_status = DOC_NEEDS_REVIEW
    return remaining


@router.post("/{document_id}/extractions/bulk-review", summary="Accept or reject every pending field at once")
async def bulk_review_extractions(
    document_id: str,
    body: BulkExtractionReview,
    user: User = Depends(require_reviewer),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Accept or reject every field still pending_review on this document's
    current version in one action -- the "Accept/Reject All Remaining"
    buttons. Same rules and side effects as `review_extraction`, applied in
    bulk rather than once per field."""
    if body.action not in ("accept", "reject"):
        raise HTTPException(400, "action must be accept or reject")

    doc = await db.get(Document, document_id)
    if not doc or not await permissions.can_read(db, user, doc.id):
        raise HTTPException(404, "Document not found")

    new_status = RV_VERIFIED if body.action == "accept" else RV_REJECTED
    stmt = select(FieldValue).where(
        FieldValue.document_id == doc.id, FieldValue.document_version == doc.current_version,
        FieldValue.node_kind == "scalar", FieldValue.review_status == RV_PENDING)
    if body.skip_new:
        stmt = stmt.where(FieldValue.is_discovered.is_(False))
    pending_fields = (await db.execute(stmt)).scalars().all()

    if not pending_fields:
        return {"count": 0, "review_status": new_status, "document_status": doc.processing_status,
                "pending_remaining": 0}

    field_ids = [f.id for f in pending_fields]
    for f in pending_fields:
        f.review_status = new_status

    now = dt.datetime.now(dt.timezone.utc)
    await db.execute(
        update(ReviewItem).where(ReviewItem.field_value_id.in_(field_ids), ReviewItem.status == RV_PENDING)
        .values(status=new_status, resolved_by=user.id, resolved_at=now))

    remaining = await _flip_doc_status_on_pending_change(db, doc)

    await audit.log_event(
        db, tenant_id=user.tenant_id, actor_id=user.id, event_type=AUDIT_REVIEW,
        object_type="document", object_id=doc.id,
        detail={"action": body.action, "count": len(field_ids), "bulk": True}, commit=False)
    await db.commit()
    return {
        "count": len(field_ids), "review_status": new_status,
        "document_status": doc.processing_status, "pending_remaining": remaining,
    }


@router.post("/{document_id}/fields/suggest", summary="Suggest details for new fields before adding them to a type")
async def suggest_new_field_details(
    document_id: str,
    body: FieldSuggestRequest,
    user: User = Depends(require_reviewer),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Pre-fills the "add to document type" form for fields the AI found that
    no type defines: a reusable name, a one-sentence description, a data type,
    the best-fitting document type of *this* document, and -- when the fact is
    really an existing field worded differently -- which one. Read-only; the
    administrator reviews and edits everything before anything is saved."""
    doc = await db.get(Document, document_id)
    if not doc or not await permissions.can_read(db, user, doc.id):
        raise HTTPException(404, "Document not found")

    rows = (await db.execute(select(FieldValue).where(
        FieldValue.id.in_(body.field_value_ids), FieldValue.document_id == doc.id,
        FieldValue.document_version == doc.current_version, FieldValue.is_discovered.is_(True),
        FieldValue.node_kind == "scalar"))).scalars().all()
    if not rows:
        raise HTTPException(404, "No new fields found for those ids")

    types = (await db.execute(
        select(DocumentType).join(document_type_links, document_type_links.c.document_type_id == DocumentType.id)
        .where(document_type_links.c.document_id == doc.id).order_by(DocumentType.name))).scalars().all()
    infos = []
    for t in types:
        schema = await db.get(TypeSchema, t.active_schema_id) if t.active_schema_id else None
        infos.append(field_suggest.TypeInfo(
            id=str(t.id), name=t.name, description=t.description or "",
            fields=[{"key": f["key"], "name": f["name"], "description": f.get("description", ""),
                     "data_type": f["data_type"]} for f in (schema.fields if schema else [])]))

    by_id = {str(r.id): r for r in rows}
    facts = [field_suggest.Fact(
        id=str(r.id), name=r.field_name, value=r.raw_value or "", quote=r.source_text or "",
        current_type_id=str(r.document_type_id) if r.document_type_id else "") for r in rows]
    suggestions, used_ai = await asyncio.to_thread(field_suggest.suggest, facts, infos)
    return {
        "suggestions": suggestions, "ai": used_ai,
        "document_types": [{"id": i.id, "name": i.name,
                            "fields": [{"key": f["key"], "name": f["name"], "data_type": f["data_type"]}
                                       for f in i.fields]} for i in infos],
        "fields": [{"id": k, "name": r.field_name, "value": r.raw_value, "quote": r.source_text,
                    "review_status": r.review_status} for k, r in by_id.items()],
    }


@router.post("/{document_id}/fields/extract-new", summary="Read the fields a type gained since this document was extracted")
async def extract_new_fields(
    document_id: str,
    body: ExtractNewFieldsRequest,
    request: Request,
    user: User = Depends(require_reviewer),
    db: AsyncSession = Depends(get_tenant_db),
):
    """When a field is added to a document type, documents already read under it
    don't have that field. Reprocessing fixes that but redoes everything and
    deletes the document's reviewed and edited values. This reads only the
    missing fields from the document's stored text and adds them, leaving every
    other value alone. The new values go through the normal auto-accept rule, so
    uncertain ones appear for review. All or nothing."""
    doc = await db.get(Document, document_id)
    if not doc or not await permissions.can_read(db, user, doc.id):
        raise HTTPException(404, "Document not found")
    dt = await db.get(DocumentType, body.document_type_id)
    linked = dt is not None and (await db.execute(
        select(document_type_links.c.document_id).where(
            document_type_links.c.document_id == doc.id,
            document_type_links.c.document_type_id == dt.id))).first() is not None
    if not linked or dt.tenant_id != user.tenant_id:
        raise HTTPException(404, "That document type isn't one of this document's types")

    new_fields, schema = await field_extraction.new_fields_for_document(db, doc, dt)
    wanted = [f for f in new_fields if not body.keys or f["key"] in body.keys]
    if not wanted:
        raise HTTPException(409, "Nothing new to extract: this document already has every field of this type.")

    defs = validate_schema(ensure_keys(wanted))
    order = {f["key"]: i for i, f in enumerate(schema.fields)}
    try:
        built = await field_extraction.extract_fields(db, doc, dt, defs, schema.version)
    except field_extraction.FieldExtractionError as e:
        await db.rollback()
        raise HTTPException(e.status, e.message)

    # A list the model found nothing for still gets an (empty) row, so it isn't offered again.
    filler = extraction_rows.build_rows(
        field_extraction.empty_containers(defs, built), tenant_id=doc.tenant_id, document_id=doc.id,
        document_version=doc.current_version, doc_type_id=dt.id, doc_type_name=dt.name,
        schema_version=schema.version, model="", prompt_version="", layout_units=[], autoaccept=1.0)
    for row in [*built.fields, *filler.fields]:
        if row.parent_id is None:
            row.ordinal = order.get(row.field_key)
    db.add_all([*built.fields, *filler.fields])
    await db.flush()  # review items point at these rows
    db.add_all(built.reviews)

    remaining = await _flip_doc_status_on_pending_change(db, doc)
    extracted = []
    for d in defs:
        mine = [f for f in built.fields if f.field_path == d.key or f.field_path.startswith((f"{d.key}[", f"{d.key}."))]
        top = next((f for f in mine if f.field_path == d.key), None)
        extracted.append({
            "key": d.key, "name": d.name, "data_type": d.data_type,
            # "Found" means a value was actually read, not just that a row exists for the question.
            "found": any(f.raw_value for f in mine if f.node_kind == "scalar"),
            "entries": sum(1 for f in mine if top and f.parent_id == top.id) if d.data_type in ("list", "object") else None,
        })
    await audit.log_event(
        db, tenant_id=user.tenant_id, actor_id=user.id, event_type=AUDIT_METADATA_CHANGE,
        object_type="document", object_id=doc.id,
        detail={"action": "extract_new_fields", "document_type": dt.name, "schema_version": schema.version,
                "fields": [e["name"] for e in extracted]},
        ip_address=client_ip(request), commit=False)
    await db.commit()
    return {"extracted": extracted, "document_status": doc.processing_status, "pending_remaining": remaining}


async def _linked(db: AsyncSession, doc: Document, type_id) -> bool:
    return (await db.execute(select(document_type_links.c.document_id).where(
        document_type_links.c.document_id == doc.id,
        document_type_links.c.document_type_id == type_id))).first() is not None


@router.post("/{document_id}/types", summary="Add a document type to this document and read its fields")
async def add_document_type(
    document_id: str,
    body: AddDocumentTypeRequest,
    request: Request,
    user: User = Depends(require_reviewer),
    db: AsyncSession = Depends(get_tenant_db),
):
    """For a document the AI missed a type on. Links the type, then reads every
    field of its current schema from the document's stored text. Other types'
    values are untouched. The new values follow the normal auto-accept rule.
    All or nothing: if the read fails nothing is added."""
    doc = await db.get(Document, document_id)
    if not doc or not await permissions.can_read(db, user, doc.id):
        raise HTTPException(404, "Document not found")
    dt = await db.get(DocumentType, body.document_type_id)
    if dt is None or dt.tenant_id != user.tenant_id or not dt.is_enabled:
        raise HTTPException(404, "Document type not found")
    if await _linked(db, doc, dt.id):
        raise HTTPException(409, f"This document is already a {dt.name}.")

    schema = await db.get(TypeSchema, dt.active_schema_id) if dt.active_schema_id else None
    built = filler = None
    if schema is not None and schema.fields:
        defs = validate_schema(ensure_keys(schema.fields))
        try:
            built = await field_extraction.extract_fields(db, doc, dt, defs, schema.version)
        except field_extraction.FieldExtractionError as e:
            await db.rollback()
            raise HTTPException(e.status, e.message)
        filler = extraction_rows.build_rows(
            field_extraction.empty_containers(defs, built), tenant_id=doc.tenant_id, document_id=doc.id,
            document_version=doc.current_version, doc_type_id=dt.id, doc_type_name=dt.name,
            schema_version=schema.version, model="", prompt_version="", layout_units=[], autoaccept=1.0)
        order = {f["key"]: i for i, f in enumerate(schema.fields)}
        for row in [*built.fields, *filler.fields]:
            if row.parent_id is None:
                row.ordinal = order.get(row.field_key)
        db.add_all([*built.fields, *filler.fields])
        await db.flush()  # review items point at these rows
        db.add_all(built.reviews)

    await db.execute(document_type_links.insert().values(document_id=doc.id, document_type_id=dt.id))
    db.add(Classification(tenant_id=user.tenant_id, document_id=doc.id, document_type_id=dt.id,
                          label=dt.name, confidence=1.0, source="human", is_override=True))
    doc.types_locked = True
    remaining = await _flip_doc_status_on_pending_change(db, doc)
    count = len(built.fields) if built else 0
    await audit.log_event(
        db, tenant_id=user.tenant_id, actor_id=user.id, event_type=AUDIT_METADATA_CHANGE,
        object_type="document", object_id=doc.id,
        detail={"action": "add_document_type", "document_type": dt.name, "values": count},
        ip_address=client_ip(request), commit=False)
    await db.commit()
    return {"document_type": dt.name, "values": count, "pending_remaining": remaining,
            "document_status": doc.processing_status}


@router.delete("/{document_id}/types/{type_id}", summary="Remove a document type and its extracted data")
async def remove_document_type(
    document_id: str,
    type_id: str,
    request: Request,
    user: User = Depends(require_reviewer),
    db: AsyncSession = Depends(get_tenant_db),
):
    """For a document the AI gave the wrong type. Unlinks the type and deletes the
    values extracted under it -- including its suggested ("new") fields and any
    review items on them. Other types' values are untouched. Reprocessing keeps
    the result, because a person has now chosen this document's types."""
    doc = await db.get(Document, document_id)
    if not doc or not await permissions.can_read(db, user, doc.id):
        raise HTTPException(404, "Document not found")
    dt = await db.get(DocumentType, type_id)
    if dt is None or dt.tenant_id != user.tenant_id or not await _linked(db, doc, dt.id):
        raise HTTPException(404, "That document type isn't one of this document's types")

    scope = (FieldValue.document_id == doc.id, FieldValue.document_type_id == dt.id)
    removed = (await db.execute(select(func.count()).select_from(FieldValue).where(*scope))).scalar() or 0
    # Review items go with their values (the FK cascades); say so explicitly so the
    # queue is right even if a database was built without the cascade.
    await db.execute(delete(ReviewItem).where(ReviewItem.field_value_id.in_(select(FieldValue.id).where(*scope))))
    await db.execute(delete(FieldValue).where(*scope))
    await db.execute(document_type_links.delete().where(
        document_type_links.c.document_id == doc.id, document_type_links.c.document_type_id == dt.id))
    await db.execute(delete(Classification).where(
        Classification.document_id == doc.id, Classification.document_type_id == dt.id))
    doc.types_locked = True
    remaining = await _flip_doc_status_on_pending_change(db, doc)
    await audit.log_event(
        db, tenant_id=user.tenant_id, actor_id=user.id, event_type=AUDIT_METADATA_CHANGE,
        object_type="document", object_id=doc.id,
        detail={"action": "remove_document_type", "document_type": dt.name, "values_deleted": removed},
        ip_address=client_ip(request), commit=False)
    await db.commit()
    return {"document_type": dt.name, "values_deleted": removed, "pending_remaining": remaining,
            "document_status": doc.processing_status}


@router.post("/{document_id}/fields/promote", summary="Add new fields to a document type")
async def promote_new_fields(
    document_id: str,
    body: PromoteFieldsRequest,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Turns discovered ("new") fields into real fields of one of the
    document's types: appends them to that type's schema as a new version (or
    maps them onto an existing field), converts this document's values, and
    accepts them -- adding a field to a type is the acceptance. Administrators
    only, because it edits a document type. All or nothing."""
    doc = await db.get(Document, document_id)
    if not doc or not await permissions.can_read(db, admin, doc.id):
        raise HTTPException(404, "Document not found")

    items = [field_promotion.PromoteItem(
        field_value_id=str(i.field_value_id), name=i.name, description=i.description,
        data_type=i.data_type, document_type_id=str(i.document_type_id), existing_key=i.existing_key,
        shape=i.shape, columns=[c.model_dump() for c in i.columns] if i.columns else None)
        for i in body.items]
    try:
        results = await field_promotion.promote_fields(db, admin, doc, items)
    except field_promotion.PromoteError as e:
        await db.rollback()
        raise HTTPException(e.status, e.message)

    remaining = await _flip_doc_status_on_pending_change(db, doc)
    await audit.log_event(
        db, tenant_id=admin.tenant_id, actor_id=admin.id, event_type=AUDIT_CONFIG_CHANGE,
        object_type="document", object_id=doc.id,
        detail={"action": "promote_fields",
                "fields": [{"name": r["name"], "type": r["document_type_name"], "key": r["key"],
                            "schema_version": r["schema_version"], "created": r["created_field"]}
                           for r in results]},
        ip_address=client_ip(request), commit=False)
    await db.commit()
    return {"promoted": results, "document_status": doc.processing_status, "pending_remaining": remaining}


@router.post("/{document_id}/extractions/{field_value_id}/edit", summary="Directly correct a field's value")
async def edit_extraction(
    document_id: str,
    field_value_id: str,
    body: ExtractionEdit,
    user: User = Depends(require_reviewer),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Directly replace a field's value, regardless of its current review
    status. Marks it corrected + user-edited -- is_user_edited stays true even
    if review_status later changes again, as a durable record a human typed
    this value rather than just accepting/rejecting the AI's raw_value."""
    doc = await db.get(Document, document_id)
    if not doc or not await permissions.can_read(db, user, doc.id):
        raise HTTPException(404, "Document not found")
    ext = await db.get(FieldValue, field_value_id)
    if not ext or str(ext.document_id) != str(doc.id):
        raise HTTPException(404, "FieldValue not found")
    if ext.node_kind != "scalar":
        raise HTTPException(400, "Only scalar fields have a value to edit")

    ext.raw_value = body.value
    norm = normalize.normalize(body.value, ext.value_type)
    ext.value_text, ext.value_number = norm.value_text, norm.value_number
    ext.value_date, ext.value_currency = norm.value_date, norm.value_currency
    ext.review_status = RV_CORRECTED
    ext.is_user_edited = True

    item = (await db.execute(
        select(ReviewItem).where(ReviewItem.field_value_id == ext.id, ReviewItem.status == RV_PENDING)
    )).scalars().first()
    if item:
        item.status = RV_CORRECTED
        item.resolved_by = user.id
        item.resolved_at = dt.datetime.now(dt.timezone.utc)
        item.payload = {**(item.payload or {}), "action": "edit", "correction": body.value}

    remaining = await _flip_doc_status_on_pending_change(db, doc)

    await audit.log_event(
        db, tenant_id=user.tenant_id, actor_id=user.id, event_type=AUDIT_REVIEW,
        object_type="extraction", object_id=ext.id,
        detail={"action": "edit", "field": ext.field_name}, commit=False)
    await db.commit()
    return {
        "id": str(ext.id), "field_name": ext.field_name, "review_status": ext.review_status,
        "raw_value": ext.raw_value, "is_user_edited": ext.is_user_edited,
        "document_status": doc.processing_status, "pending_remaining": remaining,
    }


@router.delete("/{document_id}/extractions/{field_value_id}", summary="Delete a row from a list/table field")
async def delete_extraction_item(
    document_id: str,
    field_value_id: str,
    user: User = Depends(require_reviewer),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Remove one row from a list/table field (e.g. a duplicated or spurious
    entry the AI extracted). Only items that live under a `list` container can
    be deleted this way -- schema-defined fields can be corrected via `edit`
    but not removed. Postgres ON DELETE CASCADE on field_values.parent_id and
    review_items.field_value_id takes care of the item's descendants and any
    pending review items for them in the same statement.
    """
    doc = await db.get(Document, document_id)
    if not doc or not await permissions.can_read(db, user, doc.id):
        raise HTTPException(404, "Document not found")
    ext = await db.get(FieldValue, field_value_id)
    if not ext or str(ext.document_id) != str(doc.id):
        raise HTTPException(404, "FieldValue not found")

    parent = await db.get(FieldValue, ext.parent_id) if ext.parent_id else None
    if not parent or parent.node_kind != "list":
        raise HTTPException(400, "Only items inside a list/table field can be deleted")

    field_name, field_path, parent_id = ext.field_name, ext.field_path, parent.id
    await db.delete(ext)
    remaining = await _flip_doc_status_on_pending_change(db, doc)

    await audit.log_event(
        db, tenant_id=user.tenant_id, actor_id=user.id, event_type=AUDIT_DELETE,
        object_type="extraction", object_id=field_value_id,
        detail={"field": field_name, "field_path": field_path, "parent_id": str(parent_id)}, commit=False)
    await db.commit()
    return {"id": field_value_id, "document_status": doc.processing_status, "pending_remaining": remaining}


@router.get("/{document_id}/layout", summary="Get per-page layout for viewer overlays")
async def layout(document_id: str, version: int | None = Query(default=None),
                 include_blocks: bool = Query(default=False),
                 user: User = Depends(get_current_user), db: AsyncSession = Depends(get_tenant_db)):
    """Per-page dimensions (+ optional layout blocks) for viewer overlays."""
    doc = await db.get(Document, document_id)
    if not doc or not await permissions.can_read(db, user, doc.id):
        raise HTTPException(404, "Document not found")
    v = version or doc.current_version
    dv = (await db.execute(
        select(DocumentVersion).where(DocumentVersion.document_id == doc.id, DocumentVersion.version == v)
    )).scalars().first()
    if not dv:
        raise HTTPException(404, "Version not found")
    u = dv.understanding or {}
    pages = []
    for unit in u.get("units", []):
        entry = {"page": unit.get("page"), "width": unit.get("width"), "height": unit.get("height"),
                 "block_count": len(unit.get("blocks") or [])}
        if include_blocks:
            entry["blocks"] = unit.get("blocks") or []
        pages.append(entry)
    return {"document_id": str(doc.id), "version": v, "anchor_type": u.get("anchor_type"),
            "parser": u.get("parser"), "ocr_used": u.get("ocr_used"), "pages": pages}


@router.get("/{document_id}/versions", summary="List a document's version history")
async def list_versions(document_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_tenant_db)):
    doc = await db.get(Document, document_id)
    if not doc or not await permissions.can_read(db, user, doc.id):
        raise HTTPException(404, "Document not found")
    rows = (await db.execute(
        select(DocumentVersion).where(DocumentVersion.document_id == doc.id).order_by(DocumentVersion.version.desc())
    )).scalars().all()
    return [{"version": v.version, "file_size": v.file_size, "content_hash": v.content_hash,
             "uploaded_by": str(v.uploaded_by) if v.uploaded_by else None, "created_at": v.created_at.isoformat()}
            for v in rows]


@router.post("/{document_id}/versions", response_model=DocumentOut, status_code=201, summary="Upload a new version of a document")
async def upload_new_version(document_id: str, file: UploadFile = File(...),
                             user: User = Depends(get_current_user),
                             tenant: MgmtTenant = Depends(get_current_tenant),
                             db: AsyncSession = Depends(get_tenant_db)):
    """Replaces the document's `current_version` with a new file and re-runs
    the full pipeline from scratch -- requires Update permission (owner,
    admin, or an explicit grant), not just Read."""
    doc = await db.get(Document, document_id)
    if not doc or not await permissions.has_permission(db, user, doc.id, PERM_UPDATE):
        raise HTTPException(404, "Document not found or no update permission")
    data = await file.read()
    try:
        ext, mime_type = mime.validate(file.filename, data, settings.max_upload_mb * 1024 * 1024)
    except mime.UploadValidationError as e:
        raise HTTPException(400, str(e))
    new_v = doc.current_version + 1
    key = _storage_key(user.tenant_id, doc.id, new_v, file.filename)
    await asyncio.to_thread(get_storage(tenant).put_object, key, data, mime_type)
    db.add(DocumentVersion(tenant_id=user.tenant_id, document_id=doc.id, version=new_v, storage_key=key,
                           file_size=len(data), content_hash=sha256_hex(data), mime_type=mime_type, uploaded_by=user.id))
    doc.current_version = new_v
    doc.file_type, doc.mime_type = ext, mime_type  # a new version may be a different format
    doc.file_size = len(data)
    doc.processing_status = DOC_PENDING
    await db.commit()
    _trigger_pipeline(doc.id, tenant.id)
    return DocumentOut.model_validate(doc)


@router.post("/{document_id}/reprocess", response_model=dict, summary="Re-run the AI pipeline")
async def reprocess(document_id: str, from_stage: str | None = Query(default=None),
                    user: User = Depends(get_current_user), tenant: MgmtTenant = Depends(get_current_tenant),
                    db: AsyncSession = Depends(get_tenant_db)):
    """Re-queues the pipeline for the document's current version. Omit
    `from_stage` to run it end-to-end, or pass one of the pipeline stage
    names (see `GET /{document_id}/pipeline`) to resume partway through --
    e.g. after fixing a document type's schema and wanting fresh extraction
    without re-paying for text extraction/classification/summarization."""
    doc = await db.get(Document, document_id)
    if not doc or not await permissions.has_permission(db, user, doc.id, PERM_UPDATE):
        raise HTTPException(404, "Document not found or no update permission")
    if from_stage and from_stage not in PIPELINE_STAGES:
        raise HTTPException(400, f"from_stage must be one of {PIPELINE_STAGES}")
    _trigger_pipeline(doc.id, tenant.id, from_stage)
    return {"status": "queued", "document_id": str(doc.id), "from_stage": from_stage}


@router.delete("/{document_id}", status_code=204, summary="Soft-delete a document")
async def soft_delete(document_id: str, request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_tenant_db)):
    """Marks the document `is_deleted` rather than removing it -- recoverable
    via `POST /{document_id}/restore` within the retention window
    (currently 30 days), after which it's treated as permanently gone."""
    doc = await db.get(Document, document_id)
    if not doc or not await permissions.has_permission(db, user, doc.id, PERM_DELETE):
        raise HTTPException(404, "Document not found or no delete permission")
    doc.is_deleted = True
    doc.deleted_at = dt.datetime.now(dt.timezone.utc)
    await audit.log_event(db, tenant_id=user.tenant_id, actor_id=user.id, event_type=AUDIT_DELETE,
                          object_type="document", object_id=doc.id, ip_address=client_ip(request), commit=False)
    await db.commit()


@router.post("/{document_id}/restore", response_model=DocumentOut, summary="Restore a soft-deleted document")
async def restore(document_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_tenant_db)):
    """Requires being an administrator, the document's owner, or holding
    Manage permission on it. 410 once the retention window has elapsed."""
    doc = await db.get(Document, document_id)
    if not doc or doc.tenant_id != user.tenant_id:
        raise HTTPException(404, "Document not found")
    if not (user.role == "administrator" or doc.owner_id == user.id
            or await permissions.has_permission(db, user, doc.id, PERM_MANAGE)):
        raise HTTPException(403, "No permission to restore")
    if not doc.is_deleted:
        return DocumentOut.model_validate(doc)
    if doc.deleted_at and (dt.datetime.now(dt.timezone.utc) - doc.deleted_at).days > RETENTION_DAYS:
        raise HTTPException(410, "Retention window elapsed; document purged")
    doc.is_deleted = False
    doc.deleted_at = None
    await audit.log_event(db, tenant_id=user.tenant_id, actor_id=user.id, event_type=AUDIT_RESTORE,
                          object_type="document", object_id=doc.id, commit=False)
    await db.commit()
    return DocumentOut.model_validate(doc)
