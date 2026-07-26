"""AI Research Assistant — streaming RAG chat over SSE + conversation history."""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.api.deps import get_current_user
from app.core.db import AsyncSessionLocal, get_db
from app.models.chat import Conversation, Message
from app.models.tenant import User
from app.schemas import ChatRequest
from app.services import chat

router = APIRouter(tags=["chat"])


@router.get("/conversations")
async def list_conversations(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(Conversation).where(Conversation.user_id == user.id).order_by(Conversation.updated_at.desc())
    )).scalars().all()
    return [{"id": str(c.id), "title": c.title, "updated_at": c.updated_at.isoformat()} for c in rows]


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    conv = await db.get(Conversation, conversation_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")
    msgs = (await db.execute(
        select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at)
    )).scalars().all()
    return {"id": str(conv.id), "title": conv.title,
            "messages": [{"role": m.role, "content": m.content, "citations": m.citations} for m in msgs]}


@router.post("/chat")
async def chat_stream(body: ChatRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    # Resolve or create the conversation up front (owned by caller).
    if body.conversation_id:
        conv = await db.get(Conversation, body.conversation_id)
        if not conv or conv.user_id != user.id:
            raise HTTPException(404, "Conversation not found")
    else:
        conv = Conversation(tenant_id=user.tenant_id, user_id=user.id, title="New conversation")
        db.add(conv)
        await db.commit()
        await db.refresh(conv)
    conv_id = conv.id
    user_id = user.id

    async def event_gen():
        # Fresh session for the streaming lifetime.
        async with AsyncSessionLocal() as sdb:
            u = await sdb.get(User, user_id)
            c = await sdb.get(Conversation, conv_id)
            yield {"event": "start", "data": json.dumps({"conversation_id": str(conv_id)})}
            async for ev in chat.stream_answer(sdb, u, c, body.question):
                yield {"event": ev["type"], "data": json.dumps(ev)}

    return EventSourceResponse(event_gen())
