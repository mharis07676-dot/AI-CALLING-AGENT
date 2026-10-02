from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthContext, get_current_auth
from app.db.session import get_db
from app.schemas import LeadCreate, LeadOut, LeadUpdate
from app.services import LeadService

router = APIRouter(prefix="/leads", tags=["leads"])


@router.get("/", response_model=list[LeadOut])
async def list_leads(
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list:
    return await LeadService(db, auth.tenant_id).list()


@router.post("/", response_model=LeadOut, status_code=201)
async def create_lead(
    payload: LeadCreate,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LeadOut:
    return await LeadService(db, auth.tenant_id).create(payload)


@router.patch("/{lead_id}", response_model=LeadOut)
async def update_lead(
    lead_id: UUID,
    payload: LeadUpdate,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LeadOut:
    lead = await LeadService(db, auth.tenant_id).update(lead_id, payload)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return lead
