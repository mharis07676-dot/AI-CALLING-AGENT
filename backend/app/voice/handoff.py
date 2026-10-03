"""Human handoff: redirect the active Twilio CallSid to Dial a configured agent.

The model must never choose the destination number. Destination always comes
from HUMAN_HANDOFF_NUMBER.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID
from xml.sax.saxutils import escape

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Call, CallStatus, Handoff, HandoffStatus
from app.services import CallService, HandoffService

logger = logging.getLogger(__name__)

# Telephony statuses stored on Call.handoff_status (not the Handoff enum).
HANDOFF_STATUS_REQUESTED = "requested"
HANDOFF_STATUS_DIALING = "dialing"
HANDOFF_STATUS_CONNECTED = "connected"
HANDOFF_STATUS_COMPLETED = "completed"
HANDOFF_STATUS_NO_ANSWER = "no_answer"
HANDOFF_STATUS_BUSY = "busy"
HANDOFF_STATUS_FAILED = "failed"
HANDOFF_STATUS_CANCELLED = "cancelled"
HANDOFF_STATUS_UNAVAILABLE = "unavailable"

_DESTINATION_ARG_KEYS = frozenset(
    {
        "phone",
        "phone_number",
        "number",
        "destination",
        "destination_number",
        "to",
        "to_number",
        "transfer_to",
        "agent_number",
        "human_number",
    }
)

_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")

# In-process per-call mute / isolation (never shared across CallSids).
_ai_silenced: dict[UUID, bool] = {}
_redirect_started: dict[UUID, bool] = {}


def mask_phone(number: str | None) -> str:
    digits = re.sub(r"\D", "", number or "")
    if len(digits) < 4:
        return "****"
    return f"{'*' * max(7, len(digits) - 4)}{digits[-4:]}"


def strip_model_destination_args(arguments: dict[str, Any]) -> dict[str, Any]:
    """Remove any phone/destination fields the model tried to supply."""
    clean = dict(arguments or {})
    for key in list(clean.keys()):
        if str(key).lower() in _DESTINATION_ARG_KEYS:
            clean.pop(key, None)
    return clean


def configured_handoff_number() -> str | None:
    settings = get_settings()
    if not settings.human_handoff_enabled:
        return None
    number = (settings.human_handoff_number or "").strip()
    if not number:
        return None
    if not _E164_RE.match(number):
        logger.warning("HUMAN_HANDOFF_NUMBER is not valid E.164 (value not logged)")
        return None
    return number


def handoff_timeout_seconds() -> int:
    settings = get_settings()
    try:
        value = int(settings.human_handoff_timeout_seconds)
    except (TypeError, ValueError):
        value = 25
    return max(5, min(value, 120))


def public_api_base() -> str:
    return (get_settings().public_base_url or "").rstrip("/")


def is_ai_silenced(call_id: UUID) -> bool:
    return bool(_ai_silenced.get(call_id))


def mark_ai_silenced(call_id: UUID, silenced: bool = True) -> None:
    if silenced:
        _ai_silenced[call_id] = True
    else:
        _ai_silenced.pop(call_id, None)


def clear_handoff_runtime(call_id: UUID) -> None:
    _ai_silenced.pop(call_id, None)
    _redirect_started.pop(call_id, None)


def build_dial_twiml(
    *,
    destination: str,
    timeout: int,
    action_url: str,
    status_callback_url: str | None = None,
) -> str:
    """Build Dial TwiML. Destination must already be the configured env number."""
    number_attrs = ""
    if status_callback_url:
        number_attrs = (
            f' statusCallback="{escape(status_callback_url)}" '
            f'statusCallbackEvent="answered completed" '
            f'statusCallbackMethod="POST"'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Dial timeout="{int(timeout)}" action="{escape(action_url)}" method="POST">'
        f"<Number{number_attrs}>{escape(destination)}</Number>"
        "</Dial>"
        "</Response>"
    )


def build_unavailable_twiml(message: str) -> str:
    safe = escape(message)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"<Say voice=\"Polly.Joanna\">{safe}</Say>"
        "<Pause length=\"1\"/>"
        "<Hangup/>"
        "</Response>"
    )


async def resolve_twilio_call_sid(
    *,
    call: Call,
    twilio: Any | None = None,
) -> str | None:
    """Resolve the active Twilio CallSid for this call only (isolated)."""
    # Lazy import avoids pulling DB engine when tools/config are imported in tests.
    from app.voice.recording import TwilioRecordingClient, looks_like_twilio_call_sid

    meta = dict(call.metadata_json or {})
    for candidate in (
        meta.get("twilio_call_sid"),
        call.provider_call_id,
    ):
        if looks_like_twilio_call_sid(str(candidate) if candidate else None):
            return str(candidate)

    owns = twilio is None
    client = twilio or TwilioRecordingClient()
    try:
        if owns:
            await client.__aenter__()
        if not client.configured:
            return None
        return await client.find_call_sid(
            from_number=call.from_number,
            started_at=call.started_at,
        )
    finally:
        if owns:
            await client.__aexit__(None, None, None)


async def prepare_handoff(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    call_id: UUID,
    reason: str,
    department: str | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark handoff requested and resolve CallSid. Does not redirect yet."""
    settings = get_settings()
    calls = CallService(db, tenant_id)
    call = await calls.get(call_id)
    if call is None:
        return {
            "ok": False,
            "error": "call_not_found",
            "speakable_summary": "I could not start the transfer right now.",
        }

    destination = configured_handoff_number()
    if not settings.human_handoff_enabled or not destination:
        await _set_call_handoff(
            calls,
            call,
            status=HANDOFF_STATUS_UNAVAILABLE,
            reason=reason,
            requested=True,
        )
        await calls.add_event(
            call_id,
            "handoff.unavailable",
            {"reason": "human_handoff_not_configured"},
        )
        return {
            "ok": False,
            "error": "human_handoff_not_configured",
            "speakable_summary": (
                "I'm sorry, a human representative is not available right now. "
                "I can continue helping you here."
            ),
        }

    if not settings.twilio_hangup_configured:
        await _set_call_handoff(
            calls,
            call,
            status=HANDOFF_STATUS_FAILED,
            reason=reason,
            requested=True,
        )
        return {
            "ok": False,
            "error": "twilio_not_configured",
            "speakable_summary": (
                "I'm sorry, I can't connect you to a representative right now. "
                "I can continue helping you here."
            ),
        }

    if not public_api_base():
        await _set_call_handoff(
            calls,
            call,
            status=HANDOFF_STATUS_FAILED,
            reason=reason,
            requested=True,
        )
        return {
            "ok": False,
            "error": "public_base_url_missing",
            "speakable_summary": (
                "I'm sorry, I can't connect you to a representative right now. "
                "I can continue helping you here."
            ),
        }

    # Avoid infinite transfer loops on the same call.
    if call.handoff_status in {
        HANDOFF_STATUS_DIALING,
        HANDOFF_STATUS_CONNECTED,
    }:
        return {
            "ok": False,
            "error": "handoff_already_in_progress",
            "speakable_summary": "I'm already connecting you to a representative.",
            "already_in_progress": True,
        }

    call_sid = await resolve_twilio_call_sid(call=call)
    if not call_sid:
        await _set_call_handoff(
            calls,
            call,
            status=HANDOFF_STATUS_FAILED,
            reason=reason,
            requested=True,
        )
        await calls.add_event(call_id, "handoff.call_sid_missing", {})
        return {
            "ok": False,
            "error": "twilio_call_sid_not_found",
            "speakable_summary": (
                "I'm sorry, I couldn't start the transfer. "
                "I can continue helping you here."
            ),
        }

    meta = dict(call.metadata_json or {})
    meta["twilio_call_sid"] = call_sid
    meta["handoff_destination_masked"] = mask_phone(destination)
    if department:
        meta["handoff_department"] = str(department)[:64]

    handoff_context = dict(context or {})
    if department:
        handoff_context["department"] = str(department)[:64]
    handoff_context["destination_masked"] = mask_phone(destination)
    handoff_context["telephony"] = True

    await HandoffService(db, tenant_id).request_telephony(
        call_id=call_id,
        customer_id=call.customer_id,
        reason=reason,
        context=handoff_context,
    )

    await _set_call_handoff(
        calls,
        call,
        status=HANDOFF_STATUS_REQUESTED,
        reason=reason,
        requested=True,
        metadata_json=meta,
        call_status=CallStatus.TRANSFERRED,
    )
    await calls.add_event(
        call_id,
        "handoff.requested",
        {
            "destination_masked": mask_phone(destination),
            "call_sid_suffix": call_sid[-4:],
            "department": department,
        },
    )

    logger.info(
        "HANDOFF_REQUESTED call_id=%s destination=%s call_sid_suffix=%s",
        call_id,
        mask_phone(destination),
        call_sid[-4:],
    )
    return {
        "ok": True,
        "call_id": str(call_id),
        "call_sid": call_sid,
        "destination_masked": mask_phone(destination),
        "initiate_redirect": True,
        "speakable_summary": "Sure, I'll connect you to a representative.",
    }


