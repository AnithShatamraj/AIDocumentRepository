"""Document library: upload, versioning, download (presigned), soft-delete, reprocess."""
from __future__ import annotations

import asyncio
import datetime as dt
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import client_ip, get_current_user, require_reviewer
from app.core.config import settings
from app.core.db import get_db
from app.models.catalog import Category
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
    PERM_DELETE,
    PERM_MANAGE,
    PERM_UPDATE,
    PIPELINE_STAGES,
    RV_PENDING,
    RV_REJECTED,
    RV_VERIFIED,
)
from app.models.content import Classification, Extraction, Summary
from app.models.document import Document, DocumentVersion
from app.models.review import ReviewItem
from app.models.tenant import User
from app.schemas import DocumentDetail, DocumentOut, ExtractionReview, PresignedUrl
from app.services import audit, mime, permissions
from app.services.hashing import sha256_hex
from app.storage import get_storage

router = APIRouter(prefix="/documents", tags=["documents"])
RETENTION_DAYS = 30


def _storage_key(tenant_id, document_id, version, filename) -> str:
    return f"tenant/{tenant_id}/doc/{document_id}/v{version}/{filename}"


async def _create_document(db, user, filename, data, description) -> tuple[Document, dict]:
    ext, mime_type = mime.validate(filename, data, settings.max_upload_mb * 1024 * 1024)
    content_hash = sha256_hex(data)

    dup = (await db.execute(
        select(Document).where(Document.tenant_id == user.tenant_id, Document.content_hash == content_hash,
                               Document.is_deleted.is_(False))
    )).scalars().first()

    doc = Document(
        tenant_id=user.tenant_id, owner_id=user.id, name=filename, description=description,
        file_type=ext, mime_type=mime_type, file_size=len(data), content_hash=content_hash,
        current_version=1, processing_status=DOC_PENDING)
    db.add(doc)
    await db.flush()

    key = _storage_key(user.tenant_id, doc.id, 1, filename)
    await asyncio.to_thread(get_storage().put_object, key, data, mime_type)
    db.add(DocumentVersion(
        tenant_id=user.tenant_id, document_id=doc.id, version=1, storage_key=key,
        file_size=len(data), content_hash=content_hash, mime_type=mime_type, uploaded_by=user.id))
    await db.flush()
    return doc, {"duplicate_of": str(dup.id) if dup else None}


def _trigger_pipeline(document_id, from_stage=None):
    from app.worker.tasks import start_processing

    start_processing.delay(str(document_id), from_stage, str(uuid.uuid4()))


