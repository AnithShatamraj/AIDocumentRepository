"""Management database engines and session factories.

A second, independent stack from `core/db.py` -- the management database
holds only the tenant registry (connection info, storage location, platform
config), never tenant data. It is always the *one* database every process
knows how to reach without first resolving a tenant, which is exactly why
tenant resolution (auth, task dispatch) starts here before any tenant-specific
engine gets involved -- see `core/tenant_db.py`.

Same two-engine-on-purpose pattern as `db.py`: async (asyncpg) for FastAPI
request handlers, sync (psycopg2) for Celery workers and the CLI.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator, Generator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings


class MgmtBase(DeclarativeBase):
    """Declarative base for management-database-only models (app.models.mgmt).

    Deliberately separate from `core.db.Base`: tables here live in a
    physically different database than every tenant-schema table, so they
    must never share a metadata registry (Alembic autogenerate/create_all
    would otherwise try to create tenant tables here or vice versa).
    """


# --- Async (API) ---
mgmt_async_engine = create_async_engine(
    settings.mgmt_database_url_async,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    future=True,
)
MgmtAsyncSessionLocal = async_sessionmaker(
    mgmt_async_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)

# --- Sync (Celery / CLI) ---
mgmt_sync_engine = create_engine(
    settings.mgmt_database_url_sync, pool_pre_ping=True, pool_size=5, max_overflow=10, future=True
)
MgmtSyncSessionLocal = sessionmaker(bind=mgmt_sync_engine, autoflush=False, expire_on_commit=False)


async def get_mgmt_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an async session on the management DB."""
    async with MgmtAsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


def get_mgmt_sync_db() -> Generator[Session, None, None]:
    """Context-manager style sync session for Celery tasks / CLI commands."""
    session = MgmtSyncSessionLocal()
    try:
        yield session
    finally:
        session.close()
