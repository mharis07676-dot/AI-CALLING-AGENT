from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.tools import ToolExecutor
from app.db.session import get_db
from app.models import Tenant
from app.schemas import ToolExecutionResult
from app.voice.call_manager import CallManager
from app.voice.realtime import create_realtime_session_stub
from app.voice.sip import SipClient, SipInboundCall

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


async def _tenant_from_slug(db: AsyncSession, tenant_slug: str) -> Tenant:
    result = await db.execute(select(Tenant).where(Tenant.slug == tenant_slug, Tenant.is_active.is_(True)))
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant


@router.post("/sip/{tenant_slug}/inbound")
async def sip_inbound_webhook(
    tenant_slug: str,
    payload: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_synas_webhook_secret: Annotated[str | None, Header()] = None,
) -> dict:
    # Shared-secret check can be tightened per provider later
    _ = x_synas_webhook_secret
    tenant = await _tenant_from_slug(db, tenant_slug)
    from_number = payload.get("from_number") or payload.get("from")
    to_number = payload.get("to_number") or payload.get("to")
    provider_call_id = payload.get("provider_call_id") or payload.get("call_id")
    if not from_number or not to_number:
        raise HTTPException(status_code=400, detail="from_number and to_number are required")

    manager = CallManager(db, tenant.id)
    decision, call = await manager.admit_inbound(
        from_number=from_number,
        to_number=to_number,
        provider_call_id=provider_call_id,
    )
    sip = SipClient()
    await sip.acknowledge_inbound(
        SipInboundCall(
            provider_call_id=provider_call_id or str(call.id if call else ""),
            from_number=from_number,
            to_number=to_number,
        )
    )
    session = None
    if call and decision.accepted:
        session = await create_realtime_session_stub(tenant_id=tenant.id, call_id=call.id)

    return {
        "accepted": decision.accepted,
        "reason": decision.reason,
        "active_calls": decision.active_calls,
        "limit": decision.limit,
        "call_id": str(call.id) if call else None,
        "realtime_session": session,
    }


@router.post("/openai/{tenant_slug}/tools", response_model=ToolExecutionResult)
async def openai_tool_webhook(
    tenant_slug: str,
    payload: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ToolExecutionResult:
    """Sideband tool execution from OpenAI Realtime.

    The model proposes; Synas backend validates and executes.
    """
    tenant = await _tenant_from_slug(db, tenant_slug)
    tool_name = payload.get("tool_name") or payload.get("name")
    arguments = payload.get("arguments") or payload.get("params") or {}
    call_id_raw = payload.get("call_id")
    if not tool_name:
        raise HTTPException(status_code=400, detail="tool_name is required")
    call_id = UUID(call_id_raw) if call_id_raw else None
    executor = ToolExecutor(db, tenant.id)
    return await executor.execute(tool_name, arguments, call_id=call_id)
