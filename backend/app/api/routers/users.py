"""User & group management (admin-gated)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_tenant_db, require_admin
from app.core.security import hash_password
from app.models.constants import ROLES
from app.models.tenant import Group, User
from app.schemas import GroupCreate, GroupOut, UserCreate, UserOut

router = APIRouter(tags=["users"])


@router.get("/users", response_model=list[UserOut])
async def list_users(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_tenant_db)):
    rows = await db.execute(select(User).where(User.tenant_id == user.tenant_id).order_by(User.email))
    return [UserOut.model_validate(u) for u in rows.scalars().all()]


@router.post("/users", response_model=UserOut, status_code=201)
async def create_user(body: UserCreate, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_tenant_db)):
    if body.role not in ROLES:
        raise HTTPException(400, f"role must be one of {ROLES}")
    exists = (await db.execute(
        select(User).where(User.tenant_id == admin.tenant_id, User.email == body.email.lower())
    )).scalars().first()
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already exists")
    u = User(tenant_id=admin.tenant_id, email=body.email.lower(), full_name=body.full_name,
             hashed_password=hash_password(body.password), role=body.role)
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return UserOut.model_validate(u)


@router.post("/users/{user_id}/deactivate", response_model=UserOut)
async def deactivate_user(user_id: str, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_tenant_db)):
    u = await db.get(User, user_id)
    if not u or u.tenant_id != admin.tenant_id:
        raise HTTPException(404, "User not found")
    u.is_active = False
    await db.commit()
    return UserOut.model_validate(u)


@router.get("/groups", response_model=list[GroupOut])
async def list_groups(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_tenant_db)):
    rows = await db.execute(select(Group).where(Group.tenant_id == user.tenant_id).order_by(Group.name))
    return [GroupOut.model_validate(g) for g in rows.scalars().all()]


@router.post("/groups", response_model=GroupOut, status_code=201)
async def create_group(body: GroupCreate, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_tenant_db)):
    g = Group(tenant_id=admin.tenant_id, name=body.name, description=body.description)
    if body.member_ids:
        members = (await db.execute(
            select(User).where(User.tenant_id == admin.tenant_id, User.id.in_(body.member_ids))
        )).scalars().all()
        g.members = members
    db.add(g)
    await db.commit()
    await db.refresh(g)
    return GroupOut.model_validate(g)
