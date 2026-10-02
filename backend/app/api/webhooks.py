"""Inbound webhooks — OpenAI Realtime SIP is the authoritative call-accept path."""

from __future__ import annotations

import json
import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.tools import ToolExecutor
from app.config import get_settings
from app.db.session import get_db
from app.models import Tenant
from app.schemas import ToolExecutionResult
from app.voice.inbound_sip import handle_realtime_incoming_sip
from app.voice.openai_webhook import InvalidOpenAIWebhookSignature, verify_openai_webhook_signature

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


async def _tenant_from_slug(db: AsyncSession, tenant_slug: str) -> Tenant:
    result = await db.execute(select(Tenant).where(Tenant.slug == tenant_slug, Tenant.is_active.is_(True)))
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant


def _header_map(request: Request) -> dict[str, str]:
    return {k: v for k, v in request.headers.items()}


@router.post("/openai/{tenant_slug}/inbound")
async def openai_realtime_sip_inbound(
    tenant_slug: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Authoritative OpenAI Realtime SIP inbound webhook.

    Configure this URL in OpenAI Platform project webhooks for event:
    ``realtime.call.incoming``.
    """
    settings = get_settings()
    body_bytes = await request.body()
    headers = _header_map(request)

    try:
        verify_openai_webhook_signature(
            payload=body_bytes,
            headers=headers,
            secret=settings.openai_webhook_secret,
        )
    except InvalidOpenAIWebhookSignature:
        logger.warning("Invalid OpenAI webhook signature for tenant=%s", tenant_slug)
        raise HTTPException(status_code=400, detail="Invalid webhook signature") from None

    try:
        event = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Malformed webhook payload") from exc

    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="Malformed webhook payload")

    event_type = event.get("type")
    if event_type != "realtime.call.incoming":
        # Acknowledge unrelated OpenAI events without treating them as call accepts.
        return {"ok": True, "ignored": True, "type": event_type}

    tenant = await _tenant_from_slug(db, tenant_slug)
    webhook_id = headers.get("webhook-id") or headers.get("Webhook-Id")

    try:
        result = await handle_realtime_incoming_sip(
            db,
            tenant_id=tenant.id,
            event=event,
            webhook_id=webhook_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Malformed SIP event: {exc}") from exc

    # Never echo secrets. Keep response shape ops-safe.
    safe = {
        "ok": bool(result.get("ok")),
        "accepted": bool(result.get("accepted")),
        "duplicate": bool(result.get("duplicate")),
        "call_id": result.get("call_id"),
        "openai_call_id": result.get("openai_call_id"),
        "status": result.get("status"),
        "reason": result.get("reason"),
        "error": result.get("error"),
        "message": result.get("message"),
    }
    if result.get("ok") is False and result.get("accepted") is False and result.get("error"):
        # Persist failure already happened; signal OpenAI with 502 for ops visibility.
        raise HTTPException(status_code=502, detail=safe)
    return safe


@router.post("/sip/{tenant_slug}/inbound")
async def sip_inbound_webhook(
    tenant_slug: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Legacy path — delegates OpenAI ``realtime.call.incoming`` to the authoritative handler.

    Non-OpenAI payloads are rejected so there is only one call-accept path.
    """
    body_bytes = await request.body()
    try:
        payload = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}

    if isinstance(payload, dict) and payload.get("type") == "realtime.call.incoming":
        # Re-dispatch through the authoritative OpenAI handler (signature required).
        return await openai_realtime_sip_inbound(tenant_slug, request, db)

    raise HTTPException(
        status_code=410,
        detail=(
            "Legacy SIP inbound stub removed. Configure OpenAI project webhook to "
            "POST /api/v1/webhooks/openai/{tenant_slug}/inbound for realtime.call.incoming."
        ),
    )


@router.post("/openai/{tenant_slug}/tools", response_model=ToolExecutionResult)
async def openai_tool_webhook(
    tenant_slug: str,
    payload: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ToolExecutionResult:
    """Optional HTTP tool execution sideband (Realtime WS is preferred)."""
    tenant = await _tenant_from_slug(db, tenant_slug)
    tool_name = payload.get("tool_name") or payload.get("name")
    arguments = payload.get("arguments") or payload.get("params") or {}
    call_id_raw = payload.get("call_id")
    if not tool_name:
        raise HTTPException(status_code=400, detail="tool_name is required")
    call_id = UUID(call_id_raw) if call_id_raw else None
    executor = ToolExecutor(db, tenant.id)
    return await executor.execute(tool_name, arguments, call_id=call_id)
