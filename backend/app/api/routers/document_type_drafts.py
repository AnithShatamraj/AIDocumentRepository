"""AI-assisted document type authoring: a guided chat that ends in a published type.

A *draft* is one resumable session: the conversation, the work-in-progress
name/description/field tree, and any sample documents. Nothing is created in
the document-type catalog until `publish`. Administrators only -- the same
rule as creating a type directly.
"""
from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_tenant, get_tenant_db, require_admin
from app.core.config import settings
from app.models.mgmt import Tenant as MgmtTenant
from app.models.tenant import User
from app.models.type_draft import (
    DRAFT_ACTIVE,
    DocumentTypeDraft,
    DocumentTypeDraftSample,
)
from app.schemas import DocumentTypeOut, DraftMessageIn, DraftUpdate, DraftValidateIn
from app.services import mime
from app.services import type_builder as tb

router = APIRouter(prefix="/document-type-drafts", tags=["document-type-drafts"])


async def _load(db: AsyncSession, admin: User, draft_id: str) -> DocumentTypeDraft:
    try:
        key = uuid.UUID(draft_id)
    except ValueError:
        raise HTTPException(404, "Draft not found")
    d = await db.get(DocumentTypeDraft, key)
    if d is None or d.tenant_id != admin.tenant_id:
        raise HTTPException(404, "Draft not found")
    return d


def _raise(e: tb.DraftError):
    raise HTTPException(e.status_code, e.detail)


@router.post("", status_code=201, summary="Start a new draft")
async def create_draft(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_tenant_db)):
    """Creates an empty draft with the assistant's opening question already in
    the conversation. Returns the full draft (same shape as `GET /{id}`)."""
    d = await tb.create_draft(db, admin)
    await db.commit()
    return await tb.draft_detail(db, d)


