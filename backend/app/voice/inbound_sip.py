"""Authoritative OpenAI Realtime SIP inbound call handler."""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import CallStatus, Customer
from app.services import AgentConfigService, CallService
from app.voice.call_lifecycle import (
    CallLifecycle,
    log_call_event,
    openai_session_confirmed_gone,
    prior_call_blocks_new_invite,
    utc_now,
)
from app.voice.call_manager import CallManager
from app.voice.concurrency import release_concurrency_slot
from app.voice.idempotency import claim_idempotency, release_idempotency
from app.voice.language_control import state_from_preference
from app.voice.openai_webhook import IncomingSipCall, parse_realtime_incoming_event
from app.voice.realtime import (
    accept_realtime_call,
    build_accept_payload,
    reject_realtime_call,
    resolve_realtime_voice,
    select_initial_greeting,
)
from app.voice.realtime_forensics import is_openai_session_confirmed_gone
from app.voice.recording import (
    recording_notice_enabled,
    recording_notice_text,
    schedule_start_recording,
)
from app.voice.session_monitor import start_sideband_monitor

logger = logging.getLogger(__name__)

_ACCEPT_ATTEMPTS = 3
_TERMINAL_STATUSES = {
    CallStatus.COMPLETED,
    CallStatus.FAILED,
    CallStatus.REJECTED,
    CallStatus.TRANSFERRED,
}


async def _release_idempotency(db: AsyncSession, *, scope: str, key: str) -> None:
    await release_idempotency(db, scope=scope, key=key)


def _duplicate_result(call: Any, incoming: IncomingSipCall, *, accepted: bool) -> dict[str, Any]:
    return {
        "ok": True,
        "duplicate": True,
        "accepted": accepted,
        "call_id": str(call.id) if call is not None else None,
        "openai_call_id": incoming.openai_call_id,
        "status": call.status.value if call is not None and getattr(call, "status", None) else None,
        "message": "Duplicate webhook delivery ignored",
    }


