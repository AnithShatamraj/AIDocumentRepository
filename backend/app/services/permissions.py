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
def permitted_documents_condition(
    user: User, group_ids: list[uuid.UUID], level: str = PERM_READ
) -> ColumnElement[bool]:
    """A boolean condition (usable wherever `Document` is in the FROM) that is
    true for documents the user holds `level` on. Admins hold everything in
    their tenant; owners hold every level on their own documents; everyone else
    needs a matching ACL grant. Read is satisfied by a grant at any level;
    higher levels require that level or `manage` — the same rule
    `has_permission` applies to a single document, expressed as SQL so a whole
    result set can be filtered in one query."""
    base = and_(Document.tenant_id == user.tenant_id, Document.is_deleted.is_(False))
    if user.role == ROLE_ADMIN:
        return base

    principal = [DocumentPermission.user_id == user.id]
    if group_ids:
        principal.append(DocumentPermission.group_id.in_(group_ids))

    conds = [DocumentPermission.document_id == Document.id, or_(*principal)]
    if level != PERM_READ:
        conds.append(DocumentPermission.level.in_([level, PERM_MANAGE]))

    acl = exists(select(DocumentPermission.id).where(*conds))
    return and_(base, or_(Document.owner_id == user.id, acl))


def readable_documents_condition(
    user: User, group_ids: list[uuid.UUID]
) -> ColumnElement[bool]:
    """Documents the user may READ. The workhorse of permission-aware retrieval:
    every search / Q&A / structured-search path filters through it."""
    return permitted_documents_condition(user, group_ids, PERM_READ)


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
