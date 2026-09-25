"""Semantic / keyword / hybrid retrieval — all permission-aware by JOIN.

Every retrieval path filters through `readable_documents_condition`, so a user
can never receive a chunk from a document they can't read, and revocation is
effective on the very next query (no reindexing).

Any search can be restricted to a set of documents (`document_ids`) — the
foundation the research assistant's *scope* is built on. Scoping is not only a
filter: below `EXACT_SCAN_MAX_DOCUMENTS` it also changes how the vector search
runs, from approximate to exact. See `_use_exact_scan`.
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.registry import get_embedder
from app.core.config import settings
from app.models.content import Chunk, Embedding
from app.models.document import Document
from app.models.tenant import User
from app.services import permissions

# Below this many documents in scope, vector search computes exact distances
# instead of walking the HNSW graph. Document count is the proxy for chunk
# count on purpose: it's free, whereas counting chunks costs a query on every
# search. A few hundred documents is comfortably inside the range where a
# sequential distance scan is both fast and exact.
EXACT_SCAN_MAX_DOCUMENTS = 500


@dataclass
class Hit:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_name: str
    content: str
    page: int | None
    anchor: str | None
    section_title: str | None
    score: float


async def _embed_query(query: str) -> list[float]:
    embedder = get_embedder()
    res = await asyncio.to_thread(embedder.embed, [query], model=settings.embedding_model)
    return res.vectors[0]


async def _enable_iterative_scan(db: AsyncSession) -> None:
    # pgvector >= 0.8: keep HNSW returning >= k rows under selective ACL filters.
    # Guarded so search still works on older pgvector builds lacking the GUC.
    try:
        await db.execute(text("SET LOCAL hnsw.iterative_scan = 'relaxed_order'"))
        await db.execute(text("SET LOCAL hnsw.ef_search = 100"))
    except Exception:  # noqa: BLE001
        await db.rollback()


def _use_exact_scan(document_ids: Sequence[uuid.UUID] | None) -> bool:
    """Should this search compute exact distances rather than walk the index?

    This is a correctness decision before it is a performance one. An HNSW
    traversal under a filter cannot promise it found the true top-k — that's
    what `hnsw.iterative_scan` mitigates, and a mitigation is not a guarantee.
    An assistant that reports "I searched all 12 documents in your scope" has
    to have actually searched them, so a small scope is scanned exhaustively.

    Unscoped search keeps the approximate path: over a whole corpus, exact is
    the wrong trade and no such claim is being made.
    """
    return document_ids is not None and len(document_ids) <= EXACT_SCAN_MAX_DOCUMENTS


async def _set_exact_scan(db: AsyncSession, on: bool) -> bool:
    """Toggle the planner off the HNSW ordering index for this transaction.

    Disabling index scans leaves *bitmap* index scans enabled, so the
    `document_id IN (...)` filter still uses its index — only the ordered walk
    of the vector index is ruled out, which is exactly the part that makes the
    result approximate. Returns False if the GUC could not be set, so the
    caller can report honestly rather than over-claim.
    """
    try:
        await db.execute(text(f"SET LOCAL enable_indexscan = {'off' if on else 'on'}"))
        return True
    except Exception:  # noqa: BLE001
        await db.rollback()
        return False


async def vector_search(
    db: AsyncSession, user: User, query: str, k: int = 10,
    document_ids: Sequence[uuid.UUID] | None = None,
) -> list[Hit]:
    """Embedding similarity over readable chunks, optionally scoped.

    `document_ids=None` means the whole readable corpus; an *empty* list means
    an empty scope and returns nothing — the distinction matters, since
    silently treating "scope resolved to nothing" as "search everything" is how
    a scoped question gets answered from outside its scope.
    """
    if document_ids is not None and len(document_ids) == 0:
        return []

    qvec = await _embed_query(query)
    group_ids = await permissions.user_group_ids(db, user.id)
    cond = permissions.readable_documents_condition(user, group_ids)
    distance = Embedding.vector.cosine_distance(qvec).label("distance")

    exact = _use_exact_scan(document_ids)
    if exact:
        exact = await _set_exact_scan(db, True)
    else:
        await _enable_iterative_scan(db)

    stmt = (
        select(Chunk, Document.name, distance)
        .join(Embedding, Embedding.chunk_id == Chunk.id)
        .join(Document, Document.id == Chunk.document_id)
        .where(cond, Chunk.document_version == Document.current_version)
        .order_by(distance)
        .limit(k)
    )
    if document_ids is not None:
        stmt = stmt.where(Chunk.document_id.in_(document_ids))

    try:
        rows = await db.execute(stmt)
        results = rows.all()
    finally:
        # Restore the planner for anything else running on this session — the
        # setting is transaction-scoped, and a request's session outlives this
        # one query (hybrid_search runs a keyword pass straight after).
        if exact:
            await _set_exact_scan(db, False)

    hits = []
    for chunk, doc_name, dist in results:
        hits.append(Hit(
            chunk_id=chunk.id, document_id=chunk.document_id, document_name=doc_name,
            content=chunk.content, page=chunk.page, anchor=chunk.anchor,
            section_title=chunk.section_title, score=float(1.0 - dist),
        ))
    return hits


async def keyword_search(
    db: AsyncSession, user: User, query: str, k: int = 10,
    document_ids: Sequence[uuid.UUID] | None = None,
) -> list[Hit]:
    if document_ids is not None and len(document_ids) == 0:
        return []

    group_ids = await permissions.user_group_ids(db, user.id)
    cond = permissions.readable_documents_condition(user, group_ids)
    tsv = func.to_tsvector("english", Chunk.content)
    tsq = func.plainto_tsquery("english", query)
    rank = func.ts_rank(tsv, tsq).label("rank")

    stmt = (
        select(Chunk, Document.name, rank)
        .join(Document, Document.id == Chunk.document_id)
        .where(cond, Chunk.document_version == Document.current_version, tsv.op("@@")(tsq))
        .order_by(rank.desc())
        .limit(k)
    )
    if document_ids is not None:
        stmt = stmt.where(Chunk.document_id.in_(document_ids))

    rows = await db.execute(stmt)
    return [
        Hit(chunk_id=c.id, document_id=c.document_id, document_name=name, content=c.content,
            page=c.page, anchor=c.anchor, section_title=c.section_title, score=float(r))
        for c, name, r in rows.all()
    ]


async def hybrid_search(
    db: AsyncSession, user: User, query: str, k: int = 10,
    document_ids: Sequence[uuid.UUID] | None = None,
) -> list[Hit]:
    """Reciprocal Rank Fusion over vector + keyword result lists."""
    vec = await vector_search(db, user, query, k=k * 2, document_ids=document_ids)
    kw = await keyword_search(db, user, query, k=k * 2, document_ids=document_ids)
    C = 60.0
    scored: dict[uuid.UUID, tuple[Hit, float]] = {}
    for rank, hit in enumerate(vec):
        scored[hit.chunk_id] = (hit, scored.get(hit.chunk_id, (hit, 0.0))[1] + 1.0 / (C + rank))
    for rank, hit in enumerate(kw):
        prev = scored.get(hit.chunk_id)
        base = prev[1] if prev else 0.0
        scored[hit.chunk_id] = (prev[0] if prev else hit, base + 1.0 / (C + rank))
    ranked = sorted(scored.values(), key=lambda t: t[1], reverse=True)[:k]
    out = []
    for hit, s in ranked:
        hit.score = round(s, 6)
        out.append(hit)
    return out
