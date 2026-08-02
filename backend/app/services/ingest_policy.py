"""Upload-time policy: unique names, and what to do about duplicate content.

Duplicate handling is deliberately conservative about disclosure. Matching a
content hash tells us a document exists — but saying so to someone who has no
right to read it leaks its existence (and auto-granting access would hand out
someone else's document without the owner's consent). So:

  * duplicate the uploader **can already read**  -> link to it, no reprocessing
  * duplicate they **cannot** read               -> silently give them their own
    document; derived artifacts are cloned server-side so nothing is re-paid for,
    and the other document is never mentioned

`allow_duplicate_documents` (admin setting) decides between linking and cloning
for the readable case.
"""
from __future__ import annotations

import re
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.content import Chunk, Classification, Embedding, FieldValue, Summary
from app.models.document import Document, DocumentVersion
from app.models.ops import AIConfig
from app.models.tenant import User
from app.services import permissions

_SUFFIX_RE = re.compile(r"^(?P<base>.*?)(?: \((?P<n>\d+)\))?$")


async def allow_duplicates(db: AsyncSession, tenant_id: uuid.UUID) -> bool:
    cfg = (await db.execute(select(AIConfig).where(AIConfig.tenant_id == tenant_id))).scalars().first()
    if not cfg:
        return True
    return bool((cfg.processing_config or {}).get("allow_duplicate_documents", True))


async def name_taken(db: AsyncSession, tenant_id: uuid.UUID, name: str,
                     exclude_id: uuid.UUID | None = None) -> bool:
    stmt = select(Document.id).where(
        Document.tenant_id == tenant_id, Document.is_deleted.is_(False),
        func.lower(Document.name) == name.strip().lower())
    if exclude_id:
        stmt = stmt.where(Document.id != exclude_id)
    return (await db.execute(stmt.limit(1))).first() is not None


async def suggest_name(db: AsyncSession, tenant_id: uuid.UUID, name: str) -> str:
    """'Lease.pdf' -> 'Lease (2).pdf' style suggestion that is actually free."""
    stem, dot, ext = name.rpartition(".")
    base_name = stem if dot else name
    suffix = f".{ext}" if dot else ""
    m = _SUFFIX_RE.match(base_name)
    base = (m.group("base") if m else base_name).rstrip()
    for n in range(2, 100):
        candidate = f"{base} ({n}){suffix}"
        if not await name_taken(db, tenant_id, candidate):
            return candidate
    return f"{base} ({uuid.uuid4().hex[:6]}){suffix}"


async def find_duplicate(db: AsyncSession, user: User, content_hash: str) -> tuple[Document | None, bool]:
    """Return (existing document with this content, caller_can_read)."""
    if not content_hash:
        return None, False
    dup = (await db.execute(
        select(Document).where(
            Document.tenant_id == user.tenant_id,
            Document.content_hash == content_hash,
            Document.is_deleted.is_(False))
        .order_by(Document.created_at).limit(1)
    )).scalars().first()
    if dup is None:
        return None, False
    return dup, await permissions.can_read(db, user, dup.id)


