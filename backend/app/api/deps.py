"""API dependencies: authentication, role gating, tenant scoping."""
from __future__ import annotations

import uuid

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import decode_access_token
from app.models.constants import ROLE_ADMIN, ROLE_REVIEWER
from app.models.tenant import User


async def get_current_user(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        payload = decode_access_token(token)
    except Exception:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")

    user = await db.get(User, uuid.UUID(payload["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or inactive")
    # Defense in depth: token tenant must match the user's tenant.
    if str(user.tenant_id) != payload.get("tenant_id"):
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
