"""Tag taxonomy and document tagging.

Two rules shape everything in here.

**Tag names leak.** A tag is tenant-wide, but the documents carrying it are not:
offering "Project Falcon" in an autocomplete to someone who can read none of its
documents discloses that the project exists. So every listing counts through
`readable_documents_condition` and a tag with no readable documents is simply
not returned -- the same non-disclosure stance the duplicate-upload policy
already takes.

**Tags are set by people, never by the pipeline.** Applying one needs `update`
on the document; reshaping the shared taxonomy (rename / merge / delete) is
admin-only, because it changes what every other user sees.
"""
from __future__ import annotations

import uuid

from sqlalchemy import and_, delete, exists, func, insert, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.constants import PERM_UPDATE
from app.models.document import Document
from app.models.tag import Tag, document_tags
from app.models.tenant import User
from app.services import permissions

MAX_NAME_LEN = 64
MAX_TAGS_PER_DOCUMENT = 50


class TagError(ValueError):
    """Invalid tag name or a conflicting taxonomy change."""


# ------------------------------------------------------------------ names
def normalize(name: str) -> str:
    """Trim, collapse internal whitespace, and reject the unusable.

    Stored with the author's capitalization; compared case-insensitively by the
    unique index, so "Urgent" and "urgent" can never coexist.
    """
    cleaned = " ".join((name or "").split())
    if not cleaned:
        raise TagError("Tag name cannot be empty")
    if len(cleaned) > MAX_NAME_LEN:
        raise TagError(f"Tag name cannot exceed {MAX_NAME_LEN} characters")
    return cleaned


def normalize_many(names: list[str]) -> list[str]:
    """Normalize, then drop case-insensitive duplicates, keeping first-seen order."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in names:
        cleaned = normalize(raw)
        key = cleaned.lower()
        if key not in seen:
            seen.add(key)
            out.append(cleaned)
    if len(out) > MAX_TAGS_PER_DOCUMENT:
        raise TagError(f"A document cannot carry more than {MAX_TAGS_PER_DOCUMENT} tags")
    return out


# ------------------------------------------------------------- tag lookup
async def find_by_name(db: AsyncSession, tenant_id: uuid.UUID, name: str) -> Tag | None:
    row = await db.execute(
        select(Tag).where(Tag.tenant_id == tenant_id, func.lower(Tag.name) == name.lower()))
    return row.scalar_one_or_none()


async def get_or_create(
    db: AsyncSession, *, tenant_id: uuid.UUID, name: str, created_by: uuid.UUID | None
) -> Tag:
    cleaned = normalize(name)
    existing = await find_by_name(db, tenant_id, cleaned)
    if existing is not None:
        return existing
    tag = Tag(tenant_id=tenant_id, name=cleaned, created_by=created_by)
    db.add(tag)
    await db.flush()
    return tag


async def resolve_names(
    db: AsyncSession, tenant_id: uuid.UUID, names: list[str]
) -> dict[str, Tag]:
    """Map lowercased name -> Tag for those that exist. Unknown names are absent."""
    lowered = [n.lower() for n in names]
    if not lowered:
        return {}
    rows = await db.execute(
        select(Tag).where(Tag.tenant_id == tenant_id, func.lower(Tag.name).in_(lowered)))
    return {t.name.lower(): t for t in rows.scalars().all()}


# ------------------------------------------------------------- listing
async def list_with_counts(
    db: AsyncSession, user: User, *, q: str | None = None, include_empty: bool = False
) -> list[dict]:
    """Tags plus how many documents *this caller* can see under each.

    `include_empty` keeps tags that no readable document carries, which is only
    meaningful for taxonomy management -- callers must gate it on admin, or it
    becomes the leak this whole module is built to avoid.
    """
    group_ids = await permissions.user_group_ids(db, user.id)
    cond = permissions.readable_documents_condition(user, group_ids)

    # The permission condition lives in the JOIN's ON clause, not WHERE: with an
    # outer join a WHERE clause would discard the NULL side and silently turn it
    # back into an inner join.
    stmt = (
        select(Tag.id, Tag.name, func.count(Document.id).label("document_count"))
        .select_from(Tag)
        .join(document_tags, document_tags.c.tag_id == Tag.id, isouter=include_empty)
        .join(Document,
              and_(Document.id == document_tags.c.document_id, cond),
              isouter=include_empty)
        .where(Tag.tenant_id == user.tenant_id)
        .group_by(Tag.id, Tag.name)
        .order_by(func.lower(Tag.name))
    )
    if q:
        stmt = stmt.where(Tag.name.ilike(f"%{q}%"))
    if not include_empty:
        # An inner join already drops tags with no readable document, but a tag
        # whose only documents are unreadable would otherwise survive as a
        # zero-count row on some plans. Make the intent explicit.
        stmt = stmt.having(func.count(Document.id) > 0)

    rows = await db.execute(stmt)
    return [{"id": str(tid), "name": name, "document_count": int(count)}
            for tid, name, count in rows.all()]


# --------------------------------------------------------- filter condition
def documents_with_tags_condition(tag_ids: list[uuid.UUID], *, match_all: bool) -> ColumnElement[bool]:
    """Condition selecting documents carrying these tags (all of them, or any).

    Callers must resolve names to ids first and short-circuit when a requested
    name doesn't exist: under `match_all` an unknown tag means nothing can
    match, and counting only the resolved ids would wrongly let documents
    through.
    """
    if not tag_ids:
        return true()
    if not match_all:
        return exists(
            select(document_tags.c.tag_id).where(
                document_tags.c.document_id == Document.id,
                document_tags.c.tag_id.in_(tag_ids),
            )
        )
    # Correlated count: how many of the requested tags this document carries.
    # `tag_ids` is already deduplicated, so equality means "carries them all".
    carried = (
        select(func.count(document_tags.c.tag_id))
        .where(
            document_tags.c.document_id == Document.id,
            document_tags.c.tag_id.in_(tag_ids),
        )
        .scalar_subquery()
    )
    return carried == len(tag_ids)


# ------------------------------------------------------------- assignment
async def document_tag_ids(db: AsyncSession, document_id: uuid.UUID) -> set[uuid.UUID]:
    rows = await db.execute(
        select(document_tags.c.tag_id).where(document_tags.c.document_id == document_id))
    return {r[0] for r in rows.all()}


async def apply_changes(
    db: AsyncSession, *, user: User, document_id: uuid.UUID,
    add: list[Tag] | None = None, remove_ids: set[uuid.UUID] | None = None,
    current: set[uuid.UUID] | None = None,
) -> tuple[int, int]:
    """Add and remove links for one document. Returns (added, removed).

    Diffs rather than delete-all-then-reinsert, so an unchanged tag keeps the
    `created_at`/`created_by` recording who originally applied it. Callers that
    already hold the current link set pass it in rather than paying for it twice.
    """
    if current is None:
        current = await document_tag_ids(db, document_id)
    want_new = [t for t in (add or []) if t.id not in current]
    drop = (remove_ids or set()) & current

    if want_new:
        await db.execute(insert(document_tags), [
            {"document_id": document_id, "tag_id": t.id, "created_by": user.id}
            for t in want_new
        ])
    if drop:
        await db.execute(delete(document_tags).where(
            document_tags.c.document_id == document_id,
            document_tags.c.tag_id.in_(drop),
        ))
    return len(want_new), len(drop)


async def set_document_tags(
    db: AsyncSession, *, user: User, document_id: uuid.UUID, names: list[str]
) -> list[Tag]:
    """Replace a document's tags with exactly `names`, creating any that are new."""
    wanted = normalize_many(names)
    tags = [await get_or_create(db, tenant_id=user.tenant_id, name=n, created_by=user.id)
            for n in wanted]
    keep = {t.id for t in tags}
    current = await document_tag_ids(db, document_id)
    await apply_changes(db, user=user, document_id=document_id,
                        add=tags, remove_ids=current - keep, current=current)
    return tags