async def clone_derived_artifacts(db: AsyncSession, source: Document, target: Document) -> dict:
    """Copy a processed document's derived data onto an identical new document.

    Chunks and embeddings are copied rather than shared: retrieval resolves ACLs
    by joining chunk -> document, so each document must own its rows for
    permissions to stay independent. Nothing here calls a model, so the clone
    costs no tokens — only the vectors' storage.
    """
    counts = {"chunks": 0, "field_values": 0, "summaries": 0, "classifications": 0}

    src_version = (await db.execute(
        select(DocumentVersion).where(DocumentVersion.document_id == source.id,
                                      DocumentVersion.version == source.current_version)
    )).scalars().first()
    tgt_version = (await db.execute(
        select(DocumentVersion).where(DocumentVersion.document_id == target.id,
                                      DocumentVersion.version == target.current_version)
    )).scalars().first()
    if src_version and tgt_version:
        tgt_version.extracted_text = src_version.extracted_text
        tgt_version.understanding = src_version.understanding

    chunks = (await db.execute(
        select(Chunk).where(Chunk.document_id == source.id,
                            Chunk.document_version == source.current_version)
    )).scalars().all()
    embeddings = {e.chunk_id: e for e in (await db.execute(
        select(Embedding).where(Embedding.document_id == source.id))).scalars().all()}
    for c in chunks:
        new_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{target.id}:{target.current_version}:{c.ordinal}")
        db.add(Chunk(id=new_id, tenant_id=target.tenant_id, document_id=target.id,
                     document_version=target.current_version, ordinal=c.ordinal, content=c.content,
                     page=c.page, anchor=c.anchor, section_title=c.section_title, bbox=c.bbox))
        src_emb = embeddings.get(c.id)
        if src_emb is not None:
            db.add(Embedding(tenant_id=target.tenant_id, chunk_id=new_id, document_id=target.id,
                             vector=src_emb.vector, embedding_model=src_emb.embedding_model,
                             embedding_dim=src_emb.embedding_dim))
        counts["chunks"] += 1

    summary = (await db.execute(
        select(Summary).where(Summary.document_id == source.id,
                              Summary.document_version == source.current_version)
    )).scalars().first()
    if summary:
        db.add(Summary(tenant_id=target.tenant_id, document_id=target.id,
                       document_version=target.current_version,
                       executive_summary=summary.executive_summary, highlights=summary.highlights,
                       model_version=summary.model_version, prompt_version=summary.prompt_version))
        counts["summaries"] = 1

    for cl in (await db.execute(
        select(Classification).where(Classification.document_id == source.id))).scalars().all():
        db.add(Classification(tenant_id=target.tenant_id, document_id=target.id,
                              document_type_id=cl.document_type_id, label=cl.label,
                              confidence=cl.confidence, is_override=cl.is_override, source=cl.source,
                              model_version=cl.model_version, prompt_version=cl.prompt_version))
        counts["classifications"] += 1

    # Field values are a tree: copy parents before children so parent_id remaps.
    values = (await db.execute(
        select(FieldValue).where(FieldValue.document_id == source.id,
                                 FieldValue.document_version == source.current_version)
        .order_by(FieldValue.field_path)
    )).scalars().all()
    id_map: dict[uuid.UUID, uuid.UUID] = {}
    for v in sorted(values, key=lambda x: len(x.field_path)):
        new_id = uuid.uuid5(uuid.NAMESPACE_URL,
                            f"{target.id}:{target.current_version}:{v.field_path}")
        id_map[v.id] = new_id
        db.add(FieldValue(
            id=new_id, tenant_id=target.tenant_id, document_id=target.id,
            document_version=target.current_version, document_type_id=v.document_type_id,
            parent_id=id_map.get(v.parent_id) if v.parent_id else None,
            field_key=v.field_key, field_path=v.field_path, field_name=v.field_name,
            node_kind=v.node_kind, ordinal=v.ordinal, data_type=v.data_type,
            raw_value=v.raw_value, value_type=v.value_type, value_text=v.value_text,
            value_number=v.value_number, value_date=v.value_date, value_datetime=v.value_datetime,
            value_time=v.value_time, value_boolean=v.value_boolean, value_currency=v.value_currency,
            confidence=v.confidence, source_page=v.source_page, source_anchor=v.source_anchor,
            source_text=v.source_text, source_bbox=v.source_bbox, review_status=v.review_status,
            model_version=v.model_version, prompt_version=v.prompt_version,
            schema_version=v.schema_version, is_discovered=v.is_discovered))
        counts["field_values"] += 1

    # Type assignments (multi-label) come across too.
    from app.models.document import document_type_links

    links = (await db.execute(
        select(document_type_links.c.document_type_id).where(
            document_type_links.c.document_id == source.id))).all()
    for (type_id,) in links:
        await db.execute(document_type_links.insert().values(
            document_id=target.id, document_type_id=type_id))

    target.processing_status = source.processing_status
    target.duplicate_of_id = source.id
    return counts