async def redirect_active_call(*, tenant_id: UUID, call_id: UUID) -> dict[str, Any]:
    """Update the active Twilio CallSid so Twilio fetches Dial TwiML.

    Does not hang up the caller. Isolated per call_id / CallSid.
    """
    if _redirect_started.get(call_id):
        return {"ok": True, "skipped": True, "reason": "redirect_already_started"}

    settings = get_settings()
    destination = configured_handoff_number()
    if not destination or not settings.twilio_hangup_configured:
        return {"ok": False, "error": "human_handoff_not_configured"}

    base = public_api_base()
    if not base:
        return {"ok": False, "error": "public_base_url_missing"}

    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        calls = CallService(db, tenant_id)
        call = await calls.get(call_id)
        if call is None:
            return {"ok": False, "error": "call_not_found"}
        call_sid = await resolve_twilio_call_sid(call=call)
        if not call_sid:
            await _set_call_handoff(
                calls,
                call,
                status=HANDOFF_STATUS_FAILED,
                reason=call.handoff_reason or "transfer",
                requested=True,
            )
            await db.commit()
            return {"ok": False, "error": "twilio_call_sid_not_found"}

        twiml_url = f"{base}/api/v1/voice/handoff/twiml?call_id={call_id}"
        result = await _twilio_update_call(call_sid, url=twiml_url)
        if not result.get("ok"):
            await _set_call_handoff(
                calls,
                call,
                status=HANDOFF_STATUS_FAILED,
                reason=call.handoff_reason or "transfer",
                requested=True,
            )
            await calls.add_event(
                call_id,
                "handoff.redirect_failed",
                {"error": result.get("error"), "call_sid_suffix": call_sid[-4:]},
            )
            await db.commit()
            return result

        _redirect_started[call_id] = True
        mark_ai_silenced(call_id, True)
        meta = dict(call.metadata_json or {})
        meta["twilio_call_sid"] = call_sid
        await _set_call_handoff(
            calls,
            call,
            status=HANDOFF_STATUS_DIALING,
            reason=call.handoff_reason or "customer_requested_human",
            requested=True,
            metadata_json=meta,
            call_status=CallStatus.TRANSFERRED,
        )
        await calls.add_event(
            call_id,
            "handoff.dialing",
            {
                "destination_masked": mask_phone(destination),
                "call_sid_suffix": call_sid[-4:],
            },
        )
        await db.commit()

    logger.info(
        "HANDOFF_REDIRECTED call_id=%s destination=%s call_sid_suffix=%s",
        call_id,
        mask_phone(destination),
        call_sid[-4:],
    )
    return {
        "ok": True,
        "call_sid": call_sid,
        "destination_masked": mask_phone(destination),
        "caller_hung_up": False,
    }


