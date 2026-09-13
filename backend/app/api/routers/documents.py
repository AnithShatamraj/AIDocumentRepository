"""Document library: upload, versioning, download (presigned), soft-delete, reprocess."""
from __future__ import annotations

import asyncio
import datetime as dt
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import client_ip, get_current_tenant, get_current_user, get_tenant_db, require_reviewer
from app.core.config import settings
from app.models.catalog import DocumentType
from app.models.constants import (
    AUDIT_DELETE,
    AUDIT_DOWNLOAD,
    AUDIT_RESTORE,
    AUDIT_REVIEW,
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
from app.models.document import Document, DocumentVersion
from app.models.mgmt import Tenant as MgmtTenant
from app.models.review import ReviewItem
from app.models.tenant import User
from app.schemas import (
    BulkExtractionReview,
    DocumentDetail,
    DocumentOut,
    ExtractionEdit,
    ExtractionReview,
    PresignedUrl,
)
from app.services import audit, ingest_policy, mime, normalize, permissions
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
        meta = {"action": "cloned", "reused": counts}
        if readable:
            meta["duplicate_of"] = str(dup.id)
            meta["message"] = f"Identical to '{dup.name}' — reused its processing results."
        return doc, meta

    return doc, {"action": "new", "duplicate_of": str(dup.id) if (dup and readable) else None}


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


@router.get("/name-available")
async def name_available(name: str = Query(...), user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_tenant_db)):
    """Live check for the upload dialog."""
    taken = await ingest_policy.name_taken(db, user.tenant_id, name)
    return {"name": name, "available": not taken,
            "suggestion": (await ingest_policy.suggest_name(db, user.tenant_id, name)) if taken else None}


@router.post("", response_model=dict, status_code=201)
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
    return {"document": DocumentOut.model_validate(doc).model_dump(), **meta}


@router.post("/bulk", response_model=dict, status_code=201)
async def bulk_upload(
    request: Request,
    files: list[UploadFile] = File(...),
    user: User = Depends(get_current_user),
    tenant: MgmtTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_tenant_db),
):
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


@router.get("", response_model=list[DocumentOut])
async def list_documents(
    q: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_tenant_db),
):
    group_ids = await permissions.user_group_ids(db, user.id)
    cond = permissions.readable_documents_condition(user, group_ids)
    stmt = select(Document).where(cond)
    if q:
        stmt = stmt.where(Document.name.ilike(f"%{q}%"))
    if status_filter:
        stmt = stmt.where(Document.processing_status == status_filter)
    stmt = stmt.order_by(Document.created_at.desc()).limit(200)
    rows = await db.execute(stmt)
    return [DocumentOut.model_validate(d) for d in rows.scalars().all()]


@router.get("/{document_id}", response_model=DocumentDetail)
async def get_document(document_id: str, request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_tenant_db)):
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
    detail["document_types"] = [{"id": str(c.id), "name": c.name, "description": c.description,
                             "is_enabled": c.is_enabled, "active_schema_id": str(c.active_schema_id) if c.active_schema_id else None}
                            for c in doc.document_types]
    detail["summary"] = ({"executive_summary": summary.executive_summary, "highlights": summary.highlights,
                          "model_version": summary.model_version} if summary else None)
    detail["fields"] = _build_field_tree(extractions)
    # Back-compat alias for older clients that read a flat list.
    detail["extractions"] = [_field_dict(e) for e in extractions if e.node_kind == "scalar"]
    detail["classifications"] = [{"label": c.label, "confidence": c.confidence, "source": c.source,
                                  "is_override": c.is_override} for c in classifications]
    return detail


@router.get("/{document_id}/download", response_model=PresignedUrl)
async def download(document_id: str, request: Request, version: int | None = Query(default=None),
                   disposition: str = Query(default="attachment"),
                   user: User = Depends(get_current_user), tenant: MgmtTenant = Depends(get_current_tenant),
                   db: AsyncSession = Depends(get_tenant_db)):
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
    filename = None if disposition == "inline" else doc.name
    url = await asyncio.to_thread(get_storage(tenant).presigned_get_url, dv.storage_key, 900, filename)
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


@router.get("/{document_id}/pages/{page_no}/image")
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

    data = _PDF_CACHE.get(dv.storage_key)
    if data is None:
        data = await asyncio.to_thread(get_storage(tenant).get_bytes, dv.storage_key)
        if len(_PDF_CACHE) >= _PDF_CACHE_MAX:
            _PDF_CACHE.pop(next(iter(_PDF_CACHE)))
        _PDF_CACHE[dv.storage_key] = data
    try:
        png = await asyncio.to_thread(_render_page_png, data, page_no, scale)
    except IndexError as e:
        raise HTTPException(404, str(e))
    return Response(png, media_type="image/png",
                    headers={"Cache-Control": "private, max-age=3600"})


@router.post("/{document_id}/extractions/{field_value_id}/review")
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


@router.post("/{document_id}/extractions/bulk-review")
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
    pending_fields = (await db.execute(
        select(FieldValue).where(
            FieldValue.document_id == doc.id, FieldValue.document_version == doc.current_version,
            FieldValue.node_kind == "scalar", FieldValue.review_status == RV_PENDING)
    )).scalars().all()

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


@router.post("/{document_id}/extractions/{field_value_id}/edit")
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


@router.get("/{document_id}/layout")
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


@router.get("/{document_id}/versions")
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


@router.post("/{document_id}/versions", response_model=DocumentOut, status_code=201)
async def upload_new_version(document_id: str, file: UploadFile = File(...),
                             user: User = Depends(get_current_user),
                             tenant: MgmtTenant = Depends(get_current_tenant),
                             db: AsyncSession = Depends(get_tenant_db)):
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
    doc.file_size = len(data)
    doc.processing_status = DOC_PENDING
    await db.commit()
    _trigger_pipeline(doc.id, tenant.id)
    return DocumentOut.model_validate(doc)


@router.post("/{document_id}/reprocess", response_model=dict)
async def reprocess(document_id: str, from_stage: str | None = Query(default=None),
                    user: User = Depends(get_current_user), tenant: MgmtTenant = Depends(get_current_tenant),
                    db: AsyncSession = Depends(get_tenant_db)):
    doc = await db.get(Document, document_id)
    if not doc or not await permissions.has_permission(db, user, doc.id, PERM_UPDATE):
        raise HTTPException(404, "Document not found or no update permission")
    if from_stage and from_stage not in PIPELINE_STAGES:
        raise HTTPException(400, f"from_stage must be one of {PIPELINE_STAGES}")
    _trigger_pipeline(doc.id, tenant.id, from_stage)
    return {"status": "queued", "document_id": str(doc.id), "from_stage": from_stage}


@router.delete("/{document_id}", status_code=204)
async def soft_delete(document_id: str, request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_tenant_db)):
    doc = await db.get(Document, document_id)
    if not doc or not await permissions.has_permission(db, user, doc.id, PERM_DELETE):
        raise HTTPException(404, "Document not found or no delete permission")
    doc.is_deleted = True
    doc.deleted_at = dt.datetime.now(dt.timezone.utc)
    await audit.log_event(db, tenant_id=user.tenant_id, actor_id=user.id, event_type=AUDIT_DELETE,
                          object_type="document", object_id=doc.id, ip_address=client_ip(request), commit=False)
    await db.commit()


@router.post("/{document_id}/restore", response_model=DocumentOut)
async def restore(document_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_tenant_db)):
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
