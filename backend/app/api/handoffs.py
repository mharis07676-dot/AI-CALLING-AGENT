from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthContext, get_current_auth
from app.db.session import get_db
from app.schemas import HandoffOut, HandoffUpdate
from app.services import HandoffService

router = APIRouter(prefix="/handoffs", tags=["handoffs"])


@router.patch("/{handoff_id}", response_model=HandoffOut)
async def update_handoff(
    handoff_id: UUID,
    payload: HandoffUpdate,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HandoffOut:
    """Accept or resolve a handoff (status / assigned_to / resolution_notes)."""
    updated = await HandoffService(db, auth.tenant_id).update(handoff_id, payload)
    if updated is None:
        raise HTTPException(status_code=404, detail="Handoff not found")
    return updated
