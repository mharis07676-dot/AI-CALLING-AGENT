from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthContext, create_access_token, get_current_auth, hash_password, verify_password
from app.db.session import get_db
from app.models import Tenant, User, UserRole
from app.schemas import LoginRequest, TenantCreate, TenantOut, TokenOut, UserCreate, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/tenants", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
async def create_tenant(
    payload: TenantCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Tenant:
    existing = await db.execute(select(Tenant).where(Tenant.slug == payload.slug))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Tenant slug already exists")
    tenant = Tenant(
        name=payload.name,
        slug=payload.slug,
        max_concurrent_calls=payload.max_concurrent_calls,
    )
    db.add(tenant)
    await db.flush()
    return tenant


@router.post("/tenants/{tenant_id}/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    tenant_id: UUID,
    payload: UserCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    tenant = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    if tenant.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    user = User(
        tenant_id=tenant_id,
        email=payload.email.lower(),
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        role=payload.role,
    )
    db.add(user)
    await db.flush()
    return user


@router.post("/login", response_model=TokenOut)
async def login(
    payload: LoginRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenOut:
    tenant_result = await db.execute(select(Tenant).where(Tenant.slug == payload.tenant_slug))
    tenant = tenant_result.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    user_result = await db.execute(
        select(User).where(
            User.tenant_id == tenant.id,
            User.email == payload.email.lower(),
            User.is_active.is_(True),
        )
    )
    user = user_result.scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token(
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role,
        email=user.email,
    )
    return TokenOut(access_token=token)


@router.get("/me", response_model=UserOut)
async def me(
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    result = await db.execute(
        select(User).where(User.id == auth.user_id, User.tenant_id == auth.tenant_id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


# silence unused import warning for timezone if needed later
_ = datetime, timezone, UserRole
