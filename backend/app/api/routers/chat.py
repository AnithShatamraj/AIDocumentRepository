"""AI Research Assistant — streaming RAG chat over SSE + conversation history."""
from __future__ import annotations

import datetime as dt
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.api.deps import get_current_tenant, get_current_user, get_tenant_db
from app.core.tenant_db import get_tenant_session
from app.models.chat import Conversation, Message, QAAuditLog
from app.models.mgmt import Tenant as MgmtTenant
from app.models.tenant import User
from app.schemas import ChatRequest, ConversationRename, MessageFeedback
from app.services import chat

router = APIRouter(tags=["chat"])


async def _owned(db: AsyncSession, conversation_id: str, user: User) -> Conversation:
    conv = await db.get(Conversation, conversation_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")
    return conv


@router.get("/conversations", summary="List the caller's chat conversations")
async def list_conversations(
    q: str | None = Query(default=None, description="Substring filter over the title"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Most recently modified first. Each item carries a short `summary` of the
    latest answer and the `documents` its answers cited, for the Research cards."""
    return await chat.conversation_cards(db, user, q=q)


@router.get("/conversations/{conversation_id}", summary="Get one conversation's message history")
async def get_conversation(conversation_id: str, user: User = Depends(get_current_user),
                           db: AsyncSession = Depends(get_tenant_db)):
    """Permissions are re-checked on every read, not just when the answer was
    written. Access can be revoked after the fact, and a stored citation would
    otherwise keep disclosing a document name, page and snippet from a file the
    caller may no longer open. Redacted citations are counted, so the history
    shows that something was removed rather than silently shrinking."""
    conv = await _owned(db, conversation_id, user)
    return {"id": str(conv.id), "title": conv.title,
            "messages": await chat.readable_history(db, user, conv)}


@router.patch("/conversations/{conversation_id}", summary="Rename a conversation")
async def rename_conversation(conversation_id: str, body: ConversationRename,
                              user: User = Depends(get_current_user),
                              db: AsyncSession = Depends(get_tenant_db)):
    conv = await _owned(db, conversation_id, user)
    conv.title = body.title.strip()
    await db.commit()
    return {"id": str(conv.id), "title": conv.title}


@router.delete("/conversations/{conversation_id}", status_code=204, summary="Delete a conversation")
async def delete_conversation(conversation_id: str, user: User = Depends(get_current_user),
                              db: AsyncSession = Depends(get_tenant_db)):
    """Messages go with it (ON DELETE CASCADE). The Q&A audit log does not:
    its conversation_id is SET NULL, so the security record of what was asked
    and retrieved survives a user tidying up their history."""
    conv = await _owned(db, conversation_id, user)
    await db.delete(conv)
    await db.commit()


@router.post("/messages/{message_id}/feedback", summary="Rate an answer")
async def rate_answer(message_id: str, body: MessageFeedback,
                      user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_tenant_db)):
    """Recorded on the Q&A audit row for that answer, which already holds the
    question, the query retrieval used, and the chunks it returned — everything
    needed to act on "this answer was wrong"."""
    msg = await db.get(Message, message_id)
    if not msg:
        raise HTTPException(404, "Message not found")
    await _owned(db, str(msg.conversation_id), user)

    row = (await db.execute(
        select(QAAuditLog).where(QAAuditLog.message_id == msg.id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "No audit record for this message")
    row.feedback = body.vote
    row.feedback_reason = (body.reason or "").strip() or None
    row.feedback_at = dt.datetime.now(dt.timezone.utc)
    await db.commit()
    return {"message_id": str(msg.id), "vote": row.feedback}


@router.post("/chat", summary="Ask a question, get a streamed grounded answer (SSE)")
async def chat_stream(body: ChatRequest, user: User = Depends(get_current_user),
                      tenant: MgmtTenant = Depends(get_current_tenant),
                      db: AsyncSession = Depends(get_tenant_db)):
    """Retrieval-augmented chat over the caller's readable documents. Omit
    `conversation_id` to start a new conversation, or pass an existing one to
    continue it. The response is `text/event-stream`: a `start` event with the
    conversation id, a `meta` event carrying citations and coverage, streamed
    answer tokens, then a `check` event reporting which sentences the cited
    passages actually support."""
    # Resolve or create the conversation up front (owned by caller).
    if body.conversation_id:
        conv = await _owned(db, body.conversation_id, user)
    else:
        conv = Conversation(tenant_id=user.tenant_id, user_id=user.id, title="New conversation")
        db.add(conv)
        await db.commit()
        await db.refresh(conv)
    conv_id = conv.id
    user_id = user.id

    async def event_gen():
        # Fresh session for the streaming lifetime, on the same tenant's database.
        async for sdb in get_tenant_session(tenant):
            u = await sdb.get(User, user_id)
            c = await sdb.get(Conversation, conv_id)
            yield {"event": "start", "data": json.dumps({"conversation_id": str(conv_id)})}
            async for ev in chat.stream_answer(sdb, u, c, body.question):
                yield {"event": ev["type"], "data": json.dumps(ev)}

    return EventSourceResponse(event_gen())