async def handle_realtime_incoming_sip(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    event: dict[str, Any],
    webhook_id: str | None,
) -> dict[str, Any]:
    """Accept or reject an inbound OpenAI Realtime SIP call.

    Signature verification happens in the webhook route. This function dedupes,
    accepts, then does CRM / recording / sideband work. Accept is not gated on
    those steps.
    """
    settings = get_settings()
    webhook_received_at = utc_now()
    incoming = parse_realtime_incoming_event(event, webhook_id=webhook_id)
    log_call_event(
        "CALL_WEBHOOK_RECEIVED",
        tenant_id=tenant_id,
        openai_call_id=incoming.openai_call_id,
        webhook_id=incoming.webhook_id,
        event_id=incoming.event_id,
        webhook_received_at=webhook_received_at.isoformat(),
    )
    calls = CallService(db, tenant_id)

    existing = await calls.get_by_openai_session_id(incoming.openai_call_id)
    if existing is not None and existing.status == CallStatus.ACTIVE:
        # Already accepted — attach control again, never accept twice.
        await start_sideband_monitor(
            tenant_id=tenant_id,
            call_id=existing.id,
            openai_call_id=incoming.openai_call_id,
        )
        return _duplicate_result(existing, incoming, accepted=True)
    if existing is not None and existing.status == CallStatus.FAILED:
        if not await calls.reopen_failed_accept(existing.id):
            return _duplicate_result(existing, incoming, accepted=False)
    elif existing is not None and existing.status in _TERMINAL_STATUSES:
        return _duplicate_result(
            existing,
            incoming,
            accepted=existing.status == CallStatus.TRANSFERRED,
        )

    # One claim per webhook delivery and one claim per OpenAI call id.
    # A second webhook id for the same call must not accept again.
    dedupe_key = incoming.webhook_id or incoming.event_id
    claimed_event = await claim_idempotency(
        db,
        scope="openai_webhook",
        key=dedupe_key,
        tenant_id=tenant_id,
        call_id=existing.id if existing else None,
    )
    claimed_call = await claim_idempotency(
        db,
        scope="openai_call_accept",
        key=incoming.openai_call_id,
        tenant_id=tenant_id,
        call_id=existing.id if existing else None,
    )
    if not claimed_event or not claimed_call:
        latest = existing or await calls.get_by_openai_session_id(incoming.openai_call_id)
        return _duplicate_result(
            latest,
            incoming,
            accepted=latest is not None and latest.status not in {CallStatus.REJECTED, CallStatus.FAILED},
        )

    # A live sideband blocks a second invite. Session-gone / past grace / terminal
    # rows must not 603 — accept the new rtc call after idempotent cleanup.
    prior = await calls.get_open_for_caller(incoming.from_number, within_seconds=90)
    if (
        prior is not None
        and prior.openai_session_id
        and prior.openai_session_id != incoming.openai_call_id
    ):
        prior_gone = openai_session_confirmed_gone(prior) or is_openai_session_confirmed_gone(
            str(prior.openai_session_id),
            call=prior,
        )
        if prior_gone:
            log_call_event(
                "PARALLEL_INVITE_SUPERSEDE",
                tenant_id=tenant_id,
                call_id=prior.id,
                openai_call_id=incoming.openai_call_id,
                prior_openai_call_id=prior.openai_session_id,
                reason="openai_session_confirmed_gone",
            )
            await calls.apply_lifecycle(
                prior.id,
                CallLifecycle.ENDED,
                termination_source="superseded_by_new_invite",
                termination_reason="openai_session_confirmed_gone",
                hangup_requested_by_backend=False,
                extra_metadata={"openai_session_confirmed_gone": True},
            )
        elif prior_call_blocks_new_invite(prior):
            logger.warning(
                "Rejecting parallel SIP invite caller_call=%s new_openai_call_id=%s",
                prior.id,
                incoming.openai_call_id,
            )
            reject_result = await reject_realtime_call(
                openai_call_id=incoming.openai_call_id,
                status_code=603,
            )
            return {
                "ok": True,
                "accepted": False,
                "duplicate": True,
                "reason": "caller_already_in_progress",
                "call_id": str(prior.id),
                "openai_call_id": incoming.openai_call_id,
                "reject": {"ok": reject_result.get("ok"), "error": reject_result.get("error")},
                "message": "Caller already has a live call; parallel invite rejected",
            }
        else:
            await calls.apply_lifecycle(
                prior.id,
                CallLifecycle.ENDED,
                termination_source="superseded_by_new_invite",
                termination_reason="prior_session_not_live",
                hangup_requested_by_backend=False,
            )

    manager = CallManager(db, tenant_id)
    if existing is not None and existing.status in {CallStatus.RINGING, CallStatus.FAILED}:
        decision = await manager.can_accept()
        call = existing
    else:
        decision, call = await manager.admit_inbound(
            from_number=incoming.from_number,
            to_number=incoming.to_number,
            provider_call_id=incoming.provider_call_id,
            openai_session_id=incoming.openai_call_id,
        )

    if call is None:
        await _release_idempotency(db, scope="openai_webhook", key=dedupe_key)
        await _release_idempotency(db, scope="openai_call_accept", key=incoming.openai_call_id)
        return {
            "ok": False,
            "accepted": False,
            "error": "call_create_failed",
            "message": "Failed to create call record",
        }

    if not decision.accepted:
        logger.warning(
            "Declining OpenAI SIP call with 603 tenant=%s reason=%s openai_call_id=%s",
            tenant_id,
            decision.reason,
            incoming.openai_call_id,
        )
        reject_result = await reject_realtime_call(
            openai_call_id=incoming.openai_call_id,
            status_code=603,
        )
        await calls.apply_lifecycle(
            call.id,
            CallLifecycle.FAILED,
            termination_source="admission",
            termination_reason=decision.reason,
            sip_response_code=603,
            hangup_requested_by_backend=False,
            failure_reason=decision.reason,
        )
        return {
            "ok": True,
            "accepted": False,
            "reason": decision.reason,
            "call_id": str(call.id),
            "openai_call_id": incoming.openai_call_id,
            "reject": {"ok": reject_result.get("ok"), "error": reject_result.get("error")},
        }

    await calls.apply_lifecycle(
        call.id,
        CallLifecycle.ACCEPTING,
        webhook_event_id=incoming.event_id,
        extra_metadata={
            "webhook_id": incoming.webhook_id,
            "webhook_received_at": webhook_received_at.isoformat(),
            "from": incoming.from_number,
            "to": incoming.to_number,
        },
    )

    # Accept with the default session. Tenant voice, CRM language, and recording
    # run after OpenAI has the call.
    session_config = build_accept_payload(tenant_id=tenant_id, call_id=call.id)
    accept_started_at = utc_now()
    started = time.perf_counter()
    log_call_event(
        "CALL_ACCEPT_STARTED",
        tenant_id=tenant_id,
        call_id=call.id,
        openai_call_id=incoming.openai_call_id,
        webhook_received_at=webhook_received_at.isoformat(),
        accept_started_at=accept_started_at.isoformat(),
    )
    try:
        accept_result = await accept_realtime_call(
            openai_call_id=incoming.openai_call_id,
            session_config=session_config,
            attempts=_ACCEPT_ATTEMPTS,
        )
    except Exception:
        logger.exception("OpenAI accept raised call_id=%s", call.id)
        accept_result = {
            "ok": False,
            "error": "openai_accept_request_failed",
            "message": "OpenAI accept request failed",
        }
    accept_completed_at = utc_now()
    accept_latency_ms = int((time.perf_counter() - started) * 1000)

    if not accept_result.get("ok"):
        await _release_idempotency(db, scope="openai_webhook", key=dedupe_key)
        await _release_idempotency(db, scope="openai_call_accept", key=incoming.openai_call_id)
        await calls.apply_lifecycle(
            call.id,
            CallLifecycle.FAILED,
            termination_source="openai_accept",
            termination_reason=accept_result.get("error") or "openai_accept_failed",
            openai_error_code=accept_result.get("error"),
            openai_error_message=accept_result.get("message"),
            sip_response_code=accept_result.get("status_code"),
            hangup_requested_by_backend=False,
            extra_metadata={
                "webhook_received_at": webhook_received_at.isoformat(),
                "accept_started_at": accept_started_at.isoformat(),
                "accept_completed_at": accept_completed_at.isoformat(),
                "accept_latency_ms": accept_latency_ms,
            },
        )
        await release_concurrency_slot(tenant_id=tenant_id, call_id=call.id)
        log_call_event(
            "CALL_FAILED",
            tenant_id=tenant_id,
            call_id=call.id,
            openai_call_id=incoming.openai_call_id,
            http_status=accept_result.get("status_code"),
            error=accept_result.get("error"),
            accept_latency_ms=accept_latency_ms,
            webhook_received_at=webhook_received_at.isoformat(),
            accept_started_at=accept_started_at.isoformat(),
            accept_completed_at=accept_completed_at.isoformat(),
        )
        return {
            "ok": False,
            "accepted": False,
            "call_id": str(call.id),
            "openai_call_id": incoming.openai_call_id,
            "error": accept_result.get("error"),
            "message": accept_result.get("message") or "OpenAI accept failed",
        }

    await calls.apply_lifecycle(
        call.id,
        CallLifecycle.ACTIVE,
        webhook_event_id=incoming.event_id,
        hangup_requested_by_backend=False,
        answered_at=accept_completed_at,
        extra_metadata={
            "webhook_id": incoming.webhook_id,
            "webhook_received_at": webhook_received_at.isoformat(),
            "accept_started_at": accept_started_at.isoformat(),
            "accept_completed_at": accept_completed_at.isoformat(),
            "accepted_at": accept_completed_at.isoformat(),
            "accept_latency_ms": accept_latency_ms,
            "openai_call_id": incoming.openai_call_id,
        },
    )
    log_call_event(
        "CALL_ACCEPTED",
        tenant_id=tenant_id,
        call_id=call.id,
        openai_call_id=incoming.openai_call_id,
        webhook_received_at=webhook_received_at.isoformat(),
        accept_started_at=accept_started_at.isoformat(),
        accept_completed_at=accept_completed_at.isoformat(),
        accept_latency_ms=accept_latency_ms,
        http_status=accept_result.get("status_code"),
    )

    try:
        await _initialize_after_accept(
            db,
            tenant_id=tenant_id,
            call_id=call.id,
            customer_id=getattr(call, "customer_id", None),
            openai_call_id=incoming.openai_call_id,
            model=settings.openai_realtime_model,
        )
    except Exception:
        # The SIP call is already accepted. Init failure must not hang up,
        # fail the lifecycle, or hold the next caller.
        logger.exception(
            "Post-accept initialization failed call_id=%s openai_call_id=%s",
            call.id,
            incoming.openai_call_id,
        )

    return {
        "ok": True,
        "accepted": True,
        "duplicate": False,
        "call_id": str(call.id),
        "openai_call_id": incoming.openai_call_id,
        "status": CallStatus.ACTIVE.value,
        "message": "OpenAI realtime SIP call accepted; sideband monitor started",
    }


