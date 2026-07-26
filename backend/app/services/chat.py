"""AI Research Assistant: grounded RAG with citations, refusal, streaming, audit.

Groundedness is a feature with acceptance criteria: if retrieval yields nothing
adequate from the caller's authorized corpus, we refuse explicitly rather than
hallucinate.
"""
from __future__ import annotations

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
from app.models.tenant import User
from app.services import search

# The offline stub's hashing embeddings compress cosine similarity into a narrow,
# noisy band, so it needs a lower grounding floor than real embeddings (and can't
# separate every case — real embeddings do). Favor answering grounded queries.
OFFLINE_SIM_FLOOR = 0.10
REFUSAL = ("I couldn't find anything in the documents you're authorized to access "
           "that answers this question. Try rephrasing, or upload a relevant document.")


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


async def stream_answer(
    db: AsyncSession, user: User, conversation: Conversation, question: str, k: int = 8
) -> AsyncIterator[dict]:
    # --- Per-tenant token budget (chat is the cost hot spot) ---
    if await _tokens_today(db, user.tenant_id) >= settings.chat_max_tokens_per_day:
        msg = "Daily AI token budget for your organization has been reached. Try again later."
        yield {"type": "meta", "citations": []}
        yield {"type": "delta", "text": msg}
        await _persist(db, user, conversation, question, msg, [], [], refused=True, tokens=0)
        yield {"type": "done", "refused": True}
        return

    # --- Refusal on insufficient grounding ---
    # Gate on raw vector cosine similarity (RRF fused scores aren't an absolute
    # relevance measure); refuse when the best chunk isn't similar enough.
    vec_hits = await search.vector_search(db, user, question, k=k)
    top_sim = vec_hits[0].score if vec_hits else 0.0
    floor = OFFLINE_SIM_FLOOR if is_offline() else settings.rag_min_similarity
    if not vec_hits or top_sim < floor:
        yield {"type": "meta", "citations": []}
        yield {"type": "delta", "text": REFUSAL}
        await _persist(db, user, conversation, question, REFUSAL, [], [], refused=True, tokens=0)
        yield {"type": "done", "refused": True}
        return

    # Fuse vector + keyword for final context ordering.
    hits = await search.hybrid_search(db, user, question, k=k)
    adequate = hits[:k] or vec_hits[:k]

    citations = []
    context_blocks = []
    for i, h in enumerate(adequate, start=1):
        loc = f"p.{h.page}" if h.page else (h.anchor or "")
        context_blocks.append(f"[{i}] (from \"{h.document_name}\" {loc})\n{h.content}")
        citations.append({
            "index": i, "chunk_id": str(h.chunk_id), "document_id": str(h.document_id),
            "document_name": h.document_name, "page": h.page, "anchor": h.anchor,
            "snippet": h.content[:280],
        })
    context = "\n\n".join(context_blocks)

    # Prior turns for follow-up continuity.
    history = (await db.execute(
        select(Message).where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at).limit(10))).scalars().all()

    llm_messages = [LLMMessage("system", prompts.render(prompts.CHAT_SYSTEM, context=context))]
    for m in history:
        llm_messages.append(LLMMessage(m.role, m.content))
    llm_messages.append(LLMMessage("user", question))

    yield {"type": "meta", "citations": citations}

    llm = get_llm()
    answer_parts: list[str] = []
    async for delta in llm.astream(llm_messages, model=settings.llm_chat_model):
        answer_parts.append(delta)
        yield {"type": "delta", "text": delta}
    answer = "".join(answer_parts)

    tokens = _est_tokens(context + question + answer)
    await _persist(
        db, user, conversation, question, answer, citations,
        [c["chunk_id"] for c in citations], refused=False, tokens=tokens,
    )
    yield {"type": "done", "refused": False, "citations": citations}


async def _persist(db, user, conversation, question, answer, citations, chunk_ids, *, refused, tokens):
    db.add(Message(conversation_id=conversation.id, role="user", content=question, citations=[]))
    db.add(Message(conversation_id=conversation.id, role="assistant", content=answer, citations=citations))
    db.add(QAAuditLog(
        tenant_id=user.tenant_id, user_id=user.id, conversation_id=conversation.id,
        question=question, retrieved_chunk_ids=chunk_ids, answer=answer, refused=refused,
        model_version=settings.llm_chat_model, prompt_version=prompts.PROMPT_VERSIONS["chat"], tokens=tokens))
    if conversation.title == "New conversation":
        conversation.title = question[:80]
    await db.commit()
