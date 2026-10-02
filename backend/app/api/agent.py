from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthContext, get_current_auth, require_roles
from app.db.session import get_db
from app.models import UserRole
from app.schemas import AgentConfigOut, AgentConfigUpdate
from app.services import AgentConfigService

router = APIRouter(prefix="/agent", tags=["agent"])


@router.get("/config", response_model=AgentConfigOut)
async def get_agent_config(
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AgentConfigOut:
    """Return safe business agent settings only — never secrets."""
    return await AgentConfigService(db, auth.tenant_id).get()


@router.put("/config", response_model=AgentConfigOut)
async def put_agent_config(
    payload: AgentConfigUpdate,
    auth: Annotated[AuthContext, Depends(require_roles(UserRole.ADMIN, UserRole.OWNER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AgentConfigOut:
    return await AgentConfigService(db, auth.tenant_id).update(payload)