@router.post("", response_model=dict, status_code=201)
async def upload(
    request: Request,
    file: UploadFile = File(...),
    description: str = Form(""),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    data = await file.read()
    try:
        doc, meta = await _create_document(db, user, file.filename, data, description)
    except mime.UploadValidationError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    await audit.log_event(db, tenant_id=user.tenant_id, actor_id=user.id, event_type=AUDIT_UPLOAD,
                          object_type="document", object_id=doc.id, ip_address=client_ip(request), commit=False)
    await db.commit()
    _trigger_pipeline(doc.id)
    return {"document": DocumentOut.model_validate(doc).model_dump(), **meta}


@router.post("/bulk", response_model=dict, status_code=201)
async def bulk_upload(
    request: Request,
    files: list[UploadFile] = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    results = []
    for f in files:
        data = await f.read()
        try:
            doc, meta = await _create_document(db, user, f.filename, data, "")
            await audit.log_event(db, tenant_id=user.tenant_id, actor_id=user.id, event_type=AUDIT_UPLOAD,
                                  object_type="document", object_id=doc.id, commit=False)
            await db.commit()
            _trigger_pipeline(doc.id)
            results.append({"filename": f.filename, "status": "accepted", "document_id": str(doc.id),
                            "duplicate_of": meta["duplicate_of"]})
        except mime.UploadValidationError as e:
            await db.rollback()
            results.append({"filename": f.filename, "status": "rejected", "error": str(e)})
    return {"results": results}


@router.get("", response_model=list[DocumentOut])
async def list_documents(
    q: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
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
async def get_document(document_id: str, request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    doc = await db.get(Document, document_id)
    if not doc or not await permissions.can_read(db, user, doc.id):
        raise HTTPException(404, "Document not found")

    summary = (await db.execute(
        select(Summary).where(Summary.document_id == doc.id, Summary.document_version == doc.current_version)
    )).scalars().first()
    extractions = (await db.execute(
        select(Extraction).where(Extraction.document_id == doc.id, Extraction.document_version == doc.current_version)
    )).scalars().all()
    classifications = (await db.execute(
        select(Classification).where(Classification.document_id == doc.id)
    )).scalars().all()
    await db.refresh(doc, attribute_names=["categories"])
    await audit.log_event(db, tenant_id=user.tenant_id, actor_id=user.id, event_type=AUDIT_VIEW,
                          object_type="document", object_id=doc.id, ip_address=client_ip(request))

    detail = DocumentDetail.model_validate(doc).model_dump()
    detail["categories"] = [{"id": str(c.id), "name": c.name, "description": c.description,
                             "is_enabled": c.is_enabled, "active_schema_id": str(c.active_schema_id) if c.active_schema_id else None}
                            for c in doc.categories]
    detail["summary"] = ({"executive_summary": summary.executive_summary, "highlights": summary.highlights,
                          "model_version": summary.model_version} if summary else None)
    detail["extractions"] = [{
        "id": str(e.id), "field_name": e.field_name, "raw_value": e.raw_value, "value_type": e.value_type,
        "value_text": e.value_text, "value_number": e.value_number,
        "value_date": e.value_date.isoformat() if e.value_date else None, "value_currency": e.value_currency,
        "confidence": e.confidence, "review_status": e.review_status, "source_page": e.source_page,
        "source_text": e.source_text, "source_bbox": e.source_bbox} for e in extractions]
    detail["classifications"] = [{"label": c.label, "confidence": c.confidence, "source": c.source,
                                  "is_override": c.is_override} for c in classifications]
    return detail


@router.get("/{document_id}/download", response_model=PresignedUrl)
async def download(document_id: str, request: Request, version: int | None = Query(default=None),
                   disposition: str = Query(default="attachment"),
                   user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
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
    url = await asyncio.to_thread(get_storage().presigned_get_url, dv.storage_key, 900, filename)
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
                     user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
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
        data = await asyncio.to_thread(get_storage().get_bytes, dv.storage_key)
        if len(_PDF_CACHE) >= _PDF_CACHE_MAX:
            _PDF_CACHE.pop(next(iter(_PDF_CACHE)))
        _PDF_CACHE[dv.storage_key] = data
    try:
        png = await asyncio.to_thread(_render_page_png, data, page_no, scale)
    except IndexError as e:
        raise HTTPException(404, str(e))
    return Response(png, media_type="image/png",
                    headers={"Cache-Control": "private, max-age=3600"})


@router.post("/{document_id}/extractions/{extraction_id}/review")
async def review_extraction(
    document_id: str,
    extraction_id: str,
    body: ExtractionReview,
    user: User = Depends(require_reviewer),
    db: AsyncSession = Depends(get_db),
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
    ext = await db.get(Extraction, extraction_id)
    if not ext or str(ext.document_id) != str(doc.id):
        raise HTTPException(404, "Extraction not found")

    ext.review_status = RV_VERIFIED if body.action == "accept" else RV_REJECTED

    # Resolve the queue item that was raised for this field, if any.
    item = (await db.execute(
        select(ReviewItem).where(ReviewItem.extraction_id == ext.id, ReviewItem.status == RV_PENDING)
    )).scalars().first()
    now = dt.datetime.now(dt.timezone.utc)
    if item:
        item.status = ext.review_status
        item.resolved_by = user.id
        item.resolved_at = now
        item.notes = body.notes
        # Learning-loop feedstock: (input, prediction, correction).
        item.payload = {**(item.payload or {}), "action": body.action, "correction": None}

    # Flip the document out of needs_review once nothing is pending.
    # Flush first so the count sees this item's new status, not a stale one.
    await db.flush()
    remaining = (await db.execute(
        select(func.count()).select_from(ReviewItem).where(
            ReviewItem.document_id == doc.id, ReviewItem.status == RV_PENDING)
    )).scalar() or 0
    if remaining == 0 and doc.processing_status in (DOC_NEEDS_REVIEW, DOC_PROCESSED):
        doc.processing_status = DOC_PROCESSED
    elif remaining and doc.processing_status not in (DOC_FAILED,):
        doc.processing_status = DOC_NEEDS_REVIEW

    await audit.log_event(
        db, tenant_id=user.tenant_id, actor_id=user.id, event_type=AUDIT_REVIEW,
        object_type="extraction", object_id=ext.id,
        detail={"action": body.action, "field": ext.field_name, "inline": True}, commit=False)
    await db.commit()
    return {
        "id": str(ext.id), "field_name": ext.field_name, "review_status": ext.review_status,
        "document_status": doc.processing_status, "pending_remaining": remaining,
    }


@router.get("/{document_id}/layout")
async def layout(document_id: str, version: int | None = Query(default=None),
                 include_blocks: bool = Query(default=False),
                 user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
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
async def list_versions(document_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
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
                             user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
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
    await asyncio.to_thread(get_storage().put_object, key, data, mime_type)
    db.add(DocumentVersion(tenant_id=user.tenant_id, document_id=doc.id, version=new_v, storage_key=key,
                           file_size=len(data), content_hash=sha256_hex(data), mime_type=mime_type, uploaded_by=user.id))
    doc.current_version = new_v
    doc.file_size = len(data)
    doc.processing_status = DOC_PENDING
    await db.commit()
    _trigger_pipeline(doc.id)
    return DocumentOut.model_validate(doc)


@router.post("/{document_id}/reprocess", response_model=dict)
async def reprocess(document_id: str, from_stage: str | None = Query(default=None),
                    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    doc = await db.get(Document, document_id)
    if not doc or not await permissions.has_permission(db, user, doc.id, PERM_UPDATE):
        raise HTTPException(404, "Document not found or no update permission")
    if from_stage and from_stage not in PIPELINE_STAGES:
        raise HTTPException(400, f"from_stage must be one of {PIPELINE_STAGES}")
    _trigger_pipeline(doc.id, from_stage)
    return {"status": "queued", "document_id": str(doc.id), "from_stage": from_stage}


@router.delete("/{document_id}", status_code=204)
async def soft_delete(document_id: str, request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    doc = await db.get(Document, document_id)
    if not doc or not await permissions.has_permission(db, user, doc.id, PERM_DELETE):
        raise HTTPException(404, "Document not found or no delete permission")
    doc.is_deleted = True
    doc.deleted_at = dt.datetime.now(dt.timezone.utc)
    await audit.log_event(db, tenant_id=user.tenant_id, actor_id=user.id, event_type=AUDIT_DELETE,
                          object_type="document", object_id=doc.id, ip_address=client_ip(request), commit=False)
    await db.commit()


@router.post("/{document_id}/restore", response_model=DocumentOut)
async def restore(document_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
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
