"""API dependencies: authentication, role gating, tenant scoping.

Dependency order matters here: `get_current_tenant` decodes the bearer token
(cheap, no I/O) and resolves the tenant from the MANAGEMENT database --
before any session on a *tenant's own* database opens. `get_tenant_db` then
opens that session, and `get_current_user` looks the user up on it. FastAPI
caches a dependency's result per request, so `get_current_tenant` really
only runs once even though both `get_tenant_db` and `get_current_user`
depend on it -- that's what makes "resolve tenant, then open its database"
a structural guarantee rather than a convention every route has to honor.
"""
from __future__ import annotations

import time
import uuid

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.mgmt_db import get_mgmt_db
from app.core.security import decode_access_token
from app.core.tenant_db import get_tenant_session
from app.models.constants import ROLE_ADMIN, ROLE_REVIEWER
from app.models.mgmt import Tenant as MgmtTenant
from app.models.tenant import User

# Tiny in-process cache so every request doesn't round-trip the management
# database just to re-resolve the same tenant's connection info. A tenant
# deactivated mid-window stays reachable for up to this long -- an accepted
# tradeoff of any TTL cache, not a gap specific to this one.
_TENANT_CACHE_TTL_SECONDS = 60.0
_tenant_cache: dict[str, tuple[float, MgmtTenant]] = {}


def _extract_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    return authorization.split(" ", 1)[1]


def _decode(authorization: str | None) -> dict:
    try:
        return decode_access_token(_extract_bearer(authorization))
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")


async def get_current_tenant(
    authorization: str | None = Header(default=None),
    mgmt_db: AsyncSession = Depends(get_mgmt_db),
) -> MgmtTenant:
    payload = _decode(authorization)
    tenant_id = payload.get("tenant_id", "")

    cached = _tenant_cache.get(tenant_id)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]

    tenant = await mgmt_db.get(MgmtTenant, uuid.UUID(tenant_id))
    if tenant is None or not tenant.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown or inactive tenant")
    _tenant_cache[tenant_id] = (time.monotonic() + _TENANT_CACHE_TTL_SECONDS, tenant)
    return tenant


async def get_tenant_db(tenant: MgmtTenant = Depends(get_current_tenant)) -> AsyncSession:
    async for session in get_tenant_session(tenant):
        yield session


async def get_current_user(
    authorization: str | None = Header(default=None),
    tenant: MgmtTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_tenant_db),
) -> User:
    # Re-decoding here is cheap (no I/O, pure JWT verify) -- simpler than
    # threading the already-decoded payload through request.state.
    payload = _decode(authorization)
    user = await db.get(User, uuid.UUID(payload["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or inactive")
    # Defense in depth: token's tenant must match the resolved tenant.
    if str(user.tenant_id) != str(tenant.id):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Tenant mismatch")
    return user


def require_role(*roles: str):
    async def _dep(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
        return user

    return _dep


require_admin = require_role(ROLE_ADMIN)
require_reviewer = require_role(ROLE_ADMIN, ROLE_REVIEWER)


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")
