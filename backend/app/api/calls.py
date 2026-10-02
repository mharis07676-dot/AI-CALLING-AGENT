from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.tools import ToolExecutor
from app.auth import AuthContext, get_current_auth
from app.db.session import get_db
from app.schemas import (
    CallCreate,
    CallDetailOut,
    CallOut,
    HandoffCreate,
    HandoffOut,
    HangupResponse,
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
) -> list[CallOut]:
    return await CallService(db, auth.tenant_id).list_live()


@router.get("/", response_model=list[CallOut])
async def list_calls(
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[CallOut]:
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
    return CallService(db, auth.tenant_id)._to_out(call)


@router.post("/handoffs", response_model=HandoffOut)
async def create_handoff(
    payload: HandoffCreate,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HandoffOut:
    service = HandoffService(db, auth.tenant_id)
    try:
        handoff = await service.request(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return service._to_out(handoff)


@router.get("/handoffs/open", response_model=list[HandoffOut])
async def open_handoffs(
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[HandoffOut]:
    return await HandoffService(db, auth.tenant_id).list_open()


@router.get("/{call_id}", response_model=CallDetailOut)
async def get_call(
    call_id: UUID,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CallDetailOut:
    detail = await CallService(db, auth.tenant_id).get_detail(call_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Call not found")
    return detail


@router.post("/{call_id}/hangup", response_model=HangupResponse)
async def hangup_call(
    call_id: UUID,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HangupResponse:
    try:
        result = await CallService(db, auth.tenant_id).hangup(call_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Call not found") from exc
    if not result.success and result.error == "call_not_active":
        raise HTTPException(status_code=400, detail=result.message)
    return result


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
