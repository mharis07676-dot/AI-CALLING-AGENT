"""Sideband Realtime WebSocket monitor for accepted SIP calls.

Attaches to an already-accepted OpenAI Realtime call — does not create a
second independent conversation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

from app.ai.tools import ToolExecutor
from app.ai.voice_agent_prompt import VOICE_AGENT_SYSTEM_PROMPT
from app.config import get_settings
from app.db.session import AsyncSessionLocal
from app.models import CallStatus
from app.services import CallService
from app.voice.call_lifecycle import CallLifecycle, lifecycle_from_call, log_call_event, redact_secrets
from app.voice.concurrency import concurrency_slot_held
from app.voice.idempotency import claim_idempotency
from app.voice.language_control import (
    CallLanguageState,
    apply_language_control,
    observe_caller_transcript,
)
from app.voice.handoff import (
    HANDOFF_STATUS_CONNECTED,
    HANDOFF_STATUS_DIALING,
    HANDOFF_STATUS_REQUESTED,
    is_ai_silenced,
    mark_ai_silenced,
    redirect_active_call,
)
from app.voice.realtime import (
    BILINGUAL_GREETING,
    build_language_session_update,
    build_turn_detection,
    greeting_speak_instructions,
    openai_auth_headers,
    realtime_sideband_url,
)
from app.voice.realtime_forensics import (
    drop_forensics,
    get_or_create_forensics,
    log_sideband_forensics,
)
from app.voice.recording import schedule_finalize_recording

logger = logging.getLogger(__name__)

# In-process guard against duplicate sideband tasks for the same OpenAI call.
_active_monitors: dict[str, asyncio.Task[None]] = {}

# Five attempts, then 0.4 + 0.8 + 1.6 + 3.2 seconds of backoff (~6s, under 8s).
MAX_WS_RETRIES = 5
RETRY_DELAY_SECONDS = 0.4
WS_OPEN_TIMEOUT_SECONDS = 10.0
# 404 right after accept is often "session not ready yet", not a dead call.
_RETRYABLE_HTTP_STATUSES = frozenset({404, 409, 425, 500, 502, 503, 504})

# GA + legacy Realtime audio-delta event names.
_AUDIO_DELTA_EVENTS = frozenset(
    {
        "response.output_audio.delta",
        "response.audio.delta",
    }
)
_ASSISTANT_TRANSCRIPT_DONE = frozenset(
    {
        "response.output_audio_transcript.done",
        "response.audio_transcript.done",
    }
)


def _percentile(samples: list[int], pct: float) -> int | None:
    if not samples:
        return None
    ordered = sorted(samples)
    rank = max(1, round(pct / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _language_label(state: CallLanguageState | None) -> str:
    if state is None or not state.call_language:
        return "unknown"
    if state.call_language == "en":
        return "english"
    if state.call_language == "ur":
        return "urdu"
    return str(state.call_language)


class _LatencyProbe:
    """Per-turn timing for SIP Realtime calls (sideband-observed).

    SIP media is OpenAI↔telephony; we do not invent telephony egress latency.
    First audio delta is the earliest observable model-audio signal.
    Timestamps are monotonic and are never logged raw.
    """

    def __init__(self, *, tenant_id: UUID | None = None, call_id: UUID | None = None) -> None:
        self.tenant_id = tenant_id
        self.call_id = call_id
        self.turn_number = 0
        self.language = "unknown"
        self.turns: list[dict[str, Any]] = []
        self._reset_turn_marks()

    def _reset_turn_marks(self) -> None:
        self.speech_started_at: float | None = None
        self.speech_stopped_at: float | None = None
        self.turn_committed_at: float | None = None
        self.transcript_final_at: float | None = None
        self.response_create_sent_at: float | None = None
        self.response_created_at: float | None = None
        self.first_audio_delta_at: float | None = None
        self.response_done_at: float | None = None
        self.logged = False
        self.tool_used = False
        self.wait_for_user_used = False
        self.tool_name: str | None = None
        self.tool_call_started_at: float | None = None
        self.tool_handler_started_at: float | None = None
        self.tool_handler_completed_at: float | None = None
        self.tool_output_sent_at: float | None = None
        self.post_tool_response_created_at: float | None = None
        self.post_tool_first_audio_at: float | None = None
        self.tool_started_at: float | None = None
        self.tool_latency_ms: int | None = None
        self.tool_execution_ms: int | None = None
        self.speech_duration_ms: int | None = None
        self.vad_end_delay_ms: int | None = None
        self.response_start_delay_ms: int | None = None
        self.model_first_audio_ms: int | None = None
        self.vad_to_response_ms: int | None = None
        self.response_to_first_audio_ms: int | None = None
        self.turn_end_to_first_audio_ms: int | None = None
        self.vad_to_transcript_ms: int | None = None
        self._awaiting_post_tool_response = False
        self._awaiting_post_tool_audio = False

    def _ms(self, start: float | None, end: float | None) -> int | None:
        if start is None or end is None:
            return None
        return round((end - start) * 1000)

    def _common(self) -> dict[str, Any]:
        settings = get_settings()
        return {
            "tenant_id": self.tenant_id,
            "call_id": self.call_id,
            "turn_number": self.turn_number,
            "language": self.language,
            "tool_used": self.tool_used,
            "wait_for_user_used": self.wait_for_user_used,
            "vad_type": settings.voice_vad_mode,
            "reasoning_effort": settings.voice_reasoning_effort,
        }

    def _remember_turn(self) -> None:
        if self.turn_end_to_first_audio_ms is None or self.turn_number <= 0:
            return
        row = {
            "turn_number": self.turn_number,
            "total_turn_latency_ms": self.turn_end_to_first_audio_ms,
            "tool_used": self.tool_used,
            "wait_for_user_used": self.wait_for_user_used,
            "tool_execution_ms": self.tool_execution_ms,
            "language": self.language,
        }
        self.turns = [item for item in self.turns if item["turn_number"] != self.turn_number]
        self.turns.append(row)

    def on_speech_started(self) -> None:
        self.turn_number += 1
        self._reset_turn_marks()
        self.speech_started_at = time.perf_counter()
        log_call_event("USER_SPEECH_STARTED", **self._common())

    def on_speech_stopped(self) -> None:
        self.speech_stopped_at = time.perf_counter()
        self.speech_duration_ms = self._ms(self.speech_started_at, self.speech_stopped_at)
        log_call_event(
            "USER_SPEECH_STOPPED",
            **self._common(),
            speech_duration_ms=self.speech_duration_ms,
        )

    def on_turn_committed(self) -> None:
        self.turn_committed_at = time.perf_counter()
        self.vad_end_delay_ms = self._ms(self.speech_stopped_at, self.turn_committed_at)
        log_call_event("TURN_COMMITTED", **self._common(), vad_end_delay_ms=self.vad_end_delay_ms)

    def on_transcript_final(self) -> None:
        self.transcript_final_at = time.perf_counter()
        self.vad_to_transcript_ms = self._ms(self.speech_stopped_at, self.transcript_final_at)
        log_call_event(
            "TRANSCRIPT_FINAL",
            **self._common(),
            vad_to_transcript_ms=self.vad_to_transcript_ms,
        )

    def on_response_create_sent(self) -> None:
        self.response_create_sent_at = time.perf_counter()
        log_call_event("RESPONSE_CREATE", **self._common())

    def on_response_created(self) -> None:
        now = time.perf_counter()
        if self._awaiting_post_tool_response and self.post_tool_response_created_at is None:
            self.post_tool_response_created_at = now
            self._awaiting_post_tool_response = False
            log_call_event("POST_TOOL_RESPONSE_CREATED", **self._common(), tool=self.tool_name)
            return
        if self.response_created_at is not None:
            return
        self.response_created_at = now
        self.vad_to_response_ms = self._ms(self.speech_stopped_at, now)
        self.response_start_delay_ms = self._ms(self.turn_committed_at, now)
        log_call_event(
            "RESPONSE_CREATED",
            **self._common(),
            response_start_delay_ms=self.response_start_delay_ms,
            turn_end_to_response_created_ms=self.vad_to_response_ms,
        )

    def on_tool_call_started(self, tool_name: str) -> None:
        self.tool_used = True
        self.tool_name = tool_name
        self.wait_for_user_used = tool_name == "wait_for_user"
        self.tool_call_started_at = time.perf_counter()
        log_call_event("TOOL_CALL_STARTED", **self._common(), tool=tool_name)

    def on_tool_handler_started(self) -> None:
        self.tool_handler_started_at = time.perf_counter()
        self.tool_started_at = self.tool_handler_started_at
        log_call_event("TOOL_HANDLER_STARTED", **self._common(), tool=self.tool_name)

    def on_tool_handler_completed(self) -> None:
        self.tool_handler_completed_at = time.perf_counter()
        self.tool_execution_ms = self._ms(self.tool_handler_started_at, self.tool_handler_completed_at)
        self.tool_latency_ms = self.tool_execution_ms
        log_call_event(
            "TOOL_HANDLER_COMPLETED",
            **self._common(),
            tool=self.tool_name,
            tool_execution_ms=self.tool_execution_ms,
        )
        self._remember_turn()

    def on_tool_output_sent(self) -> None:
        self.tool_output_sent_at = time.perf_counter()
        self._awaiting_post_tool_response = True
        self._awaiting_post_tool_audio = True
        log_call_event("TOOL_OUTPUT_SENT", **self._common(), tool=self.tool_name)

    def on_tool_started(self, *, wait_for_user: bool = False) -> None:
        """Back-compat for tests that only mark a tool as started."""
        self.on_tool_call_started("wait_for_user" if wait_for_user else (self.tool_name or "tool"))
        self.on_tool_handler_started()

    def on_tool_finished(self) -> None:
        if self.tool_handler_completed_at is None:
            self.on_tool_handler_completed()

    def on_first_audio_delta(self) -> None:
        now = time.perf_counter()
        if self._awaiting_post_tool_audio and self.post_tool_first_audio_at is None:
            self.post_tool_first_audio_at = now
            self._awaiting_post_tool_audio = False
            log_call_event(
                "POST_TOOL_FIRST_AUDIO",
                **self._common(),
                tool=self.tool_name,
                post_tool_first_audio_ms=self._ms(self.tool_output_sent_at, now),
            )
            if self.first_audio_delta_at is not None:
                return
        if self.first_audio_delta_at is not None:
            return
        self.first_audio_delta_at = now
        self.model_first_audio_ms = self._ms(self.response_created_at, now)
        self.response_to_first_audio_ms = self.model_first_audio_ms
        self.turn_end_to_first_audio_ms = self._ms(self.speech_stopped_at, now)
        log_call_event(
            "FIRST_RESPONSE_AUDIO_DELTA",
            **self._common(),
            model_first_audio_ms=self.model_first_audio_ms,
            total_turn_latency_ms=self.turn_end_to_first_audio_ms,
            vad_end_delay_ms=self.vad_end_delay_ms,
            response_start_delay_ms=self.response_start_delay_ms,
        )
        self.logged = True
        self._remember_turn()

    def on_response_done(self) -> None:
        self.response_done_at = time.perf_counter()
        self._remember_turn()
        log_call_event(
            "RESPONSE_DONE",
            **self._common(),
            total_turn_latency_ms=self.turn_end_to_first_audio_ms,
            tool_execution_ms=self.tool_execution_ms,
        )

    def log_call_summary(self) -> None:
        rows = list(self.turns)
        if not rows:
            return
        all_ms = [int(row["total_turn_latency_ms"]) for row in rows]
        normal = [int(row["total_turn_latency_ms"]) for row in rows if not row["tool_used"]]
        tools = [int(row["total_turn_latency_ms"]) for row in rows if row["tool_used"]]
        slowest = max(rows, key=lambda row: int(row["total_turn_latency_ms"]))
        settings = get_settings()
        log_call_event(
            "CALL_LATENCY_SUMMARY",
            tenant_id=self.tenant_id,
            call_id=self.call_id,
            p50_total_turn_latency_ms=_percentile(all_ms, 50),
            p95_total_turn_latency_ms=_percentile(all_ms, 95),
            slowest_turn_ms=slowest["total_turn_latency_ms"],
            slowest_turn_number=slowest["turn_number"],
            tool_turns=len(tools),
            normal_turns=len(normal),
            no_tool_p50_ms=_percentile(normal, 50),
            no_tool_p95_ms=_percentile(normal, 95),
            tool_p50_ms=_percentile(tools, 50),
            tool_p95_ms=_percentile(tools, 95),
            vad_type=settings.voice_vad_mode,
            reasoning_effort=settings.voice_reasoning_effort,
        )

    @property
    def eos_to_created_ms(self) -> int | None:
        return self.vad_to_response_ms

    @property
    def created_to_delta_ms(self) -> int | None:
        return self.response_to_first_audio_ms

    @property
    def eos_to_first_audio_ms(self) -> int | None:
        return self.turn_end_to_first_audio_ms


def _safe_json_loads(raw: str) -> dict[str, Any] | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def start_sideband_monitor(
    *,
    tenant_id: UUID,
    call_id: UUID,
    openai_call_id: str,
    initial_greeting: str | None = None,
    language_state: CallLanguageState | None = None,
    session_instructions: str | None = None,
) -> bool:
    """Start a background sideband monitor if one is not already running."""
    existing = _active_monitors.get(openai_call_id)
    if existing is not None and not existing.done():
        logger.info("Sideband already active for openai_call_id=%s", openai_call_id)
        return False

    task = asyncio.create_task(
        _monitor_with_retries(
            tenant_id=tenant_id,
            call_id=call_id,
            openai_call_id=openai_call_id,
            initial_greeting=initial_greeting or BILINGUAL_GREETING,
            language_state=language_state or CallLanguageState(),
            session_instructions=session_instructions,
        ),
        name=f"realtime-sideband-{openai_call_id}",
    )
    _active_monitors[openai_call_id] = task
    task.add_done_callback(lambda _: _active_monitors.pop(openai_call_id, None))
    return True


def _http_status_code(exc: InvalidStatus) -> int | None:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _http_body_preview(exc: InvalidStatus) -> str | None:
    response = getattr(exc, "response", None)
    if response is None:
        return None
    body = getattr(response, "body", None)
    if body is None:
        body = getattr(response, "text", None)
    if body is None:
        return None
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="ignore")
    if not isinstance(body, str):
        return None
    return redact_secrets(body, limit=200)


async def _emit_disconnect_forensics(
    *,
    tenant_id: UUID,
    call_id: UUID,
    openai_call_id: str,
    reason: str,
    attached: bool,
    reconnect_http_status: int | None = None,
    reconnect_body_preview: str | None = None,
    ws_close_code: int | None = None,
    ws_close_reason: str | None = None,
) -> None:
    lifecycle_state = None
    twilio_call_sid = None
    hangup_flag = None
    try:
        async with AsyncSessionLocal() as db:
            call = await CallService(db, tenant_id).get(call_id)
            if call is not None:
                lifecycle_state = lifecycle_from_call(call).value
                twilio_call_sid = getattr(call, "provider_call_id", None)
                meta = call.metadata_json if isinstance(call.metadata_json, dict) else {}
                hangup_flag = meta.get("was_hangup_requested_by_backend")
    except Exception:  # noqa: BLE001 - forensics must not break reconnect
        logger.exception("SIDEBAND_FORENSIC call lookup failed call_id=%s", call_id)

    slot_held = await concurrency_slot_held(tenant_id=tenant_id, call_id=call_id)
    log_sideband_forensics(
        reason=reason,
        tenant_id=tenant_id,
        call_id=call_id,
        openai_call_id=openai_call_id,
        lifecycle_state=lifecycle_state,
        twilio_call_sid=twilio_call_sid,
        was_hangup_requested_by_backend=hangup_flag if isinstance(hangup_flag, bool) else None,
        concurrency_slot_held=slot_held,
        attached=attached,
        reconnect_http_status=reconnect_http_status,
        reconnect_body_preview=reconnect_body_preview,
        ws_close_code=ws_close_code,
        ws_close_reason=ws_close_reason,
    )


async def _call_was_answered(*, tenant_id: UUID, call_id: UUID) -> bool:
    """True once the caller is on a live session. Those calls must not be hung up."""
    async with AsyncSessionLocal() as db:
        call = await CallService(db, tenant_id).get(call_id)
        if call is None:
            return False
        return call.answered_at is not None or call.status == CallStatus.ACTIVE


async def _call_is_terminal(*, tenant_id: UUID, call_id: UUID) -> bool:
    async with AsyncSessionLocal() as db:
        call = await CallService(db, tenant_id).get(call_id)
        if call is None:
            return True
        if call.status in {
            CallStatus.COMPLETED,
            CallStatus.FAILED,
            CallStatus.REJECTED,
            CallStatus.TRANSFERRED,
        }:
            return True
        return getattr(call, "lifecycle_state", None) in {"ENDED", "FAILED"}


async def _retry_sleep(attempt: int) -> None:
    base = RETRY_DELAY_SECONDS * (2 ** (attempt - 1))
    if base <= 0:
        return
    await asyncio.sleep(min(base * (0.5 + random.random()), 2.0))


async def _mark_control_disconnected(
    *,
    tenant_id: UUID,
    call_id: UUID,
    openai_call_id: str,
    reason: str,
) -> None:
    """Record a control-channel drop. Does not hang up the SIP call."""
    log_call_event(
        "SIDEBAND_DISCONNECTED",
        tenant_id=tenant_id,
        call_id=call_id,
        openai_call_id=openai_call_id,
        sideband_disconnect_reason=reason,
    )
    try:
        async with AsyncSessionLocal() as db:
            calls = CallService(db, tenant_id)
            call = await calls.get(call_id)
            if call is None or call.status in {
                CallStatus.COMPLETED,
                CallStatus.FAILED,
                CallStatus.REJECTED,
                CallStatus.TRANSFERRED,
            }:
                return
            meta = dict(call.metadata_json or {})
            meta["control_channel"] = "disconnected"
            meta["sideband_disconnect_reason"] = reason[:300]
            meta["sideband_disconnected_at"] = datetime.now(timezone.utc).isoformat()
            await calls.set_status(call_id, call.status, metadata_json=meta)
            await db.commit()
    except Exception:  # noqa: BLE001 - disconnect bookkeeping must not end the call
        logger.exception("SIDEBAND_DISCONNECTED persist failed call_id=%s", call_id)


async def _handoff_owns_leg(*, tenant_id: UUID, call_id: UUID) -> bool:
    """Dial owns the caller. Reconnecting the sideband must not end that leg."""
    async with AsyncSessionLocal() as db:
        call = await CallService(db, tenant_id).get(call_id)
        if call is None:
            return False
        return bool(
            getattr(call, "handoff_requested", False)
            and call.handoff_status
            in {
                HANDOFF_STATUS_REQUESTED,
                HANDOFF_STATUS_DIALING,
                HANDOFF_STATUS_CONNECTED,
            }
        )


async def _monitor_with_retries(
    *,
    tenant_id: UUID,
    call_id: UUID,
    openai_call_id: str,
    initial_greeting: str = BILINGUAL_GREETING,
    language_state: CallLanguageState | None = None,
    session_instructions: str | None = None,
) -> None:
    # Set as soon as the WebSocket handshake succeeds. A later tool or DB error
    # must not be recorded as "never attached" — the caller is already talking.
    attached: dict[str, bool] = {"ok": False}
    greeted: dict[str, bool] = {"ok": False}
    last_disconnect = "not_connected"
    confirmed_session_gone = False
    forensics = get_or_create_forensics(
        tenant_id=tenant_id,
        call_id=call_id,
        openai_call_id=openai_call_id,
    )
    for attempt in range(1, MAX_WS_RETRIES + 1):
        if await _call_is_terminal(tenant_id=tenant_id, call_id=call_id):
            return
        try:
            await _run_sideband_session(
                tenant_id=tenant_id,
                call_id=call_id,
                openai_call_id=openai_call_id,
                attached=attached,
                initial_greeting=initial_greeting,
                language_state=language_state or CallLanguageState(),
                session_instructions=session_instructions,
                send_greeting=not greeted["ok"],
                greeted=greeted,
                forensics=forensics,
            )
            last_disconnect = "websocket_closed"
            await _emit_disconnect_forensics(
                tenant_id=tenant_id,
                call_id=call_id,
                openai_call_id=openai_call_id,
                reason=last_disconnect,
                attached=attached["ok"],
                ws_close_code=forensics.ws_close_code,
                ws_close_reason=forensics.ws_close_reason,
            )
        except ConnectionClosed as exc:
            last_disconnect = "websocket_closed"
            rcvd = getattr(exc, "rcvd", None)
            code = getattr(rcvd, "code", None)
            if code is None:
                code = getattr(exc, "code", None)
            reason = getattr(rcvd, "reason", None)
            if reason is None:
                reason = getattr(exc, "reason", None)
            forensics.note_ws_close(
                code=int(code) if code is not None else None,
                reason=str(reason) if reason else None,
            )
            log_call_event(
                "SIDEBAND_DISCONNECTED",
                tenant_id=tenant_id,
                call_id=call_id,
                openai_call_id=openai_call_id,
                attempt=attempt,
                attached=attached["ok"],
                ws_close_code=code,
                ws_close_reason=reason,
            )
            await _emit_disconnect_forensics(
                tenant_id=tenant_id,
                call_id=call_id,
                openai_call_id=openai_call_id,
                reason=last_disconnect,
                attached=attached["ok"],
                ws_close_code=forensics.ws_close_code,
                ws_close_reason=forensics.ws_close_reason,
            )
        except InvalidStatus as exc:
            status = _http_status_code(exc)
            body_preview = _http_body_preview(exc)
            last_disconnect = f"http_{status}"
            forensics.note_reconnect_http(status=status, body_preview=body_preview)
            log_call_event(
                "SIDEBAND_DISCONNECTED",
                tenant_id=tenant_id,
                call_id=call_id,
                openai_call_id=openai_call_id,
                status=status,
                attached=attached["ok"],
                project_header_set=bool((get_settings().openai_sip_project_id or "").strip()),
                reconnect_body_preview=body_preview,
            )
            await _emit_disconnect_forensics(
                tenant_id=tenant_id,
                call_id=call_id,
                openai_call_id=openai_call_id,
                reason=last_disconnect,
                attached=attached["ok"],
                reconnect_http_status=status,
                reconnect_body_preview=body_preview,
            )
            # 404 after a successful attach means OpenAI has no session left.
            # Mark the row ended. Do not hang up — the session is already gone,
            # and a false 404 must not tear down a live SIP leg.
            if status == 404 and attached["ok"]:
                await _mark_session_gone_terminal(
                    tenant_id=tenant_id,
                    call_id=call_id,
                    openai_call_id=openai_call_id,
                    reason="sideband_http_404",
                )
                return
            if status == 404:
                # Pre-attach 404 is often "not ready yet". Only remember it for
                # the post-retry terminal path — do not publish session-gone yet.
                confirmed_session_gone = True
            if not attached["ok"] and status not in _RETRYABLE_HTTP_STATUSES:
                await _mark_control_disconnected(
                    tenant_id=tenant_id,
                    call_id=call_id,
                    openai_call_id=openai_call_id,
                    reason=last_disconnect,
                )
                break
        except Exception:  # noqa: BLE001 - keep monitor failures contained
            last_disconnect = "sideband_error"
            log_call_event(
                "SIDEBAND_DISCONNECTED",
                tenant_id=tenant_id,
                call_id=call_id,
                openai_call_id=openai_call_id,
                attempt=attempt,
                attached=attached["ok"],
                sideband_disconnect_reason=last_disconnect,
            )
            logger.exception(
                "SIDEBAND_DISCONNECTED openai_call_id=%s attempt=%s attached=%s",
                openai_call_id,
                attempt,
                attached["ok"],
            )
            await _emit_disconnect_forensics(
                tenant_id=tenant_id,
                call_id=call_id,
                openai_call_id=openai_call_id,
                reason=last_disconnect,
                attached=attached["ok"],
            )

        await _mark_control_disconnected(
            tenant_id=tenant_id,
            call_id=call_id,
            openai_call_id=openai_call_id,
            reason=last_disconnect,
        )

        # Handoff owns the phone leg. Reconnecting would fight Dial.
        if attached["ok"] and await _handoff_owns_leg(tenant_id=tenant_id, call_id=call_id):
            logger.info(
                "Sideband stopping; handoff owns call_id=%s openai_call_id=%s",
                call_id,
                openai_call_id,
            )
            return

        if await _call_is_terminal(tenant_id=tenant_id, call_id=call_id):
            return

        if attempt < MAX_WS_RETRIES:
            log_call_event(
                "SIDEBAND_RECONNECTING",
                tenant_id=tenant_id,
                call_id=call_id,
                openai_call_id=openai_call_id,
                attempt=attempt + 1,
                sideband_disconnect_reason=last_disconnect,
            )
            await _retry_sleep(attempt)

    if confirmed_session_gone and not attached["ok"]:
        await _mark_session_gone_terminal(
            tenant_id=tenant_id,
            call_id=call_id,
            openai_call_id=openai_call_id,
            reason="sideband_http_404",
        )
        return

    # Retries ended and the session was not confirmed gone. Leave SIP up.
    logger.warning(
        "SIDEBAND_DISCONNECTED retries exhausted; SIP call left up openai_call_id=%s attached=%s",
        openai_call_id,
        attached["ok"],
    )
    await _emit_disconnect_forensics(
        tenant_id=tenant_id,
        call_id=call_id,
        openai_call_id=openai_call_id,
        reason="retries_exhausted",
        attached=attached["ok"],
    )


async def _run_sideband_session(
    *,
    tenant_id: UUID,
    call_id: UUID,
    openai_call_id: str,
    attached: dict[str, bool] | None = None,
    initial_greeting: str = BILINGUAL_GREETING,
    language_state: CallLanguageState | None = None,
    session_instructions: str | None = None,
    send_greeting: bool = True,
    greeted: dict[str, bool] | None = None,
    forensics: Any | None = None,
) -> None:
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    url = realtime_sideband_url(openai_call_id)
    # WebSocket handshake must not send Content-Type.
    headers = openai_auth_headers(content_type=None)
    probe = forensics or get_or_create_forensics(
        tenant_id=tenant_id,
        call_id=call_id,
        openai_call_id=openai_call_id,
    )
    logger.info(
        "Connecting Realtime sideband openai_call_id=%s project_header_set=%s",
        openai_call_id,
        bool(headers.get("OpenAI-Project")),
    )

    async with websockets.connect(
        url,
        additional_headers=headers,
        max_size=8 * 1024 * 1024,
        open_timeout=WS_OPEN_TIMEOUT_SECONDS,
    ) as ws:
        if attached is not None:
            attached["ok"] = True
        probe.mark_attached()
        log_call_event(
            "SIDEBAND_CONNECTED",
            tenant_id=tenant_id,
            call_id=call_id,
            openai_call_id=openai_call_id,
        )
        # Promote RINGING → ACTIVE only after the control channel is live.
        async with AsyncSessionLocal() as db:
            calls = CallService(db, tenant_id)
            call = await calls.get(call_id)
            if call is not None and call.status not in {
                CallStatus.COMPLETED,
                CallStatus.FAILED,
                CallStatus.REJECTED,
                CallStatus.TRANSFERRED,
            }:
                meta = dict(call.metadata_json or {})
                meta["control_channel"] = "connected"
                meta.pop("sideband_disconnected_at", None)
                meta["sideband_connected_at"] = datetime.now(timezone.utc).isoformat()
                extra: dict[str, Any] = {"metadata_json": meta, "openai_session_id": openai_call_id}
                if call.status == CallStatus.RINGING:
                    extra["answered_at"] = datetime.now(timezone.utc)
                    await calls.set_status(call_id, CallStatus.ACTIVE, **extra)
                else:
                    await calls.set_status(call_id, call.status, **extra)
            await db.commit()

        # First attach only: apply tenant instructions, then speak once.
        # Manual response.create #1: the greeting is not a caller turn, so VAD
        # create_response cannot start it. A reconnect must not repeat it.
        if send_greeting and session_instructions:
            await ws.send(json.dumps(build_language_session_update(session_instructions)))
        if send_greeting:
            await ws.send(
                json.dumps(
                    {
                        "type": "response.create",
                        "response": {
                            "instructions": greeting_speak_instructions(initial_greeting),
                        },
                    }
                )
            )
            if greeted is not None:
                greeted["ok"] = True

        latency = _LatencyProbe(tenant_id=tenant_id, call_id=call_id)
        call_language = language_state or CallLanguageState()
        latency.language = _language_label(call_language)
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="ignore")
                event = _safe_json_loads(raw)
                if event is None:
                    continue
                probe.observe(event)
                try:
                    await _handle_event(
                        ws=ws,
                        event=event,
                        tenant_id=tenant_id,
                        call_id=call_id,
                        openai_call_id=openai_call_id,
                        latency=latency,
                        language_state=call_language,
                        session_instructions=session_instructions,
                    )
                except Exception:  # noqa: BLE001 - one bad event must not drop a live call
                    logger.exception(
                        "Realtime sideband event failed openai_call_id=%s type=%s",
                        openai_call_id,
                        event.get("type"),
                    )
        finally:
            latency.log_call_summary()


async def _handle_event(
    *,
    ws: Any,
    event: dict[str, Any],
    tenant_id: UUID,
    call_id: UUID,
    openai_call_id: str,
    latency: _LatencyProbe | None = None,
    language_state: CallLanguageState | None = None,
    session_instructions: str | None = None,
) -> None:
    event_type = str(event.get("type") or "")
    event_id = str(event.get("event_id") or event.get("id") or "")

    # After handoff starts, keep the caller leg alive but stop all AI speech/tools.
    if is_ai_silenced(call_id):
        if event_type in {
            "response.function_call_arguments.done",
            "response.created",
            "input_audio_buffer.speech_stopped",
        }:
            try:
                await ws.send(json.dumps({"type": "response.cancel"}))
            except Exception:  # noqa: BLE001
                pass
        return

    if latency is not None:
        if language_state is not None:
            latency.language = _language_label(language_state)
        if event_type == "input_audio_buffer.speech_started":
            latency.on_speech_started()
        elif event_type == "input_audio_buffer.speech_stopped":
            latency.on_speech_stopped()
        elif event_type == "input_audio_buffer.committed":
            latency.on_turn_committed()
        elif event_type == "response.created":
            latency.on_response_created()
        elif event_type in _AUDIO_DELTA_EVENTS:
            latency.on_first_audio_delta()

    if event_type in {"session.created", "session.updated"}:
        async with AsyncSessionLocal() as db:
            calls = CallService(db, tenant_id)
            await calls.add_event(call_id, event_type, {"openai_call_id": openai_call_id})
            await db.commit()
        return

    if event_type == "conversation.item.input_audio_transcription.completed":
        if latency is not None:
            latency.on_transcript_final()
        transcript = str(event.get("transcript") or "").strip()
        if not transcript:
            return

        # Language observe is in-memory and cheap — never an extra LLM call.
        # Instruction updates go out before the transcript DB write so persistence
        # cannot delay the next turn's language. Spoken audio does not wait on either.
        language_changed = False
        if language_state is not None:
            for line in observe_caller_transcript(language_state, transcript):
                logger.info("%s", line)
                if line.startswith("LANGUAGE_LOCKED") or line.startswith(
                    "LANGUAGE_SWITCH_CONFIRMED"
                ):
                    language_changed = True
        if language_changed and language_state is not None:
            if latency is not None:
                latency.language = _language_label(language_state)
            rendered = apply_language_control(
                session_instructions or VOICE_AGENT_SYSTEM_PROMPT,
                language_state,
            )
            await ws.send(json.dumps(build_language_session_update(rendered)))
            logger.info(
                "REALTIME_LANGUAGE_INSTRUCTIONS_UPDATED language=%s",
                language_state.call_language,
            )

        async with AsyncSessionLocal() as db:
            if event_id:
                claimed = await claim_idempotency(
                    db,
                    scope="openai_transcript",
                    key=event_id,
                    tenant_id=tenant_id,
                    call_id=call_id,
                )
                if not claimed:
                    await db.commit()
                    return
            calls = CallService(db, tenant_id)
            await calls.add_message(call_id, role="user", content=transcript)
            if language_changed and language_state is not None and language_state.call_language:
                await calls.set_conversation_language(call_id, language_state.call_language)
            await db.commit()
        return

    if event_type in _ASSISTANT_TRANSCRIPT_DONE:
        transcript = str(event.get("transcript") or "").strip()
        if not transcript:
            return
        async with AsyncSessionLocal() as db:
            if event_id:
                claimed = await claim_idempotency(
                    db,
                    scope="openai_transcript",
                    key=event_id,
                    tenant_id=tenant_id,
                    call_id=call_id,
                )
                if not claimed:
                    await db.commit()
                    return
            calls = CallService(db, tenant_id)
            await calls.add_message(call_id, role="assistant", content=transcript)
            await db.commit()
        return

    if event_type == "response.function_call_arguments.done":
        await _handle_tool_call(
            ws=ws,
            event=event,
            tenant_id=tenant_id,
            call_id=call_id,
            openai_call_id=openai_call_id,
            latency=latency,
        )
        return

    if event_type == "error":
        async with AsyncSessionLocal() as db:
            calls = CallService(db, tenant_id)
            err = event.get("error") if isinstance(event.get("error"), dict) else {}
            await calls.add_event(
                call_id,
                "realtime.error",
                {
                    "code": err.get("code"),
                    "type": err.get("type"),
                    "message": str(err.get("message") or "")[:300],
                },
            )
            await db.commit()
        return

    if event_type in {"response.done", "conversation.item.completed"}:
        if latency is not None and event_type == "response.done":
            latency.on_response_done()
        return


async def _handle_tool_call(
    *,
    ws: Any,
    event: dict[str, Any],
    tenant_id: UUID,
    call_id: UUID,
    openai_call_id: str = "",
    latency: _LatencyProbe | None = None,
) -> None:
    tool_call_id = str(event.get("call_id") or "")
    tool_name = str(event.get("name") or "")
    arguments_raw = event.get("arguments") or "{}"
    event_id = str(event.get("event_id") or "")

    if not tool_call_id or not tool_name:
        return

    if latency is not None:
        latency.on_tool_call_started(tool_name)

    dedupe_key = event_id or tool_call_id
    try:
        arguments = json.loads(arguments_raw) if isinstance(arguments_raw, str) else dict(arguments_raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}

    # Never allow model-supplied tenant_id to override server tenant.
    arguments.pop("tenant_id", None)
    # Prefer server call id for persistence-bound tools.
    if "call_id" in arguments:
        arguments["call_id"] = str(call_id)

    async with AsyncSessionLocal() as db:
        claimed = await claim_idempotency(
            db,
            scope="openai_tool_call",
            key=dedupe_key,
            tenant_id=tenant_id,
            call_id=call_id,
        )
        if not claimed:
            await db.commit()
            return

        executor = ToolExecutor(db, tenant_id)
        if latency is not None:
            latency.on_tool_handler_started()
        result = await executor.execute(
            tool_name,
            arguments,
            call_id=call_id,
            provider_tool_call_id=tool_call_id,
        )
        await db.commit()
        if latency is not None:
            latency.on_tool_handler_completed()

    output_payload = {
        "success": result.success,
        "speakable_summary": result.speakable_summary,
        "data": result.data,
        "error": result.error,
    }
    await ws.send(
        json.dumps(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": tool_call_id,
                    "output": json.dumps(output_payload),
                },
            }
        )
    )
    if latency is not None:
        latency.on_tool_output_sent()

    # Manual response.create paths (ordinary caller turns use create_response=true):
    # 1. call-start greeting (sideband attach)
    # 2. wait_for_user — close the function call with silence, no spoken audio
    # 3. human handoff — one confirmation sentence, then Twilio Dial
    # 4. any other tool result — GA does not auto-respond after function_call_output
    # Silent listen tool: complete the function cycle without spoken audio.
    if tool_name == "wait_for_user":
        log_call_event(
            "WAIT_FOR_USER",
            tenant_id=tenant_id,
            call_id=call_id,
            openai_call_id=openai_call_id,
            tool_latency_ms=latency.tool_latency_ms if latency else None,
        )
        if latency is not None:
            latency.on_response_create_sent()
        await ws.send(
            json.dumps(
                {
                    "type": "response.create",
                    "response": {
                        "instructions": (
                            "Remain completely silent. Do not speak. "
                            "Do not apologize. End this turn now and keep listening."
                        ),
                    },
                }
            )
        )
        return

    initiate = bool(
        result.success
        and isinstance(result.data, dict)
        and result.data.get("initiate_redirect")
        and tool_name in {"transfer_to_human", "request_human_handoff"}
    )
    if initiate:
        # Brief natural confirmation, then stop AI and redirect the active CallSid.
        if latency is not None:
            latency.on_response_create_sent()
        await ws.send(
            json.dumps(
                {
                    "type": "response.create",
                    "response": {
                        "instructions": (
                            "Say only this brief confirmation, then stop and wait silently: "
                            f'"{result.speakable_summary or "Sure, I\'ll connect you to a representative."}"'
                        ),
                    },
                }
            )
        )
        asyncio.create_task(
            _finish_handoff_after_ai_speaks(
                ws=ws,
                tenant_id=tenant_id,
                call_id=call_id,
                openai_call_id=openai_call_id,
            ),
            name=f"handoff-redirect-{call_id}",
        )
        return

    if latency is not None:
        latency.on_response_create_sent()
    await ws.send(json.dumps({"type": "response.create"}))


async def _finish_handoff_after_ai_speaks(
    *,
    ws: Any,
    tenant_id: UUID,
    call_id: UUID,
    openai_call_id: str,
) -> None:
    """Let the connect phrase play, then silence AI and Dial the human on the same CallSid."""
    # Short delay so the caller hears the confirmation before Twilio Dial takes over.
    await asyncio.sleep(2.5)
    mark_ai_silenced(call_id, True)
    try:
        await ws.send(json.dumps({"type": "response.cancel"}))
    except Exception:  # noqa: BLE001
        logger.info("HANDOFF_AI_CANCEL_SKIPPED call_id=%s", call_id)

    # Disable further auto-responses on this Realtime session if still open.
    try:
        await ws.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "type": "realtime",
                        "tools": [],
                        "tool_choice": "none",
                        "audio": {
                            "input": {
                                "turn_detection": build_turn_detection(create_response=False),
                            }
                        },
                    },
                }
            )
        )
    except Exception:  # noqa: BLE001
        pass

    redirected = await redirect_active_call(tenant_id=tenant_id, call_id=call_id)
    logger.info(
        "HANDOFF_POST_SPEECH_REDIRECT call_id=%s ok=%s error=%s caller_hung_up=%s",
        call_id,
        redirected.get("ok"),
        redirected.get("error"),
        redirected.get("caller_hung_up"),
    )
    # Do not hang up the Twilio caller. Closing the sideband is fine once Dial owns media;
    # OpenAI SIP tears down when Twilio redirects the CallSid.
    _ = openai_call_id


async def _mark_session_gone_terminal(
    *,
    tenant_id: UUID,
    call_id: UUID,
    openai_call_id: str,
    reason: str = "sideband_http_404",
) -> None:
    """Positive evidence OpenAI session is gone. Idempotent terminal cleanup."""
    forensics = get_or_create_forensics(
        tenant_id=tenant_id,
        call_id=call_id,
        openai_call_id=openai_call_id,
    )
    forensics.mark_session_gone()
    await _mark_completed(
        tenant_id=tenant_id,
        call_id=call_id,
        reason=reason,
        openai_session_confirmed_gone=True,
    )
    drop_forensics(openai_call_id)


async def _mark_completed(
    *,
    tenant_id: UUID,
    call_id: UUID,
    reason: str = "caller_or_provider_ended",
    openai_session_confirmed_gone: bool = False,
) -> None:
    async with AsyncSessionLocal() as db:
        calls = CallService(db, tenant_id)
        call = await calls.get(call_id)
        if call is None:
            return
        if call.status in {CallStatus.COMPLETED, CallStatus.FAILED, CallStatus.REJECTED}:
            # Idempotent: still stamp session-gone evidence if newly confirmed.
            if openai_session_confirmed_gone:
                meta = dict(call.metadata_json or {})
                if not meta.get("openai_session_confirmed_gone"):
                    meta["openai_session_confirmed_gone"] = True
                    meta["openai_session_gone_reason"] = reason[:300]
                    await calls.set_status(call_id, call.status, metadata_json=meta)
            await db.commit()
            schedule_finalize_recording(tenant_id=tenant_id, call_id=call_id)
            return
        # During live Dial / connected handoff, sideband close must not mark completed.
        if getattr(call, "handoff_requested", False) and call.handoff_status in {
            HANDOFF_STATUS_REQUESTED,
            HANDOFF_STATUS_DIALING,
            HANDOFF_STATUS_CONNECTED,
        }:
            await calls.add_event(
                call_id,
                "handoff.sideband_closed",
                {"handoff_status": call.handoff_status},
            )
            await db.commit()
            return
        log_call_event(
            "CALL_TERMINATING",
            tenant_id=tenant_id,
            call_id=call_id,
            termination_source="openai_session",
            termination_reason=reason,
            hangup_requested_by_backend=False,
        )
        extra_meta: dict[str, Any] = {}
        if openai_session_confirmed_gone:
            extra_meta["openai_session_confirmed_gone"] = True
            extra_meta["openai_session_gone_reason"] = reason[:300]
        await calls.apply_lifecycle(
            call_id,
            CallLifecycle.ENDED,
            termination_source="openai_session",
            termination_reason=reason,
            sideband_disconnect_reason=reason,
            hangup_requested_by_backend=False,
            failure_reason=None,
            extra_metadata=extra_meta or None,
        )
        await db.commit()
    schedule_finalize_recording(tenant_id=tenant_id, call_id=call_id)


async def _mark_monitor_failed(*, tenant_id: UUID, call_id: UUID, reason: str) -> None:
    async with AsyncSessionLocal() as db:
        calls = CallService(db, tenant_id)
        call = await calls.get(call_id)
        if call is None:
            return
        await calls.add_event(call_id, "realtime.monitor_failed", {"reason": reason})
        # Answered or already-active calls stay up. FAILED is only for a call
        # the caller never joined, so a dead RINGING row does not hold capacity.
        if call.answered_at is not None or call.status == CallStatus.ACTIVE:
            await db.commit()
            return
        if call.status == CallStatus.RINGING:
            await calls.set_status(call_id, CallStatus.FAILED, failure_reason=reason)
        await db.commit()
    schedule_finalize_recording(tenant_id=tenant_id, call_id=call_id)
