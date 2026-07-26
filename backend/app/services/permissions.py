"""Permission resolution — ACLs resolved at query time by JOIN, never denormalized.

The single most important invariant in the system (Section 4 of the spec):
permission changes must be instantly effective across search / Q&A / structured
search with zero reindexing, because we resolve them live against Postgres.
"""
from __future__ import annotations

import uuid

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models.constants import PERM_MANAGE, PERM_READ, ROLE_ADMIN
from app.models.document import Document, DocumentPermission
from app.models.tenant import User, user_group


# ---------------------------------------------------------------- group lookups
async def user_group_ids(db: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await db.execute(select(user_group.c.group_id).where(user_group.c.user_id == user_id))
    return [r[0] for r in rows.all()]


def user_group_ids_sync(db: Session, user_id: uuid.UUID) -> list[uuid.UUID]:
    rows = db.execute(select(user_group.c.group_id).where(user_group.c.user_id == user_id))
    return [r[0] for r in rows.all()]


# ------------------------------------------------------ reusable SQL conditions
def readable_documents_condition(
    user: User, group_ids: list[uuid.UUID]
) -> ColumnElement[bool]:
    """A boolean condition (usable wherever `Document` is in the FROM) that is
    true for documents the user may READ. Admins see all tenant docs; owners see
    their own; everyone else needs a matching ACL grant (any level implies read)."""
    base = and_(Document.tenant_id == user.tenant_id, Document.is_deleted.is_(False))
    if user.role == ROLE_ADMIN:
        return base

    principal = [DocumentPermission.user_id == user.id]
    if group_ids:
        principal.append(DocumentPermission.group_id.in_(group_ids))

    acl = exists(
        select(DocumentPermission.id).where(
            DocumentPermission.document_id == Document.id,
            or_(*principal),
        )
    )
    return and_(base, or_(Document.owner_id == user.id, acl))


# ------------------------------------------------------------- point checks
async def has_permission(
    db: AsyncSession, user: User, document_id: uuid.UUID, level: str
) -> bool:
    doc = await db.get(Document, document_id)
    if doc is None or doc.tenant_id != user.tenant_id or doc.is_deleted:
        return False
    if user.role == ROLE_ADMIN:
        return True
    if doc.owner_id == user.id:  # owner implicitly holds all levels incl. manage
        return True

    group_ids = await user_group_ids(db, user.id)
    principal = [DocumentPermission.user_id == user.id]
    if group_ids:
        principal.append(DocumentPermission.group_id.in_(group_ids))

    # read is satisfied by any grant; higher levels require that level or manage.
    if level == PERM_READ:
        levels = None
    else:
        levels = [level, PERM_MANAGE]

    conds = [DocumentPermission.document_id == document_id, or_(*principal)]
    if levels is not None:
        conds.append(DocumentPermission.level.in_(levels))
    found = await db.execute(select(DocumentPermission.id).where(and_(*conds)).limit(1))
    return found.first() is not None


async def can_read(db: AsyncSession, user: User, document_id: uuid.UUID) -> bool:
    return await has_permission(db, user, document_id, PERM_READ)
