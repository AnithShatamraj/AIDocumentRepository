"""Per-tenant database connection routing.

Each tenant has its own dedicated Postgres database (see `models/mgmt.py`
and `app/cli.py::provision_tenant`). This module turns a management-DB
`Tenant` record into a live SQLAlchemy engine/session for THAT tenant's own
database, caching engines per tenant so repeated requests/tasks reuse pooled
connections instead of reconnecting every time.

Mirrors `core/db.py`'s two-engine-on-purpose pattern (async for FastAPI, sync
for Celery/CLI), but keyed per tenant instead of one global engine. Plain
dict caches, not `functools.lru_cache`: engines hold live connection pools
that need `.dispose()` on eviction/rotation, which bare LRU eviction would
leak. Operational note: N tenants x pool_size x replica-count connections
must stay under Postgres's max_connections -- this cache has no eviction
policy yet, which is fine for a modest tenant count but is a real thing to
revisit before scaling past one.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.crypto import decrypt_secret

_async_engines: dict[str, AsyncEngine] = {}
_sync_engines: dict[str, Engine] = {}


def _tenant_url(tenant, *, async_driver: bool) -> str:
    scheme = "postgresql+asyncpg" if async_driver else "postgresql+psycopg2"
    password = decrypt_secret(tenant.db_password_encrypted)
    return (
        f"{scheme}://{tenant.db_role}:{password}"
        f"@{tenant.db_host}:{tenant.db_port}/{tenant.db_name}"
    )


def get_tenant_async_engine(tenant) -> AsyncEngine:
    key = str(tenant.id)
    if key not in _async_engines:
        _async_engines[key] = create_async_engine(
            _tenant_url(tenant, async_driver=True),
            pool_pre_ping=True, pool_size=2, max_overflow=3, future=True,
        )
    return _async_engines[key]


async def get_tenant_session(tenant) -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency-shaped: yields a session on `tenant`'s own database."""
    engine = get_tenant_async_engine(tenant)
    Local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    async with Local() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


def get_tenant_sync_engine(tenant) -> Engine:
    key = str(tenant.id)
    if key not in _sync_engines:
        _sync_engines[key] = create_engine(
            _tenant_url(tenant, async_driver=False),
            pool_pre_ping=True, pool_size=2, max_overflow=3, future=True,
        )
    return _sync_engines[key]


def get_tenant_sync_session(tenant) -> Session:
    """For Celery tasks / CLI scripts. Caller owns the session (close it)."""
    Local = sessionmaker(bind=get_tenant_sync_engine(tenant), autoflush=False, expire_on_commit=False)
    return Local()


def dispose_tenant_engine(tenant_id) -> None:
    """Drop this tenant's pooled connections -- call on password rotation or
    tenant offboarding so stale credentials/connections can't be reused."""
    key = str(tenant_id)
    async_engine = _async_engines.pop(key, None)
    if async_engine is not None:
        try:
            asyncio.get_running_loop().create_task(async_engine.dispose())
        except RuntimeError:
            pass  # no running loop (e.g. called from sync code) -- idle
                  # pooled connections are reaped by the server/idle timeout
    sync_engine = _sync_engines.pop(key, None)
    if sync_engine is not None:
        sync_engine.dispose()
