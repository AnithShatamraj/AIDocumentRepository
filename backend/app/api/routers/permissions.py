"""Per-document access control (grant/revoke), gated by Manage permission."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import client_ip, get_current_user, get_tenant_db
from app.models.constants import AUDIT_PERM_CHANGE, PERM_LEVELS, PERM_MANAGE
from app.models.document import Document, DocumentPermission
from app.models.tenant import User
from app.schemas import PermissionGrant, PermissionOut
from app.services import audit, permissions

router = APIRouter(prefix="/documents/{document_id}/permissions", tags=["permissions"])


async def _require_manage(db, user, document_id) -> Document:
    doc = await db.get(Document, document_id)
    if not doc or doc.tenant_id != user.tenant_id:
        raise HTTPException(404, "Document not found")
    if not (user.role == "administrator" or doc.owner_id == user.id
            or await permissions.has_permission(db, user, doc.id, PERM_MANAGE)):
        raise HTTPException(403, "Manage permission required")
    return doc


@router.get("", response_model=list[PermissionOut])
async def list_permissions(document_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_tenant_db)):
    await _require_manage(db, user, document_id)
    rows = (await db.execute(
        select(DocumentPermission).where(DocumentPermission.document_id == document_id)
    )).scalars().all()
    return [PermissionOut.model_validate(p) for p in rows]


@router.post("", response_model=PermissionOut, status_code=201)
async def grant(document_id: str, body: PermissionGrant, request: Request,
                user: User = Depends(get_current_user), db: AsyncSession = Depends(get_tenant_db)):
    doc = await _require_manage(db, user, document_id)
    if body.level not in PERM_LEVELS:
        raise HTTPException(400, f"level must be one of {PERM_LEVELS}")
    if bool(body.user_id) == bool(body.group_id):
        raise HTTPException(400, "Provide exactly one of user_id or group_id")

    existing = (await db.execute(
        select(DocumentPermission).where(and_(
            DocumentPermission.document_id == doc.id,
            DocumentPermission.user_id == body.user_id,
            DocumentPermission.group_id == body.group_id,
            DocumentPermission.level == body.level))
    )).scalars().first()
    if existing:
        return PermissionOut.model_validate(existing)

    perm = DocumentPermission(tenant_id=user.tenant_id, document_id=doc.id, user_id=body.user_id,
                              group_id=body.group_id, level=body.level, granted_by=user.id)
    db.add(perm)
    await audit.log_event(db, tenant_id=user.tenant_id, actor_id=user.id, event_type=AUDIT_PERM_CHANGE,
                          object_type="document", object_id=doc.id,
                          detail={"action": "grant", "level": body.level,
                                  "user_id": str(body.user_id) if body.user_id else None,
                                  "group_id": str(body.group_id) if body.group_id else None},
                          ip_address=client_ip(request), commit=False)
    await db.commit()
    await db.refresh(perm)
    return PermissionOut.model_validate(perm)


@router.delete("/{permission_id}", status_code=204)
async def revoke(document_id: str, permission_id: str, request: Request,
                 user: User = Depends(get_current_user), db: AsyncSession = Depends(get_tenant_db)):
    doc = await _require_manage(db, user, document_id)
    perm = await db.get(DocumentPermission, permission_id)
    if not perm or perm.document_id != doc.id:
        raise HTTPException(404, "Permission not found")
    await audit.log_event(db, tenant_id=user.tenant_id, actor_id=user.id, event_type=AUDIT_PERM_CHANGE,
                          object_type="document", object_id=doc.id,
                          detail={"action": "revoke", "level": perm.level}, ip_address=client_ip(request), commit=False)
    await db.delete(perm)
    await db.commit()