async def mark_handoff_connected(*, tenant_id: UUID, call_id: UUID) -> None:
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        calls = CallService(db, tenant_id)
        call = await calls.get(call_id)
        if call is None:
            return
        if call.handoff_status == HANDOFF_STATUS_CONNECTED:
            await db.commit()
            return
        now = datetime.now(timezone.utc)
        await _set_call_handoff(
            calls,
            call,
            status=HANDOFF_STATUS_CONNECTED,
            reason=call.handoff_reason or "customer_requested_human",
            requested=True,
            connected_at=now,
            call_status=CallStatus.TRANSFERRED,
        )
        handoff = await _latest_handoff(db, tenant_id, call_id)
        if handoff is not None and handoff.status == HandoffStatus.REQUESTED:
            handoff.status = HandoffStatus.ACCEPTED
        await calls.add_event(call_id, "handoff.connected", {})
        await db.commit()
    logger.info("HANDOFF_CONNECTED call_id=%s", call_id)


async def mark_handoff_dial_result(
    *,
    tenant_id: UUID,
    call_id: UUID,
    dial_status: str,
) -> str:
    """Update DB from Dial action. Returns TwiML for the caller leg."""
    normalized = (dial_status or "").strip().lower().replace("-", "_")
    status_map = {
        "completed": HANDOFF_STATUS_COMPLETED,
        "answered": HANDOFF_STATUS_COMPLETED,
        "busy": HANDOFF_STATUS_BUSY,
        "no_answer": HANDOFF_STATUS_NO_ANSWER,
        "failed": HANDOFF_STATUS_FAILED,
        "canceled": HANDOFF_STATUS_CANCELLED,
        "cancelled": HANDOFF_STATUS_CANCELLED,
    }
    handoff_status = status_map.get(normalized, HANDOFF_STATUS_FAILED)
    now = datetime.now(timezone.utc)

    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        calls = CallService(db, tenant_id)
        call = await calls.get(call_id)
        if call is None:
            return build_unavailable_twiml(
                "Sorry, something went wrong. Please call back later."
            )

        # If human already connected and Dial completed normally, mark completed.
        connected_at = call.handoff_connected_at
        if handoff_status == HANDOFF_STATUS_COMPLETED and connected_at is None:
            # Dial ended without an answered callback — treat as failed transfer.
            if normalized == "completed" and call.handoff_status == HANDOFF_STATUS_CONNECTED:
                pass
            elif normalized == "completed" and call.handoff_status != HANDOFF_STATUS_CONNECTED:
                handoff_status = HANDOFF_STATUS_NO_ANSWER

        await _set_call_handoff(
            calls,
            call,
            status=handoff_status,
            reason=call.handoff_reason or "customer_requested_human",
            requested=True,
            connected_at=connected_at,
            completed_at=now,
            call_status=(
                CallStatus.COMPLETED
                if handoff_status == HANDOFF_STATUS_COMPLETED
                else CallStatus.TRANSFERRED
            ),
            ended_at=now if handoff_status == HANDOFF_STATUS_COMPLETED else None,
        )
        handoff = await _latest_handoff(db, tenant_id, call_id)
        if handoff is not None:
            if handoff_status == HANDOFF_STATUS_COMPLETED:
                handoff.status = HandoffStatus.COMPLETED
            elif handoff_status in {
                HANDOFF_STATUS_NO_ANSWER,
                HANDOFF_STATUS_BUSY,
                HANDOFF_STATUS_FAILED,
                HANDOFF_STATUS_CANCELLED,
            }:
                handoff.status = HandoffStatus.CANCELLED
                ctx = dict(handoff.context or {})
                ctx["dial_status"] = normalized
                handoff.context = ctx
        await calls.add_event(
            call_id,
            "handoff.dial_result",
            {"dial_status": normalized, "handoff_status": handoff_status},
        )
        await db.commit()

    clear_handoff_runtime(call_id)

    # Dial action means the Twilio call is ending or returning TwiML — finalize recording.
    try:
        from app.voice.recording import schedule_finalize_recording

        schedule_finalize_recording(tenant_id=tenant_id, call_id=call_id)
    except Exception:  # noqa: BLE001
        logger.exception("HANDOFF_RECORDING_FINALIZE_SCHEDULE_FAILED call_id=%s", call_id)

    if handoff_status == HANDOFF_STATUS_COMPLETED:
        # Parties already talked; quiet end.
        return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'

    # Safest fallback supported without re-attaching OpenAI SIP mid-call.
    return build_unavailable_twiml(
        "Sorry, no representative is available right now. "
        "Please try again later, or call back and I can help you."
    )


