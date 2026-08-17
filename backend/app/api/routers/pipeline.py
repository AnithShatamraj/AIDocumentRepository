"""Pipeline status + live SSE updates (Redis pub/sub -> SSE)."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

import uuid

from app.api.deps import get_current_user, get_tenant_db
from app.core.mgmt_db import get_mgmt_db
from app.core.redis import doc_channel, get_async_redis
from app.core.security import decode_access_token
from app.core.tenant_db import get_tenant_session
from app.models.document import Document
from app.models.mgmt import Tenant as MgmtTenant
from app.models.pipeline import PipelineRun, PipelineStage
from app.models.tenant import User
from app.services import permissions

router = APIRouter(prefix="/documents/{document_id}", tags=["pipeline"])
STREAM_MAX_SECONDS = 900  # recycle SSE streams so shutdowns/drains aren't blocked


async def _latest_run(db, document_id):
    return (await db.execute(
        select(PipelineRun).where(PipelineRun.document_id == document_id)
        .order_by(PipelineRun.created_at.desc()).limit(1)
    )).scalars().first()


@router.get("/pipeline")
async def pipeline_status(document_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_tenant_db)):
    doc = await db.get(Document, document_id)
    if not doc or not await permissions.can_read(db, user, doc.id):
        raise HTTPException(404, "Document not found")
    run = await _latest_run(db, doc.id)
    if not run:
        return {"document_id": document_id, "status": doc.processing_status, "run": None, "stages": []}
    stages = (await db.execute(
        select(PipelineStage).where(PipelineStage.run_id == run.id).order_by(PipelineStage.ordinal)
    )).scalars().all()
    return {
        "document_id": document_id, "status": doc.processing_status,
        "run": {"id": str(run.id), "status": run.status, "total_cost_usd": run.total_cost_usd,
                "total_tokens": run.total_tokens,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None},
        "stages": [{"name": s.name, "ordinal": s.ordinal, "status": s.status, "latency_ms": s.latency_ms,
                    "error_message": s.error_message, "model_version": s.model_version,
                    "cost_usd": s.cost_usd, "output": s.output} for s in stages],
    }


@router.get("/events")
async def pipeline_events(document_id: str, token: str = Query(...), mgmt_db: AsyncSession = Depends(get_mgmt_db)):
    # EventSource can't set headers, so auth is via query token -- meaning
    # this endpoint can't use the get_current_tenant/get_tenant_db dependency
    # chain (that reads the Authorization header). Resolve the tenant by hand
    # from the same token instead, same ordering: tenant first, then a
    # session on that tenant's own database.
    try:
        payload = decode_access_token(token)
    except Exception:
        raise HTTPException(401, "Invalid token")
    tenant = await mgmt_db.get(MgmtTenant, uuid.UUID(payload["tenant_id"]))
    if tenant is None or not tenant.is_active:
        raise HTTPException(401, "Invalid token")
    async for adb in get_tenant_session(tenant):
        user = await adb.get(User, payload["sub"])
        if not user or not await permissions.can_read(adb, user, document_id):
            raise HTTPException(404, "Document not found")

    async def gen():
        r = get_async_redis()
        pubsub = r.pubsub()
        await pubsub.subscribe(doc_channel(document_id))
        # Bounded lifetime: an unbounded stream blocks graceful shutdown (uvicorn
        # reloads, k8s pod drains). EventSource reconnects automatically.
        deadline = asyncio.get_event_loop().time() + STREAM_MAX_SECONDS
        try:
            while asyncio.get_event_loop().time() < deadline:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15)
                if msg and msg.get("data"):
                    yield {"event": "stage", "data": msg["data"]}
                else:
                    yield {"event": "ping", "data": json.dumps({"ts": "keepalive"})}
                await asyncio.sleep(0.1)
            yield {"event": "reconnect", "data": json.dumps({"reason": "stream_recycled"})}
        finally:
            await pubsub.unsubscribe(doc_channel(document_id))
            await pubsub.close()

    return EventSourceResponse(gen())
