"""Semantic / keyword / hybrid retrieval — all permission-aware by JOIN.

Every retrieval path filters through `readable_documents_condition`, so a user
can never receive a chunk from a document they can't read, and revocation is
effective on the very next query (no reindexing).
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.registry import get_embedder
from app.core.config import settings
from app.models.content import Chunk, Embedding
from app.models.document import Document
from app.models.tenant import User
from app.services import permissions


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


async def vector_search(db: AsyncSession, user: User, query: str, k: int = 10) -> list[Hit]:
    qvec = await _embed_query(query)
    group_ids = await permissions.user_group_ids(db, user.id)
    cond = permissions.readable_documents_condition(user, group_ids)
    distance = Embedding.vector.cosine_distance(qvec).label("distance")

    await _enable_iterative_scan(db)
    stmt = (
        select(Chunk, Document.name, distance)
        .join(Embedding, Embedding.chunk_id == Chunk.id)
        .join(Document, Document.id == Chunk.document_id)
        .where(cond, Chunk.document_version == Document.current_version)
        .order_by(distance)
        .limit(k)
    )
    rows = await db.execute(stmt)
    hits = []
    for chunk, doc_name, dist in rows.all():
        hits.append(Hit(
            chunk_id=chunk.id, document_id=chunk.document_id, document_name=doc_name,
            content=chunk.content, page=chunk.page, anchor=chunk.anchor,
            section_title=chunk.section_title, score=float(1.0 - dist),
        ))
    return hits


async def keyword_search(db: AsyncSession, user: User, query: str, k: int = 10) -> list[Hit]:
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
    rows = await db.execute(stmt)
    return [
        Hit(chunk_id=c.id, document_id=c.document_id, document_name=name, content=c.content,
            page=c.page, anchor=c.anchor, section_title=c.section_title, score=float(r))
        for c, name, r in rows.all()
    ]


async def hybrid_search(db: AsyncSession, user: User, query: str, k: int = 10) -> list[Hit]:
    """Reciprocal Rank Fusion over vector + keyword result lists."""
    vec, kw = await vector_search(db, user, query, k=k * 2), await keyword_search(db, user, query, k=k * 2)
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
