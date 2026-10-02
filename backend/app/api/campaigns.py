from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthContext, get_current_auth, require_roles
from app.db.session import get_db
from app.models import UserRole
from app.schemas import CampaignCreate, CampaignOut
from app.services import CampaignService

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


@router.get("/", response_model=list[CampaignOut])
async def list_campaigns(
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list:
    return await CampaignService(db, auth.tenant_id).list()


@router.get("/{campaign_id}", response_model=CampaignOut)
async def get_campaign(
    campaign_id: UUID,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CampaignOut:
    campaign = await CampaignService(db, auth.tenant_id).get(campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


@router.post("/", response_model=CampaignOut, status_code=201)
async def create_campaign(
    payload: CampaignCreate,
    auth: Annotated[AuthContext, Depends(require_roles(UserRole.ADMIN, UserRole.OWNER, UserRole.AGENT))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CampaignOut:
    return await CampaignService(db, auth.tenant_id).create(payload)


@router.post("/{campaign_id}/pause", response_model=CampaignOut)
async def pause_campaign(
    campaign_id: UUID,
    auth: Annotated[AuthContext, Depends(require_roles(UserRole.ADMIN, UserRole.OWNER, UserRole.AGENT))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CampaignOut:
    service = CampaignService(db, auth.tenant_id)
    try:
        campaign = await service.pause(campaign_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


@router.post("/{campaign_id}/resume", response_model=CampaignOut)
async def resume_campaign(
    campaign_id: UUID,
    auth: Annotated[AuthContext, Depends(require_roles(UserRole.ADMIN, UserRole.OWNER, UserRole.AGENT))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CampaignOut:
    service = CampaignService(db, auth.tenant_id)
    try:
        campaign = await service.resume(campaign_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign
