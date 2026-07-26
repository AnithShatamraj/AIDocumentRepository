"""Redis clients — Celery broker lives here too, plus pub/sub for SSE fan-out.

One channel per document (`doc:events:{document_id}`) carries pipeline status
events; the API subscribes and relays them to the browser over SSE.
"""
from __future__ import annotations

import json

import redis
import redis.asyncio as aioredis

from app.core.config import settings

# Sync client for Celery tasks to publish events.
_sync_client: redis.Redis | None = None
# Async client for the API to subscribe.
_async_client: aioredis.Redis | None = None


def get_sync_redis() -> redis.Redis:
    global _sync_client
    if _sync_client is None:
        _sync_client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    return _sync_client


def get_async_redis() -> aioredis.Redis:
    global _async_client
    if _async_client is None:
        _async_client = aioredis.Redis.from_url(settings.redis_url, decode_responses=True)
    return _async_client


def doc_channel(document_id: str) -> str:
    return f"doc:events:{document_id}"


def publish_doc_event(document_id: str, event: dict) -> None:
    """Publish a pipeline/status event for a document (called from Celery)."""
    client = get_sync_redis()
    client.publish(doc_channel(str(document_id)), json.dumps(event, default=str))