@router.get("", summary="List drafts")
async def list_drafts(
    status_filter: str = Query(default=DRAFT_ACTIVE, alias="status"),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Drafts in this tenant, most recently touched first. Defaults to the
    in-progress ones (`status=draft`); `published` lists ones already turned
    into a document type."""
    rows = (await db.execute(
        select(DocumentTypeDraft).where(
            DocumentTypeDraft.tenant_id == admin.tenant_id, DocumentTypeDraft.status == status_filter)
        .order_by(DocumentTypeDraft.updated_at.desc()).limit(100))).scalars().all()
    counts = dict((await db.execute(
        select(DocumentTypeDraftSample.draft_id, func.count()).where(
            DocumentTypeDraftSample.draft_id.in_([d.id for d in rows]))
        .group_by(DocumentTypeDraftSample.draft_id))).all()) if rows else {}
    return [tb.draft_summary(d, counts.get(d.id, 0)) for d in rows]


@router.get("/{draft_id}", summary="Get a draft with its conversation")
async def get_draft(draft_id: str, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_tenant_db)):
    """Everything the builder page needs in one response: stage, name,
    description, the field tree, samples (with processing status), the full
    conversation (`messages`, oldest first) and the latest test results."""
    return await tb.draft_detail(db, await _load(db, admin, draft_id))


@router.patch("/{draft_id}", summary="Save edits made in the editor panel")
async def update_draft(draft_id: str, body: DraftUpdate, admin: User = Depends(require_admin),
                       db: AsyncSession = Depends(get_tenant_db)):
    """Autosave target for the right-hand panel. Fields are stored as sent and
    validated only when tested or published."""
    d = await _load(db, admin, draft_id)
    if d.status != DRAFT_ACTIVE:
        raise HTTPException(409, "This draft has already been published.")
    if body.name is not None:
        d.name = body.name.strip()[:255]
    if body.description is not None:
        d.description = body.description[:tb.MAX_DESCRIPTION_STORED]
    if body.fields is not None:
        if len(body.fields) > 200:
            raise HTTPException(422, "Too many top-level fields.")
        d.fields = body.fields
    tb.touch(d)
    await db.commit()
    return {"id": str(d.id), "updated_at": d.updated_at.isoformat()}


@router.delete("/{draft_id}", status_code=204, summary="Discard a draft")
async def delete_draft(draft_id: str, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_tenant_db)):
    """Deletes the draft, its conversation and its samples. A published draft
    is kept (it's the record of how the type was authored)."""
    d = await _load(db, admin, draft_id)
    if d.status != DRAFT_ACTIVE:
        raise HTTPException(409, "A published draft can't be discarded.")
    await db.delete(d)
    await db.commit()


@router.post("/{draft_id}/messages", summary="Send a chat turn")
async def send_message(draft_id: str, body: DraftMessageIn, admin: User = Depends(require_admin),
                       db: AsyncSession = Depends(get_tenant_db)):
    """One admin turn -- free text in `content`, or a button `action`
    (`describe_from_samples`, `confirm_samples`, `confirm_description`,
    `accept_name`, `suggest_name`, `skip_samples`, `derive_fields`). The reply
    depends on the draft's `stage`: it refines the description (optionally
    written from uploaded samples), proposes/accepts a name, or designs and
    refines the fields. Samples uploaded before the name step are used
    automatically, so the "do you have samples?" question is skipped.

    Before any AI step reads two or more samples it checks that they look like
    one kind of document. If not, it replies with a `sample_mismatch` message
    (what each file appears to be, which look different) and waits: remove the
    odd files and re-run the step, or send `confirm_samples` to proceed anyway.

    Blocks while the AI works (typically 3-20 s). An AI failure comes back as
    an assistant message, not an HTTP error, so the admin can just retry.
    Returns the full updated draft."""
    d = await _load(db, admin, draft_id)
    try:
        await tb.handle_turn(db, d, admin, content=body.content, action=body.action, label=body.label)
    except tb.DraftError as e:
        _raise(e)
    return await tb.draft_detail(db, d)


@router.post("/{draft_id}/samples", status_code=201, summary="Upload sample documents")
async def upload_samples(
    draft_id: str,
    background: BackgroundTasks,
    files: list[UploadFile] = File(...),
    admin: User = Depends(require_admin),
    tenant: MgmtTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_tenant_db),
):
    """`multipart/form-data`, one or more files (max 5 per draft). Each is
    validated, then its text is extracted in the background -- poll
    `GET /{id}` until no sample is `processing`. Only the extracted text is
    kept, not the file."""
    d = await _load(db, admin, draft_id)
    if d.status != DRAFT_ACTIVE:
        raise HTTPException(409, "This draft has already been published.")
    existing = (await db.execute(
        select(func.count()).select_from(DocumentTypeDraftSample).where(
            DocumentTypeDraftSample.draft_id == d.id))).scalar() or 0
    if existing + len(files) > tb.MAX_SAMPLES:
        raise HTTPException(400, f"At most {tb.MAX_SAMPLES} sample documents per draft.")

    created, jobs = [], []
    for f in files:
        data = await f.read()
        try:
            ext, _ = mime.validate(f.filename or "", data, settings.max_upload_mb * 1024 * 1024)
        except mime.UploadValidationError as e:
            raise HTTPException(400, f"{f.filename}: {e}")
        s = DocumentTypeDraftSample(draft_id=d.id, filename=(f.filename or "sample")[:512],
                                    file_type=ext, file_size=len(data))
        db.add(s)
        created.append(s)
        jobs.append((s, data, ext))
    tb.touch(d)
    await db.commit()
    for s, data, ext in jobs:
        background.add_task(tb.process_sample, tenant, s.id, data, ext)
    await db.refresh(d)
    return {"samples": [tb.sample_out(s) for s in await tb.list_samples(db, d.id)]}


@router.delete("/{draft_id}/samples/{sample_id}", status_code=204, summary="Remove a sample document")
async def delete_sample(draft_id: str, sample_id: str, admin: User = Depends(require_admin),
                        db: AsyncSession = Depends(get_tenant_db)):
    d = await _load(db, admin, draft_id)
    try:
        s = await db.get(DocumentTypeDraftSample, uuid.UUID(sample_id))
    except ValueError:
        s = None
    if s is None or s.draft_id != d.id:
        raise HTTPException(404, "Sample not found")
    await db.delete(s)
    d.last_validation = {k: v for k, v in (d.last_validation or {}).items() if k != sample_id}
    tb.touch(d)
    await db.commit()


@router.post("/{draft_id}/validate", summary="Test the fields on sample documents")
async def validate_draft(draft_id: str, body: DraftValidateIn, admin: User = Depends(require_admin),
                         db: AsyncSession = Depends(get_tenant_db)):
    """Runs the draft's current fields against the sample(s) with the same
    extractor and text limit the real pipeline uses, so what you see here is
    what documents of this type will get. The result (per sample: fields
    found/total, which are missing or low-confidence, and every extracted
    value) is saved on the draft and summarised in the chat. 422 if the
    fields aren't valid yet; 400 if there's no ready sample or no AI provider.
    Returns the full updated draft."""
    d = await _load(db, admin, draft_id)
    try:
        await tb.run_validation(db, d, admin, body.sample_id)
    except tb.DraftError as e:
        _raise(e)
    return await tb.draft_detail(db, d)


@router.post("/{draft_id}/publish", summary="Publish the draft as a document type")
async def publish_draft(draft_id: str, admin: User = Depends(require_admin),
                        db: AsyncSession = Depends(get_tenant_db)):
    """Validates the fields, creates the document type with schema v1, and
    marks the draft published. Only allowed once the guided steps are done
    (stage `fields`). 409 if the name is taken, 422 if the name or fields are
    invalid."""
    d = await _load(db, admin, draft_id)
    try:
        dt = await tb.publish(db, d, admin)
    except tb.DraftError as e:
        _raise(e)
    return {"document_type": DocumentTypeOut.model_validate(dt).model_dump(mode="json"), "draft_id": str(d.id)}
