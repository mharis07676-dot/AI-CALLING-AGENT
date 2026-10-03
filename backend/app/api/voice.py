"""Twilio Voice callbacks for human handoff Dial."""

from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.session import get_db
from app.models import Call
from app.voice.handoff import (
    build_dial_twiml,
    build_unavailable_twiml,
    configured_handoff_number,
    handoff_timeout_seconds,
    mark_handoff_connected,
    mark_handoff_dial_result,
    mask_phone,
    public_api_base,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voice/handoff", tags=["voice-handoff"])


async def _call_tenant(db: AsyncSession, call_id: UUID) -> tuple[Call | None, UUID | None]:
    result = await db.execute(select(Call).where(Call.id == call_id))
    call = result.scalar_one_or_none()
    if call is None:
        return None, None
    return call, call.tenant_id


async def _form_dict(request: Request) -> dict[str, Any]:
    if request.method != "POST":
        return {}
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001
        return {}
    return {str(k): form.get(k) for k in form.keys()}


@router.api_route("/twiml", methods=["GET", "POST"])
async def handoff_twiml(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    call_id: UUID = Query(...),
) -> Response:
    """Twilio fetches this TwiML to Dial the configured human agent number."""
    call, tenant_id = await _call_tenant(db, call_id)
    if call is None or tenant_id is None:
        xml = build_unavailable_twiml(
            "Sorry, something went wrong with the transfer. Please call back later."
        )
        return Response(content=xml, media_type="application/xml")

    destination = configured_handoff_number()
    base = public_api_base()
    if not destination or not base:
        logger.warning("HANDOFF_TWIML_UNAVAILABLE call_id=%s", call_id)
        xml = build_unavailable_twiml(
            "Sorry, no representative is available right now. Please try again later."
        )
        return Response(content=xml, media_type="application/xml")

    timeout = handoff_timeout_seconds()
    action_url = f"{base}/api/v1/voice/handoff/dial-status?call_id={call_id}"
    status_url = f"{base}/api/v1/voice/handoff/number-status?call_id={call_id}"
    xml = build_dial_twiml(
        destination=destination,
        timeout=timeout,
        action_url=action_url,
        status_callback_url=status_url,
    )
    logger.info(
        "HANDOFF_TWIML call_id=%s destination=%s timeout=%s",
        call_id,
        mask_phone(destination),
        timeout,
    )
    _ = request
    return Response(content=xml, media_type="application/xml")


@router.api_route("/number-status", methods=["GET", "POST"])
async def handoff_number_status(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    call_id: UUID = Query(...),
) -> dict:
    """Twilio Number statusCallback — mark connected when human answers."""
    call, tenant_id = await _call_tenant(db, call_id)
    if call is None or tenant_id is None:
        return {"ok": False}
    form = await _form_dict(request)
    status = str(form.get("CallStatus") or request.query_params.get("CallStatus") or "").strip().lower()
    call_sid = str(form.get("CallSid") or request.query_params.get("CallSid") or "")
    if status == "answered":
        await mark_handoff_connected(tenant_id=tenant_id, call_id=call_id)
    logger.info(
        "HANDOFF_NUMBER_STATUS call_id=%s status=%s call_sid_suffix=%s",
        call_id,
        status,
        call_sid[-4:],
    )
    return {"ok": True}


@router.api_route("/dial-status", methods=["GET", "POST"])
async def handoff_dial_status(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    call_id: UUID = Query(...),
) -> Response:
    """Dial action callback — handle no-answer / busy / completed safely."""
    call, tenant_id = await _call_tenant(db, call_id)
    if call is None or tenant_id is None:
        xml = build_unavailable_twiml(
            "Sorry, something went wrong. Please call back later."
        )
        return Response(content=xml, media_type="application/xml")

    form = await _form_dict(request)
    dial_status = str(
        form.get("DialCallStatus")
        or request.query_params.get("DialCallStatus")
        or ""
    )
    dial_sid = str(form.get("DialCallSid") or request.query_params.get("DialCallSid") or "")

    xml = await mark_handoff_dial_result(
        tenant_id=tenant_id,
        call_id=call_id,
        dial_status=dial_status,
    )
    logger.info(
        "HANDOFF_DIAL_STATUS call_id=%s dial_status=%s dial_sid_suffix=%s",
        call_id,
        dial_status,
        dial_sid[-4:],
    )
    return Response(content=xml, media_type="application/xml")


@router.get("/health")
async def handoff_health() -> dict:
    settings = get_settings()
    return {
        "enabled": bool(settings.human_handoff_enabled),
        "number_configured": bool((settings.human_handoff_number or "").strip()),
        "destination_masked": mask_phone(settings.human_handoff_number),
        "timeout_seconds": handoff_timeout_seconds(),
        "public_base_url_set": bool(public_api_base()),
        "twilio_ready": bool(settings.twilio_hangup_configured),
    }
