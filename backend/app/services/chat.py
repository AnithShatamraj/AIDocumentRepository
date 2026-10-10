"""AI Research Assistant: grounded RAG with citations, refusal, streaming, audit.

Groundedness is a feature with acceptance criteria: if retrieval yields nothing
adequate from the caller's authorized corpus, we refuse explicitly rather than
hallucinate.

Three things make an answer here trustworthy rather than merely fluent:

- **The retrieval query is not the raw message.** A follow-up like "what about
  the second one?" embeds to noise, so it is resolved against the conversation
  into a standalone query first.
- **Coverage is reported.** Every answer says how much was searched and how
  much of it was actually used, so a thin answer looks thin instead of
  authoritative.
- **The answer is read back.** Each sentence is checked against the passages it
  cites, and anything unsupported is surfaced with the answer rather than left
  for the reader to catch.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import AsyncIterator

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import prompts
from app.ai.registry import get_llm, is_offline
from app.ai.types import Message as LLMMessage
from app.core.config import settings
from app.models.chat import Conversation, Message, QAAuditLog
from app.models.document import Document
from app.models.tenant import User
from app.services import answer_check, permissions, search

# The offline stub's hashing embeddings compress cosine similarity into a narrow,
# noisy band, so it needs a lower grounding floor than real embeddings (and can't
# separate every case — real embeddings do). Favor answering grounded queries.
OFFLINE_SIM_FLOOR = 0.10
REFUSAL = ("I couldn't find anything in the documents you're authorized to access "
           "that answers this question. Try rephrasing, or upload a relevant document.")
# How much conversation the rewriter sees. Enough to resolve a reference,
# short enough that an old topic doesn't drag the new query off course.
REWRITE_HISTORY_TURNS = 6


async def _tokens_today(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    total = await db.execute(
        select(func.coalesce(func.sum(QAAuditLog.tokens), 0)).where(
            QAAuditLog.tenant_id == tenant_id, QAAuditLog.created_at >= since)
    )
    return int(total.scalar() or 0)


def _est_tokens(s: str) -> int:
    try:
        import tiktoken

        return len(tiktoken.get_encoding("cl100k_base").encode(s))
    except Exception:  # noqa: BLE001
        return max(1, len(s) // 4)


async def _history(db: AsyncSession, conversation: Conversation, limit: int = 10) -> list[Message]:
    return list((await db.execute(
        select(Message).where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at).limit(limit))).scalars().all())


async def _readable_document_count(db: AsyncSession, user: User) -> int:
    group_ids = await permissions.user_group_ids(db, user.id)
    cond = permissions.readable_documents_condition(user, group_ids)
    n = await db.execute(select(func.count()).select_from(Document).where(cond))
    return int(n.scalar() or 0)


async def standalone_question(history: list[Message], question: str) -> str:
    """Resolve a follow-up into a query that stands on its own.

    Retrieval embeds this, not the raw message. "What about the second one?"
    carries almost no signal by itself; the conversation is what makes it
    mean something. Falls back to the raw question whenever the rewrite is
    unavailable or looks degenerate — a bad rewrite is worse than none.
    """
    if not history or is_offline() or not question.strip():
        return question

    transcript = "\n".join(
        f"{m.role}: {m.content[:600]}" for m in history[-REWRITE_HISTORY_TURNS:])
    messages = [
        LLMMessage("system", prompts.REWRITE_SYSTEM),
        LLMMessage("user", prompts.render(
            prompts.REWRITE_USER, history=transcript, question=question)),
    ]
    try:
        raw, _ = await asyncio.to_thread(
            get_llm().complete_json, messages, model=settings.llm_chat_model)
    except Exception:  # noqa: BLE001
        return question

    rewritten = str(raw.get("query") or "").strip()
    # Guard against a rewrite that dropped the question or ran away with it.
    if not rewritten or len(rewritten) > 500:
        return question
    return rewritten


async def readable_history(db: AsyncSession, user: User, conversation: Conversation) -> list[dict]:
    """A conversation's messages, with citations re-checked against permissions.

    Permissions are resolved at read time everywhere else in this system, and
    stored citations are no exception: access can be revoked after an answer is
    written, and the citation still holds a document name, page and snippet.
    Redactions are counted rather than hidden, so the reader can tell the
    history was trimmed instead of watching it quietly shrink.
    """
    msgs = (await db.execute(
        select(Message).where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at))).scalars().all()

    wanted: set[uuid.UUID] = set()
    for m in msgs:
        for c in m.citations or []:
            try:
                wanted.add(uuid.UUID(str(c.get("document_id"))))
            except (ValueError, TypeError):
                continue

    readable: set[str] = set()
    if wanted:
        group_ids = await permissions.user_group_ids(db, user.id)
        cond = permissions.readable_documents_condition(user, group_ids)
        rows = await db.execute(select(Document.id).where(Document.id.in_(wanted), cond))
        readable = {str(r[0]) for r in rows.all()}

    out = []
    for m in msgs:
        original = m.citations or []
        kept = [c for c in original if str(c.get("document_id")) in readable]
        out.append({
            "id": str(m.id), "role": m.role, "content": m.content,
            "citations": kept, "citations_redacted": len(original) - len(kept),
            "meta": m.meta or {},
        })
    return out


def _citation(index: int, hit: search.Hit) -> dict:
    return {
        "index": index,
        "chunk_id": str(hit.chunk_id),
        "document_id": str(hit.document_id),
        "document_name": hit.document_name,
        "page": hit.page,
        "anchor": hit.anchor,
        "section_title": hit.section_title,
        # Same shape the PDF viewer's highlight takes, so a citation can open
        # the document at the exact block it came from rather than page 1.
        "bbox": hit.bbox,
        "snippet": hit.content[:280],
    }


async def stream_answer(
    db: AsyncSession, user: User, conversation: Conversation, question: str, k: int = 8
) -> AsyncIterator[dict]:
    # --- Per-tenant token budget (chat is the cost hot spot) ---
    if await _tokens_today(db, user.tenant_id) >= settings.chat_max_tokens_per_day:
        msg = "Daily AI token budget for your organization has been reached. Try again later."
        yield {"type": "meta", "citations": [], "coverage": None}
        yield {"type": "delta", "text": msg}
        await _persist(db, user, conversation, question, msg, [], [], refused=True, tokens=0)
        yield {"type": "done", "refused": True}
        return

    history = await _history(db, conversation)
    query = await standalone_question(history, question)
    searched = await _readable_document_count(db, user)

    # --- Refusal on insufficient grounding ---
    # Gate on raw vector cosine similarity (RRF fused scores aren't an absolute
    # relevance measure); refuse when the best chunk isn't similar enough.
    vec_hits = await search.vector_search(db, user, query, k=k)
    top_sim = vec_hits[0].score if vec_hits else 0.0
    floor = OFFLINE_SIM_FLOOR if is_offline() else settings.rag_min_similarity
    if not vec_hits or top_sim < floor:
        coverage = {"documents_searched": searched, "documents_cited": 0,
                    "passages_used": 0, "rewritten_query": query if query != question else None}
        yield {"type": "meta", "citations": [], "coverage": coverage}
        yield {"type": "delta", "text": REFUSAL}
        await _persist(db, user, conversation, question, REFUSAL, [], [],
                       refused=True, tokens=0, coverage=coverage, query=query)
        yield {"type": "done", "refused": True, "coverage": coverage}
        return

    # Fuse vector + keyword for final context ordering.
    hits = await search.hybrid_search(db, user, query, k=k)
    adequate = hits[:k] or vec_hits[:k]

    citations, context_blocks = [], []
    for i, h in enumerate(adequate, start=1):
        loc = f"p.{h.page}" if h.page else (h.anchor or "")
        context_blocks.append(f"[{i}] (from \"{h.document_name}\" {loc})\n{h.content}")
        citations.append(_citation(i, h))
    context = "\n\n".join(context_blocks)

    coverage = {
        "documents_searched": searched,
        "documents_cited": len({c["document_id"] for c in citations}),
        "passages_used": len(citations),
        "rewritten_query": query if query != question else None,
    }

    llm_messages = [LLMMessage("system", prompts.render(prompts.CHAT_SYSTEM, context=context))]
    for m in history:
        llm_messages.append(LLMMessage(m.role, m.content))
    llm_messages.append(LLMMessage("user", question))

    yield {"type": "meta", "citations": citations, "coverage": coverage}

    llm = get_llm()
    answer_parts: list[str] = []
    async for delta in llm.astream(llm_messages, model=settings.llm_chat_model):
        answer_parts.append(delta)
        yield {"type": "delta", "text": delta}
    answer = "".join(answer_parts)

    # Read the answer back against the passages it cites. This costs one extra
    # call, which is the price of the difference between "sounds right" and
    # "is supported".
    check = await answer_check.check(answer, context, len(citations))
    yield {"type": "check", **check}

    tokens = _est_tokens(context + question + answer)
    await _persist(
        db, user, conversation, question, answer, citations,
        [c["chunk_id"] for c in citations], refused=False, tokens=tokens,
        coverage=coverage, check=check, query=query,
    )
    yield {"type": "done", "refused": False, "citations": citations,
           "coverage": coverage, "check": check}


async def _persist(db, user, conversation, question, answer, citations, chunk_ids, *,
                   refused, tokens, coverage=None, check=None, query=None):
    db.add(Message(conversation_id=conversation.id, role="user", content=question, citations=[]))
    assistant = Message(
        conversation_id=conversation.id, role="assistant", content=answer,
        citations=citations,
        # Coverage and the check travel with the message so reopening a
        # conversation shows the same caveats it was answered with, rather
        # than a bare, more confident-looking answer.
        meta={"coverage": coverage or {}, "check": check or {}},
    )
    db.add(assistant)
    await db.flush()
    db.add(QAAuditLog(
        tenant_id=user.tenant_id, user_id=user.id, conversation_id=conversation.id,
        message_id=assistant.id, question=question, standalone_question=query,
        retrieved_chunk_ids=chunk_ids, answer=answer, refused=refused,
        model_version=settings.llm_chat_model, prompt_version=prompts.PROMPT_VERSIONS["chat"],
        tokens=tokens, coverage=coverage or {},
        unsupported_claims=len((check or {}).get("unsupported") or []),
    ))
    if conversation.title == "New conversation":
        conversation.title = question[:80]
    await db.commit()
