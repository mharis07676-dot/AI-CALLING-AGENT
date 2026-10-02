from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.tools import ToolExecutor
from app.auth import AuthContext, get_current_auth
from app.db.session import get_db
from app.schemas import (
    CallCreate,
    CallOut,
    HandoffCreate,
    HandoffOut,
    ToolExecutionResult,
)
from app.services import CallService, HandoffService
from app.voice.call_manager import CallManager
from app.voice.realtime import create_realtime_session_stub

router = APIRouter(prefix="/calls", tags=["calls"])


@router.get("/live", response_model=list[CallOut])
async def live_calls(
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list:
    return await CallService(db, auth.tenant_id).list_live()


@router.get("/", response_model=list[CallOut])
async def list_calls(
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list:
    return await CallService(db, auth.tenant_id).list_recent()


@router.post("/inbound", response_model=CallOut)
async def admit_inbound_call(
    payload: CallCreate,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CallOut:
    manager = CallManager(db, auth.tenant_id)
    decision, call = await manager.admit_inbound(
        from_number=payload.from_number,
        to_number=payload.to_number,
        provider_call_id=payload.provider_call_id,
    )
    if call is None:
        raise HTTPException(status_code=503, detail=decision.reason)
    if not decision.accepted:
        raise HTTPException(
            status_code=429,
            detail={
                "reason": decision.reason,
                "active_calls": decision.active_calls,
                "limit": decision.limit,
                "call_id": str(call.id),
            },
        )
    await create_realtime_session_stub(tenant_id=auth.tenant_id, call_id=call.id)
    return call


@router.post("/{call_id}/tools/{tool_name}", response_model=ToolExecutionResult)
async def execute_tool(
    call_id: UUID,
    tool_name: str,
    arguments: dict,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ToolExecutionResult:
    executor = ToolExecutor(db, auth.tenant_id)
    return await executor.execute(tool_name, arguments, call_id=call_id)


@router.post("/handoffs", response_model=HandoffOut)
async def create_handoff(
    payload: HandoffCreate,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HandoffOut:
    try:
        return await HandoffService(db, auth.tenant_id).request(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/handoffs/open", response_model=list[HandoffOut])
async def open_handoffs(
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list:
    return await HandoffService(db, auth.tenant_id).list_open()
