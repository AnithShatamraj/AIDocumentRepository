"""Regression: the tenant cache in api/deps.py must survive a failed request.

`get_mgmt_db` rolls its session back whenever an endpoint raises (any 4xx
included), and rollback() expires every object in that session. If the request
that loaded the cached Tenant also failed, the cached copy came out expired and
detached, so every request for the next cache TTL hit DetachedInstanceError
(a 500 -- and the frontend treats a failed /auth/me as "logged out").
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.api import deps
from app.core.mgmt_db import MgmtAsyncSessionLocal
from app.core.security import create_access_token
from app.models.mgmt import Tenant

pytestmark = pytest.mark.asyncio


async def test_cached_tenant_is_not_expired_by_a_rolled_back_request():
    async with MgmtAsyncSessionLocal() as s:
        tenant = (await s.execute(select(Tenant).where(Tenant.is_active.is_(True)).limit(1))).scalars().first()
    if tenant is None:
        pytest.skip("no tenant registered in the management database")

    token = create_access_token(user_id=uuid.uuid4(), tenant_id=tenant.id, tenant_slug=tenant.slug,
                                role="administrator")
    deps._tenant_cache.clear()
    try:
        async with MgmtAsyncSessionLocal() as s:
            resolved = await deps.get_current_tenant(f"Bearer {token}", s)
            await s.rollback()  # exactly what get_mgmt_db does when the endpoint raises

        # the request itself, and the *next* one served from the cache, must both be able to read it
        assert resolved.id == tenant.id and resolved.db_name == tenant.db_name
        async with MgmtAsyncSessionLocal() as s2:
            cached = await deps.get_current_tenant(f"Bearer {token}", s2)
        assert cached.id == tenant.id and cached.db_name == tenant.db_name
    finally:
        deps._tenant_cache.clear()
