"""Authoritative OpenAI Realtime SIP inbound call handler."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import CallStatus
from app.services import AgentConfigService, CallService
from app.voice.call_manager import CallManager
from app.voice.idempotency import claim_idempotency, release_idempotency
from app.voice.openai_webhook import IncomingSipCall, parse_realtime_incoming_event
from app.voice.realtime import accept_realtime_call, build_accept_payload, reject_realtime_call
from app.voice.session_monitor import start_sideband_monitor

logger = logging.getLogger(__name__)


async def _release_idempotency(db: AsyncSession, *, scope: str, key: str) -> None:
    await release_idempotency(db, scope=scope, key=key)


async def handle_realtime_incoming_sip(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    event: dict[str, Any],
    webhook_id: str | None,
) -> dict[str, Any]:
    """Accept or reject an inbound OpenAI Realtime SIP call.

    This is the single authoritative accept path for inbound SIP.
    """
    settings = get_settings()
    incoming = parse_realtime_incoming_event(event, webhook_id=webhook_id)
    calls = CallService(db, tenant_id)

    existing = await calls.get_by_openai_session_id(incoming.openai_call_id)
    if existing is not None and existing.status in {
        CallStatus.ACTIVE,
        CallStatus.COMPLETED,
        CallStatus.TRANSFERRED,
    }:
        # Already accepted — ensure sideband is attached, do not accept twice.
        if existing.status == CallStatus.ACTIVE:
            await start_sideband_monitor(
                tenant_id=tenant_id,
                call_id=existing.id,
                openai_call_id=incoming.openai_call_id,
            )
        return {
            "ok": True,
            "duplicate": True,
            "accepted": True,
            "call_id": str(existing.id),
            "openai_call_id": incoming.openai_call_id,
            "status": existing.status.value,
            "message": "OpenAI call already accepted",
        }

    # Deduplicate successful webhook deliveries. Failed accepts are not claimed
    # so OpenAI retries can re-attempt accept.
    dedupe_key = incoming.webhook_id or incoming.event_id
    claimed = await claim_idempotency(
        db,
        scope="openai_webhook",
        key=dedupe_key,
        tenant_id=tenant_id,
        call_id=existing.id if existing else None,
    )
    if not claimed:
        latest = existing or await calls.get_by_openai_session_id(incoming.openai_call_id)
        return {
            "ok": True,
            "duplicate": True,
            "accepted": latest is not None
            and latest.status not in {CallStatus.REJECTED, CallStatus.FAILED},
            "call_id": str(latest.id) if latest else None,
            "openai_call_id": incoming.openai_call_id,
            "message": "Duplicate webhook delivery ignored",
        }

    # Twilio/OpenAI often retry SIP INVITEs with new openai call_ids. Accepting each
    # one fills capacity and ends as Busy/486. Keep one in-flight call per caller.
    open_for_caller = await calls.get_open_for_caller(
        incoming.from_number,
        within_seconds=90,
    )
    if (
        open_for_caller is not None
        and open_for_caller.openai_session_id
        and open_for_caller.openai_session_id != incoming.openai_call_id
    ):
        logger.warning(
            "Rejecting parallel SIP invite for caller=%s existing_call=%s new_openai_call_id=%s",
            incoming.from_number,
            open_for_caller.id,
            incoming.openai_call_id,
        )
        await reject_realtime_call(openai_call_id=incoming.openai_call_id, status_code=486)
        return {
            "ok": True,
            "accepted": False,
            "duplicate": True,
            "reason": "caller_already_in_progress",
            "call_id": str(open_for_caller.id),
            "openai_call_id": incoming.openai_call_id,
            "message": "Caller already has an in-progress call",
        }

    manager = CallManager(db, tenant_id)
    if existing is not None and existing.status in {CallStatus.FAILED, CallStatus.RINGING}:
        decision = await manager.can_accept()
        call = existing
        if not decision.accepted and existing.status == CallStatus.RINGING:
            await calls.set_status(
                call.id,
                CallStatus.REJECTED,
                failure_reason=decision.reason,
            )
    else:
        decision, call = await manager.admit_inbound(
            from_number=incoming.from_number,
            to_number=incoming.to_number,
            provider_call_id=incoming.provider_call_id,
            openai_session_id=incoming.openai_call_id,
        )

    if call is None:
        return {
            "ok": False,
            "accepted": False,
            "error": "call_create_failed",
            "message": "Failed to create call record",
        }

    await calls.add_event(
        call.id,
        "openai.realtime.call.incoming",
        {
            "event_id": incoming.event_id,
            "webhook_id": incoming.webhook_id,
            "openai_call_id": incoming.openai_call_id,
            # SIP headers are metadata only — never treated as authorization.
            "from": incoming.from_number,
            "to": incoming.to_number,
        },
    )

    if not decision.accepted:
        logger.warning(
            "Rejecting OpenAI SIP call with 486 Busy tenant=%s reason=%s openai_call_id=%s",
            tenant_id,
            decision.reason,
            incoming.openai_call_id,
        )
        reject_result = await reject_realtime_call(
            openai_call_id=incoming.openai_call_id,
            status_code=486,
        )
        return {
            "ok": True,
            "accepted": False,
            "reason": decision.reason,
            "call_id": str(call.id),
            "openai_call_id": incoming.openai_call_id,
            "reject": {"ok": reject_result.get("ok"), "error": reject_result.get("error")},
        }

    agent = await AgentConfigService(db, tenant_id).get()
    session_config = build_accept_payload(
        tenant_id=tenant_id,
        call_id=call.id,
        voice=agent.voice or "alloy",
        language_hint="roman_urdu",
        instructions=agent.system_instructions or None,
    )

    accept_result = await accept_realtime_call(
        openai_call_id=incoming.openai_call_id,
        session_config=session_config,
    )
    if not accept_result.get("ok"):
        # Allow webhook retries: release the dedupe claim on accept failure.
        await _release_idempotency(db, scope="openai_webhook", key=dedupe_key)
        await calls.set_status(
            call.id,
            CallStatus.FAILED,
            failure_reason=accept_result.get("error") or "openai_accept_failed",
        )
        await calls.add_event(
            call.id,
            "openai.accept_failed",
            {
                "error": accept_result.get("error"),
                "status_code": accept_result.get("status_code"),
            },
        )
        logger.error(
            "OpenAI accept failed tenant=%s call=%s error=%s",
            tenant_id,
            call.id,
            accept_result.get("error"),
        )
        return {
            "ok": False,
            "accepted": False,
            "call_id": str(call.id),
            "openai_call_id": incoming.openai_call_id,
            "error": accept_result.get("error"),
            "message": accept_result.get("message") or "OpenAI accept failed",
        }

    # Stay RINGING until sideband connects (monitor promotes to ACTIVE).
    await calls.set_status(
        call.id,
        CallStatus.RINGING,
        openai_session_id=incoming.openai_call_id,
    )
    await calls.ensure_conversation(
        call.id,
        language="roman_urdu",
    )
    await calls.add_event(
        call.id,
        "openai.accept_succeeded",
        {
            "openai_call_id": incoming.openai_call_id,
            "model": settings.openai_realtime_model,
        },
    )

    # Sideband attaches to the already-accepted SIP session.
    await start_sideband_monitor(
        tenant_id=tenant_id,
        call_id=call.id,
        openai_call_id=incoming.openai_call_id,
    )

    return {
        "ok": True,
        "accepted": True,
        "duplicate": False,
        "call_id": str(call.id),
        "openai_call_id": incoming.openai_call_id,
        "status": CallStatus.RINGING.value,
        "message": "OpenAI realtime SIP call accepted; sideband monitor started",
    }


def parse_incoming_for_tests(event: dict[str, Any], webhook_id: str | None = None) -> IncomingSipCall:
    """Test helper re-export."""
    return parse_realtime_incoming_event(event, webhook_id=webhook_id)