async def _initialize_after_accept(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    call_id: UUID,
    customer_id: UUID | None,
    openai_call_id: str,
    model: str,
) -> None:
    """CRM language, tenant prompt, sideband, and recording. Never accepts or hangs up."""
    calls = CallService(db, tenant_id)
    agent = await AgentConfigService(db, tenant_id).get()
    preferred_language = await _caller_preferred_language(db, customer_id)
    language_state = state_from_preference(preferred_language)
    pref_label, initial_greeting = select_initial_greeting(preferred_language)
    if recording_notice_enabled():
        notice = recording_notice_text()
        if notice:
            initial_greeting = f"{initial_greeting} {notice}"
    session_config = build_accept_payload(
        tenant_id=tenant_id,
        call_id=call_id,
        voice=resolve_realtime_voice(getattr(agent, "voice", None)),
        preferred_language=preferred_language,
        instructions=getattr(agent, "system_instructions", None) or None,
    )
    await calls.ensure_conversation(call_id, language=language_state.call_language or "unknown")
    await calls.add_event(
        call_id,
        "openai.accept_succeeded",
        {
            "openai_call_id": openai_call_id,
            "model": model,
            "preferred_language": pref_label,
        },
    )
    await start_sideband_monitor(
        tenant_id=tenant_id,
        call_id=call_id,
        openai_call_id=openai_call_id,
        initial_greeting=initial_greeting,
        language_state=language_state,
        session_instructions=str(session_config["instructions"]),
    )
    schedule_start_recording(tenant_id=tenant_id, call_id=call_id)


async def _caller_preferred_language(
    db: AsyncSession,
    customer_id: UUID | None,
) -> str | None:
    """Read existing Customer.language / metadata preferred_language if present.

    Does not create CRM fields. New contacts default to roman_urdu in DB, which
    normalize_preferred_language treats as unknown → bilingual greeting.
    """
    if customer_id is None:
        return None
    result = await db.execute(select(Customer).where(Customer.id == customer_id))
    customer = result.scalar_one_or_none()
    if customer is None:
        return None
    meta = customer.metadata_json if isinstance(customer.metadata_json, dict) else {}
    meta_lang = meta.get("preferred_language")
    if isinstance(meta_lang, str) and meta_lang.strip():
        return meta_lang
    return customer.language


def parse_incoming_for_tests(event: dict[str, Any], webhook_id: str | None = None) -> IncomingSipCall:
    """Test helper re-export."""
    return parse_realtime_incoming_event(event, webhook_id=webhook_id)
