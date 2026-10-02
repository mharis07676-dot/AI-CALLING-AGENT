from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthContext, get_current_auth
from app.db.session import get_db
from app.models import Purpose
from app.schemas import PropertyCreate, PropertyOut, PropertySearch
from app.services import PropertyService

router = APIRouter(prefix="/properties", tags=["properties"])


@router.post("/", response_model=PropertyOut, status_code=201)
async def create_property(
    payload: PropertyCreate,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PropertyOut:
    return await PropertyService(db, auth.tenant_id).create(payload)


@router.get("/search", response_model=list[PropertyOut])
async def search_properties(
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
    purpose: Purpose | None = None,
    city: str | None = None,
    area: str | None = None,
    property_type: str | None = None,
    size_marla: float | None = None,
    max_price: int | None = None,
    min_price: int | None = None,
    bedrooms: int | None = None,
    limit: int = Query(default=5, ge=1, le=20),
) -> list:
    filters = PropertySearch(
        purpose=purpose,
        city=city,
        area=area,
        property_type=property_type,
        size_marla=size_marla,
        max_price=max_price,
        min_price=min_price,
        bedrooms=bedrooms,
        limit=limit,
    )
    return await PropertyService(db, auth.tenant_id).search(filters)


@router.get("/{property_id}", response_model=PropertyOut)
async def get_property(
    property_id: UUID,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PropertyOut:
    prop = await PropertyService(db, auth.tenant_id).get(property_id)
    if prop is None:
        raise HTTPException(status_code=404, detail="Property not found")
    return prop
