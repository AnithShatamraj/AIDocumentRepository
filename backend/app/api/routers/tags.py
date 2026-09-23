"""Tag taxonomy: list, create, rename, merge, delete.

Applying tags to a document lives on the documents router (`PUT
/api/documents/{id}/tags` and `POST /api/documents/bulk-tag`) — that's an edit
of the document, gated on `update` permission. This router is the shared
taxonomy itself, so reshaping it is admin-only.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_tenant_db, require_admin
from app.models.constants import AUDIT_TAG_CHANGE, ROLE_ADMIN
from app.models.document import Document
from app.models.tag import Tag, document_tags
from app.models.tenant import User
from app.schemas import TagCreate, TagMerge, TagOut, TagRename, TagWithCount
from app.services import audit, permissions, tags as tag_service

router = APIRouter(prefix="/tags", tags=["tags"])


async def _get(db: AsyncSession, tag_id: str, tenant_id) -> Tag:
    tag = await db.get(Tag, tag_id)
    if not tag or tag.tenant_id != tenant_id:
        raise HTTPException(404, "Tag not found")
    return tag


@router.get("", response_model=list[TagWithCount], summary="List tags with document counts")
async def list_tags(
    q: str | None = Query(default=None, description="Substring filter, for autocomplete"),
    include_empty: bool = Query(default=False, description="Admin only: keep tags no readable document carries"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Every tag carrying at least one document the caller can read, with that
    caller's own count.

    A tag whose documents are all unreadable is omitted entirely: the *name*
    alone would disclose that the thing exists. `include_empty` lifts that for
    taxonomy management and is refused to non-admins for the same reason.
    """
    if include_empty and user.role != ROLE_ADMIN:
        raise HTTPException(403, "include_empty requires an admin role")
    return await tag_service.list_with_counts(db, user, q=q, include_empty=include_empty)


@router.post("", response_model=TagOut, status_code=201, summary="Create a tag")
async def create_tag(body: TagCreate, user: User = Depends(get_current_user),
                     db: AsyncSession = Depends(get_tenant_db)):
    """Any member may add to the shared taxonomy — tagging is how people
    organize, and gating creation behind an admin makes it unusable. 409 if the
    name already exists (case-insensitively)."""
    try:
        name = tag_service.normalize(body.name)
    except tag_service.TagError as e:
        raise HTTPException(422, str(e))
    if await tag_service.find_by_name(db, user.tenant_id, name):
        raise HTTPException(409, f"A tag named '{name}' already exists")
    tag = await tag_service.get_or_create(db, tenant_id=user.tenant_id, name=name, created_by=user.id)
    await audit.log_event(db, tenant_id=user.tenant_id, actor_id=user.id,
                          event_type=AUDIT_TAG_CHANGE, object_type="tag", object_id=tag.id,
                          detail={"action": "create", "name": tag.name}, commit=False)
    await db.commit()
    await db.refresh(tag)
    return TagOut.model_validate(tag)


@router.patch("/{tag_id}", response_model=TagOut, summary="Rename a tag (admin)")
async def rename_tag(tag_id: str, body: TagRename, admin: User = Depends(require_admin),
                     db: AsyncSession = Depends(get_tenant_db)):
    """Renaming changes what every user sees, hence admin-only. 409 if the new
    name is taken — merge into that tag instead."""
    tag = await _get(db, tag_id, admin.tenant_id)
    was = tag.name
    try:
        await tag_service.rename(db, tag=tag, new_name=body.name)
    except tag_service.TagError as e:
        raise HTTPException(409, str(e))
    await audit.log_event(db, tenant_id=admin.tenant_id, actor_id=admin.id,
                          event_type=AUDIT_TAG_CHANGE, object_type="tag", object_id=tag.id,
                          detail={"action": "rename", "from": was, "to": tag.name}, commit=False)
    await db.commit()
    await db.refresh(tag)
    return TagOut.model_validate(tag)


@router.post("/{tag_id}/merge", summary="Merge a tag into another (admin)")
async def merge_tag(tag_id: str, body: TagMerge, admin: User = Depends(require_admin),
                    db: AsyncSession = Depends(get_tenant_db)):
    """Move every document from this tag onto `into_id`, then delete this one.
    Documents already carrying both keep a single link."""
    source = await _get(db, tag_id, admin.tenant_id)
    target = await _get(db, str(body.into_id), admin.tenant_id)
    source_name = source.name
    try:
        moved = await tag_service.merge(db, source=source, target=target)
    except tag_service.TagError as e:
        raise HTTPException(409, str(e))
    await audit.log_event(db, tenant_id=admin.tenant_id, actor_id=admin.id,
                          event_type=AUDIT_TAG_CHANGE, object_type="tag", object_id=target.id,
                          detail={"action": "merge", "from": source_name, "into": target.name,
                                  "documents_moved": moved}, commit=False)
    await db.commit()
    return {"merged_into": str(target.id), "name": target.name, "documents_moved": moved}


@router.delete("/{tag_id}", status_code=204, summary="Delete a tag (admin)")
async def delete_tag(tag_id: str, admin: User = Depends(require_admin),
                     db: AsyncSession = Depends(get_tenant_db)):
    """Removes the tag from every document that carries it (the link rows
    cascade). The documents themselves are untouched."""
    tag = await _get(db, tag_id, admin.tenant_id)
    used = (await db.execute(
        select(func.count()).select_from(document_tags).where(document_tags.c.tag_id == tag.id)
    )).scalar() or 0
    name = tag.name
    await db.delete(tag)
    await audit.log_event(db, tenant_id=admin.tenant_id, actor_id=admin.id,
                          event_type=AUDIT_TAG_CHANGE, object_type="tag", object_id=tag_id,
                          detail={"action": "delete", "name": name, "documents_affected": used},
                          commit=False)
    await db.commit()


@router.get("/{tag_id}/documents", summary="List the documents carrying a tag")
async def tag_documents(tag_id: str, user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_tenant_db)):
    """Permission-filtered: the caller sees only the documents they can read,
    so the count here can be lower than another user's for the same tag."""
    tag = await _get(db, tag_id, user.tenant_id)
    group_ids = await permissions.user_group_ids(db, user.id)
    cond = permissions.readable_documents_condition(user, group_ids)
    rows = await db.execute(
        select(Document.id, Document.name, Document.processing_status)
        .join(document_tags, document_tags.c.document_id == Document.id)
        .where(document_tags.c.tag_id == tag.id, cond)
        .order_by(Document.created_at.desc())
        .limit(200)
    )
    return {"tag": {"id": str(tag.id), "name": tag.name},
            "documents": [{"id": str(i), "name": n, "processing_status": s} for i, n, s in rows.all()]}
