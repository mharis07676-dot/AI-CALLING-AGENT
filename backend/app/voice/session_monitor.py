"""Sideband Realtime WebSocket monitor for accepted SIP calls.

Attaches to an already-accepted OpenAI Realtime call — does not create a
second independent conversation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

from app.ai.tools import ToolExecutor
from app.config import get_settings
from app.db.session import AsyncSessionLocal
from app.models import CallStatus
from app.services import CallService
from app.voice.idempotency import claim_idempotency
from app.voice.realtime import (
    BILINGUAL_GREETING,
    greeting_speak_instructions,
    hangup_realtime_call,
    openai_auth_headers,
    realtime_sideband_url,
)

logger = logging.getLogger(__name__)

# In-process guard against duplicate sideband tasks for the same OpenAI call.
_active_monitors: dict[str, asyncio.Task[None]] = {}

MAX_WS_RETRIES = 2
RETRY_DELAY_SECONDS = 0.75

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


class _LatencyProbe:
    """Per-turn timing for SIP Realtime calls (sideband-observed).

    SIP media is OpenAI↔telephony; we do not invent telephony egress latency.
    First ``response.output_audio.delta`` is the earliest observable audio signal.
    """

    __slots__ = (
        "speech_started_at",
        "speech_stopped_at",
        "response_created_at",
        "first_audio_delta_at",
        "logged",
        "eos_to_created_ms",
        "created_to_delta_ms",
        "eos_to_first_audio_ms",
    )

    def __init__(self) -> None:
        self.speech_started_at: float | None = None
        self.speech_stopped_at: float | None = None
        self.response_created_at: float | None = None
        self.first_audio_delta_at: float | None = None
        self.logged = False
        self.eos_to_created_ms: int | None = None
        self.created_to_delta_ms: int | None = None
        self.eos_to_first_audio_ms: int | None = None

    def on_speech_started(self) -> None:
        self.speech_started_at = time.perf_counter()
        self.speech_stopped_at = None
        self.response_created_at = None
        self.first_audio_delta_at = None
        self.logged = False
        self.eos_to_created_ms = None
        self.created_to_delta_ms = None
        self.eos_to_first_audio_ms = None
        logger.info("[VOICE_LATENCY] speech_started")

    def on_speech_stopped(self) -> None:
        self.speech_stopped_at = time.perf_counter()
        logger.info("[VOICE_LATENCY] speech_stopped")

    def on_response_created(self) -> None:
        self.response_created_at = time.perf_counter()
        if self.speech_stopped_at is not None:
            self.eos_to_created_ms = round(
                (self.response_created_at - self.speech_stopped_at) * 1000
            )
        logger.info(
            "[VOICE_LATENCY] response_created eos_to_created_ms=%s",
            self.eos_to_created_ms,
        )

    def on_first_audio_delta(self) -> None:
        if self.first_audio_delta_at is not None:
            return
        self.first_audio_delta_at = time.perf_counter()
        if self.response_created_at is not None:
            self.created_to_delta_ms = round(
                (self.first_audio_delta_at - self.response_created_at) * 1000
            )
        if self.speech_stopped_at is not None:
            self.eos_to_first_audio_ms = round(
                (self.first_audio_delta_at - self.speech_stopped_at) * 1000
            )
        logger.info(
            "[VOICE_LATENCY] eos_to_created_ms=%s created_to_delta_ms=%s "
            "eos_to_first_audio_ms=%s",
            self.eos_to_created_ms,
            self.created_to_delta_ms,
            self.eos_to_first_audio_ms,
        )
        self.logged = True


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
        ),
        name=f"realtime-sideband-{openai_call_id}",
    )
    _active_monitors[openai_call_id] = task
    task.add_done_callback(lambda _: _active_monitors.pop(openai_call_id, None))
    return True


async def _monitor_with_retries(
    *,
    tenant_id: UUID,
    call_id: UUID,
    openai_call_id: str,
    initial_greeting: str = BILINGUAL_GREETING,
) -> None:
    # Set as soon as the WebSocket handshake succeeds. A later tool or DB error
    # must not be recorded as "never attached" — the caller is already talking.
    attached: dict[str, bool] = {"ok": False}
    for attempt in range(1, MAX_WS_RETRIES + 1):
        try:
            await _run_sideband_session(
                tenant_id=tenant_id,
                call_id=call_id,
                openai_call_id=openai_call_id,
                attached=attached,
                initial_greeting=initial_greeting,
            )
            if attached["ok"]:
                await _mark_completed(tenant_id=tenant_id, call_id=call_id)
            return
        except ConnectionClosed:
            logger.info(
                "Realtime sideband closed openai_call_id=%s attempt=%s attached=%s",
                openai_call_id,
                attempt,
                attached["ok"],
            )
            if attached["ok"]:
                await _mark_completed(tenant_id=tenant_id, call_id=call_id)
                return
        except InvalidStatus as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            logger.error(
                "Realtime sideband rejected openai_call_id=%s status=%s attached=%s project_header_set=%s",
                openai_call_id,
                status,
                attached["ok"],
                bool((get_settings().openai_sip_project_id or "").strip()),
            )
            # A 404 after we already joined means the SIP call ended. Completing
            # it matches Twilio. A 404 before attach means the session never started.
            if attached["ok"]:
                await _mark_completed(tenant_id=tenant_id, call_id=call_id)
                return
            await hangup_realtime_call(openai_call_id=openai_call_id)
            await _mark_monitor_failed(
                tenant_id=tenant_id,
                call_id=call_id,
                reason=f"realtime_websocket_http_{status or 'error'}",
            )
            return
        except Exception:  # noqa: BLE001 - keep monitor failures contained
            logger.exception(
                "Realtime sideband failure openai_call_id=%s attempt=%s attached=%s",
                openai_call_id,
                attempt,
                attached["ok"],
            )
        if attempt < MAX_WS_RETRIES:
            await asyncio.sleep(RETRY_DELAY_SECONDS * attempt)

    if attached["ok"]:
        # The voice session already happened. Losing the monitor is not a failed call.
        await _mark_completed(tenant_id=tenant_id, call_id=call_id)
        return

    # Do not fake completion when the WebSocket never attached successfully.
    await hangup_realtime_call(openai_call_id=openai_call_id)
    await _mark_monitor_failed(
        tenant_id=tenant_id,
        call_id=call_id,
        reason="realtime_websocket_attach_failed",
    )


async def _run_sideband_session(
    *,
    tenant_id: UUID,
    call_id: UUID,
    openai_call_id: str,
    attached: dict[str, bool] | None = None,
    initial_greeting: str = BILINGUAL_GREETING,
) -> None:
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    url = realtime_sideband_url(openai_call_id)
    # WebSocket handshake must not send Content-Type.
    headers = openai_auth_headers(content_type=None)
    logger.info(
        "Connecting Realtime sideband openai_call_id=%s project_header_set=%s",
        openai_call_id,
        bool(headers.get("OpenAI-Project")),
    )

    async with websockets.connect(url, additional_headers=headers, max_size=8 * 1024 * 1024) as ws:
        if attached is not None:
            attached["ok"] = True
        # Promote RINGING → ACTIVE only after the control channel is live.
        async with AsyncSessionLocal() as db:
            calls = CallService(db, tenant_id)
            call = await calls.get(call_id)
            if call is not None and call.status == CallStatus.RINGING:
                await calls.set_status(
                    call_id,
                    CallStatus.ACTIVE,
                    answered_at=datetime.now(timezone.utc),
                    openai_session_id=openai_call_id,
                )
            await db.commit()

        # Speak the selected greeting once. Later turns use server_vad create_response.
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

        latency = _LatencyProbe()
        async for raw in ws:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="ignore")
            event = _safe_json_loads(raw)
            if event is None:
                continue
            try:
                await _handle_event(
                    ws=ws,
                    event=event,
                    tenant_id=tenant_id,
                    call_id=call_id,
                    openai_call_id=openai_call_id,
                    latency=latency,
                )
            except Exception:  # noqa: BLE001 - one bad event must not drop a live call
                logger.exception(
                    "Realtime sideband event failed openai_call_id=%s type=%s",
                    openai_call_id,
                    event.get("type"),
                )


async def _handle_event(
    *,
    ws: Any,
    event: dict[str, Any],
    tenant_id: UUID,
    call_id: UUID,
    openai_call_id: str,
    latency: _LatencyProbe | None = None,
) -> None:
    event_type = str(event.get("type") or "")
    event_id = str(event.get("event_id") or event.get("id") or "")

    if latency is not None:
        if event_type == "input_audio_buffer.speech_started":
            latency.on_speech_started()
        elif event_type == "input_audio_buffer.speech_stopped":
            latency.on_speech_stopped()
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
            await calls.add_message(call_id, role="user", content=transcript)
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
        return


async def _handle_tool_call(
    *,
    ws: Any,
    event: dict[str, Any],
    tenant_id: UUID,
    call_id: UUID,
) -> None:
    tool_call_id = str(event.get("call_id") or "")
    tool_name = str(event.get("name") or "")
    arguments_raw = event.get("arguments") or "{}"
    event_id = str(event.get("event_id") or "")

    if not tool_call_id or not tool_name:
        return

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
        result = await executor.execute(
            tool_name,
            arguments,
            call_id=call_id,
            provider_tool_call_id=tool_call_id,
        )
        await db.commit()

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
    await ws.send(json.dumps({"type": "response.create"}))


async def _mark_completed(*, tenant_id: UUID, call_id: UUID) -> None:
    async with AsyncSessionLocal() as db:
        calls = CallService(db, tenant_id)
        call = await calls.get(call_id)
        if call is None:
            return
        if call.status in {CallStatus.COMPLETED, CallStatus.FAILED, CallStatus.REJECTED}:
            await db.commit()
            return
        ended_at = datetime.now(timezone.utc)
        duration = None
        if call.started_at is not None:
            duration = max(0, int((ended_at - call.started_at).total_seconds()))
        await calls.set_status(
            call_id,
            CallStatus.COMPLETED,
            ended_at=ended_at,
            duration_seconds=duration,
            failure_reason=None,
        )
        await db.commit()


async def _mark_monitor_failed(*, tenant_id: UUID, call_id: UUID, reason: str) -> None:
    async with AsyncSessionLocal() as db:
        calls = CallService(db, tenant_id)
        call = await calls.get(call_id)
        if call is None:
            return
        await calls.add_event(call_id, "realtime.monitor_failed", {"reason": reason})
        # ACTIVE is set before sideband attaches. If the WebSocket never connected,
        # the call is not live — mark FAILED so capacity is released (avoids SIP 486).
        if call.status in {CallStatus.RINGING, CallStatus.ACTIVE}:
            await calls.set_status(call_id, CallStatus.FAILED, failure_reason=reason)
        await db.commit()