async def updatable_document_ids(
    db: AsyncSession, user: User, document_ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    """Of these documents, the ones the caller may modify — resolved in one query."""
    if not document_ids:
        return set()
    group_ids = await permissions.user_group_ids(db, user.id)
    cond = permissions.permitted_documents_condition(user, group_ids, PERM_UPDATE)
    rows = await db.execute(
        select(Document.id).where(Document.id.in_(document_ids), cond))
    return {r[0] for r in rows.all()}


# ------------------------------------------------------- taxonomy management
async def rename(db: AsyncSession, *, tag: Tag, new_name: str) -> Tag:
    cleaned = normalize(new_name)
    clash = await find_by_name(db, tag.tenant_id, cleaned)
    if clash is not None and clash.id != tag.id:
        raise TagError(f"A tag named '{clash.name}' already exists — merge into it instead")
    tag.name = cleaned
    await db.flush()
    return tag


async def merge(db: AsyncSession, *, source: Tag, target: Tag) -> int:
    """Move `source`'s documents onto `target` and delete `source`.

    Returns how many documents moved. Documents already carrying both keep a
    single link: they're skipped here and their leftover source link disappears
    with the source tag's ON DELETE CASCADE.
    """
    if source.id == target.id:
        raise TagError("Cannot merge a tag into itself")

    already = select(document_tags.c.document_id).where(document_tags.c.tag_id == target.id)
    moved = await db.execute(
        document_tags.update()
        .where(document_tags.c.tag_id == source.id,
               ~document_tags.c.document_id.in_(already))
        .values(tag_id=target.id)
    )
    await db.delete(source)
    await db.flush()
    return int(moved.rowcount or 0)