async def _set_call_handoff(
    calls: CallService,
    call: Call,
    *,
    status: str,
    reason: str,
    requested: bool,
    metadata_json: dict | None = None,
    call_status: CallStatus | None = None,
    connected_at: datetime | None = None,
    completed_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    extras: dict[str, Any] = {
        "handoff_requested": requested,
        "handoff_status": status,
        "handoff_reason": (reason or "")[:500],
    }
    if call.handoff_requested_at is None and requested:
        extras["handoff_requested_at"] = now
    if connected_at is not None:
        extras["handoff_connected_at"] = connected_at
    if completed_at is not None:
        extras["handoff_completed_at"] = completed_at
    if metadata_json is not None:
        extras["metadata_json"] = metadata_json
    if ended_at is not None:
        extras["ended_at"] = ended_at
    target_status = call_status or call.status
    await calls.set_status(call.id, target_status, **extras)


async def _latest_handoff(
    db: AsyncSession,
    tenant_id: UUID,
    call_id: UUID,
) -> Handoff | None:
    from sqlalchemy import select

    result = await db.execute(
        select(Handoff)
        .where(Handoff.call_id == call_id, Handoff.tenant_id == tenant_id)
        .order_by(Handoff.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _twilio_update_call(call_sid: str, *, url: str) -> dict[str, Any]:
    settings = get_settings()
    account_sid = settings.twilio_account_sid
    endpoint = (
        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}"
        f"/Calls/{call_sid}.json"
    )
    if settings.twilio_api_key_sid and settings.twilio_api_key_secret:
        auth = (settings.twilio_api_key_sid, settings.twilio_api_key_secret)
    else:
        auth = (account_sid, settings.twilio_auth_token)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                endpoint,
                data={"Url": url, "Method": "POST"},
                auth=auth,
            )
    except httpx.HTTPError:
        logger.exception("Twilio call redirect request failed call_sid_suffix=%s", call_sid[-4:])
        return {
            "ok": False,
            "error": "twilio_redirect_request_failed",
            "caller_hung_up": False,
        }

    if response.status_code >= 400:
        body = (response.text or "")[:180].replace("\n", " ")
        logger.error(
            "Twilio call redirect rejected status=%s call_sid_suffix=%s body=%s",
            response.status_code,
            call_sid[-4:],
            body,
        )
        return {
            "ok": False,
            "error": "twilio_redirect_rejected",
            "status_code": response.status_code,
            "caller_hung_up": False,
        }

    return {
        "ok": True,
        "status_code": response.status_code,
        "caller_hung_up": False,
    }
