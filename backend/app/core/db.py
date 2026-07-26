"""Database engines and session factories.

Two engines on purpose:
  * async (asyncpg) for FastAPI request handlers,
  * sync  (psycopg2) for Celery workers, which run plain blocking code.

Both point at the same Postgres. Models are shared.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator, Generator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


# --- Async (API) ---
async_engine = create_async_engine(
    settings.database_url_async,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    future=True,
)
AsyncSessionLocal = async_sessionmaker(
    async_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)

# --- Sync (Celery) ---
sync_engine = create_engine(
    settings.database_url_sync, pool_pre_ping=True, pool_size=10, max_overflow=20, future=True
)
SyncSessionLocal = sessionmaker(bind=sync_engine, autoflush=False, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an async session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


def get_sync_db() -> Generator[Session, None, None]:
    """Context-manager style sync session for Celery tasks."""
    session = SyncSessionLocal()
    try:
        yield session
    finally:
        session.close()
