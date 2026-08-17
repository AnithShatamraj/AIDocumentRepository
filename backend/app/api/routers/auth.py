"""Authentication endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import client_ip, get_current_user
from app.core.mgmt_db import get_mgmt_db
from app.core.security import create_access_token, verify_password
from app.core.tenant_db import get_tenant_session
from app.models.constants import AUDIT_LOGIN
from app.models.mgmt import Tenant as MgmtTenant
from app.models.tenant import User
from app.schemas import LoginRequest, TokenResponse, UserOut
from app.services import audit

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, mgmt_db: AsyncSession = Depends(get_mgmt_db)):
    # Resolve which tenant database to even look in BEFORE touching one --
    # this is what fixes the old ordering bug where a DB session opened
    # before tenant identity was known.
    tenant = (await mgmt_db.execute(
        select(MgmtTenant).where(MgmtTenant.slug == body.tenant_slug, MgmtTenant.is_active.is_(True))
    )).scalars().first()
    if tenant is None:
        # Same 401 as a bad password -- don't let login reveal whether a
        # workspace slug exists.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")

    async for db in get_tenant_session(tenant):
        user = (await db.execute(
            select(User).where(User.email == body.email.lower(), User.is_active.is_(True))
        )).scalars().first()
        if user is None or not verify_password(body.password, user.hashed_password):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")

        token = create_access_token(
            user_id=user.id, tenant_id=tenant.id, tenant_slug=tenant.slug, role=user.role)
        await audit.log_event(
            db, tenant_id=user.tenant_id, actor_id=user.id, event_type=AUDIT_LOGIN,
            object_type="user", object_id=user.id, ip_address=client_ip(request))
        return TokenResponse(access_token=token, user=UserOut.model_validate(user))


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return UserOut.model_validate(user)
