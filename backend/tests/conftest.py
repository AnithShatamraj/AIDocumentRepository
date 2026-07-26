"""Test fixtures.

Dispose the app's async engine after each test so pooled asyncpg connections are
closed inside the same event loop that created them (pytest-asyncio uses a fresh
loop per test) — avoids "Event loop is closed" teardown errors.
"""
from __future__ import annotations

import pytest

from app.core.db import async_engine


@pytest.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()
