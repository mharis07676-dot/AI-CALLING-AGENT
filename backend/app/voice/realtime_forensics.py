"""Bounded Realtime sideband forensics for disconnect/reconnect diagnosis.

Never stores credentials, auth headers, or raw audio. Event payloads are
trimmed to safe metadata only (types, ids, codes, short messages).
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.voice.call_lifecycle import log_call_event, redact_secrets

RING_SIZE = 50

_USER_TRANSCRIPT_EVENTS = frozenset(
    {
        "conversation.item.input_audio_transcription.completed",
    }
)
_ASSISTANT_AUDIO_EVENTS = frozenset(
    {
        "response.output_audio.delta",
        "response.audio.delta",
        "response.output_audio_transcript.done",
        "response.audio_transcript.done",
    }
)
_RESPONSE_DONE_EVENTS = frozenset({"response.done", "conversation.item.completed"})
_TOOL_START_EVENTS = frozenset({"response.function_call_arguments.done"})

_lock = threading.Lock()
_by_openai_call: dict[str, "SidebandForensics"] = {}
_session_gone: set[str] = set()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def railway_instance_id() -> str | None:
    for key in (
        "RAILWAY_DEPLOYMENT_ID",
        "RAILWAY_REPLICA_ID",
        "RAILWAY_ENVIRONMENT_ID",
        "HOSTNAME",
    ):
        value = (os.environ.get(key) or "").strip()
        if value:
            return f"{key}:{value}"
    return None


def _safe_meta(event: dict[str, Any]) -> dict[str, Any]:
    """Extract non-sensitive fields only. Never include audio, args body, or headers."""
    meta: dict[str, Any] = {}
    event_type = str(event.get("type") or "")
    for key in ("event_id", "id", "call_id", "response_id", "item_id", "name"):
        value = event.get(key)
        if value is not None and not isinstance(value, (dict, list)):
            meta[key] = str(value)[:80]
    if event_type == "error":
        err = event.get("error") if isinstance(event.get("error"), dict) else {}
        meta["error_code"] = str(err.get("code") or "")[:80]
        meta["error_type"] = str(err.get("type") or "")[:80]
        meta["error_message"] = redact_secrets(str(err.get("message") or ""), limit=120)
    if event_type in _TOOL_START_EVENTS:
        meta["tool_name"] = str(event.get("name") or "")[:64]
        meta["tool_call_id"] = str(event.get("call_id") or "")[:64]
    return meta


@dataclass
class SidebandForensics:
    tenant_id: UUID
    call_id: UUID
    openai_call_id: str
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=RING_SIZE))
    attached_once: bool = False
    last_user_transcript_at: str | None = None
    last_assistant_audio_at: str | None = None
    last_response_done_at: str | None = None
    last_error: dict[str, Any] | None = None
    active_tool_call: dict[str, Any] | None = None
    ws_close_code: int | None = None
    ws_close_reason: str | None = None
    session_confirmed_gone: bool = False
    last_reconnect_http_status: int | None = None
    last_reconnect_body_preview: str | None = None
    created_at_mono: float = field(default_factory=time.monotonic)

    def mark_attached(self) -> None:
        self.attached_once = True

    def observe(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        if not event_type:
            return
        stamp = _iso_now()
        self.events.append({"type": event_type, "ts": stamp, "meta": _safe_meta(event)})
        if event_type in _USER_TRANSCRIPT_EVENTS:
            self.last_user_transcript_at = stamp
        if event_type in _ASSISTANT_AUDIO_EVENTS:
            self.last_assistant_audio_at = stamp
        if event_type in _RESPONSE_DONE_EVENTS:
            self.last_response_done_at = stamp
            self.active_tool_call = None
        if event_type == "error":
            err = event.get("error") if isinstance(event.get("error"), dict) else {}
            self.last_error = {
                "ts": stamp,
                "code": str(err.get("code") or "")[:80],
                "type": str(err.get("type") or "")[:80],
                "message": redact_secrets(str(err.get("message") or ""), limit=120),
            }
        if event_type in _TOOL_START_EVENTS:
            self.active_tool_call = {
                "ts": stamp,
                "name": str(event.get("name") or "")[:64],
                "call_id": str(event.get("call_id") or "")[:64],
            }

    def note_ws_close(self, *, code: int | None, reason: str | None) -> None:
        self.ws_close_code = code
        if reason:
            self.ws_close_reason = redact_secrets(str(reason), limit=120)

    def note_reconnect_http(self, *, status: int | None, body_preview: str | None) -> None:
        self.last_reconnect_http_status = status
        if body_preview:
            self.last_reconnect_body_preview = redact_secrets(body_preview, limit=200)

    def mark_session_gone(self) -> None:
        self.session_confirmed_gone = True
        with _lock:
            _session_gone.add(self.openai_call_id)

    def recent_event_types(self) -> list[str]:
        return [f"{item['ts']}:{item['type']}" for item in self.events]


def get_or_create_forensics(
    *,
    tenant_id: UUID,
    call_id: UUID,
    openai_call_id: str,
) -> SidebandForensics:
    with _lock:
        existing = _by_openai_call.get(openai_call_id)
        if existing is not None:
            return existing
        state = SidebandForensics(
            tenant_id=tenant_id,
            call_id=call_id,
            openai_call_id=openai_call_id,
        )
        _by_openai_call[openai_call_id] = state
        return state


def get_forensics(openai_call_id: str) -> SidebandForensics | None:
    with _lock:
        return _by_openai_call.get(openai_call_id)


def is_openai_session_confirmed_gone(
    openai_call_id: str | None = None,
    call: Any = None,
) -> bool:
    """True when reconnect/HTTP proved OpenAI no longer has this rtc session."""
    if openai_call_id:
        with _lock:
            if openai_call_id in _session_gone:
                return True
            state = _by_openai_call.get(openai_call_id)
            if state is not None and state.session_confirmed_gone:
                return True
    if call is None:
        return False
    meta = getattr(call, "metadata_json", None)
    if not isinstance(meta, dict):
        return False
    return bool(meta.get("openai_session_confirmed_gone"))


def drop_forensics(openai_call_id: str) -> None:
    with _lock:
        _by_openai_call.pop(openai_call_id, None)


def clear_forensics_for_tests() -> None:
    with _lock:
        _by_openai_call.clear()
        _session_gone.clear()


def log_sideband_forensics(
    *,
    reason: str,
    tenant_id: UUID,
    call_id: UUID,
    openai_call_id: str,
    lifecycle_state: str | None = None,
    twilio_call_sid: str | None = None,
    was_hangup_requested_by_backend: bool | None = None,
    concurrency_slot_held: bool | None = None,
    attached: bool | None = None,
    reconnect_http_status: int | None = None,
    reconnect_body_preview: str | None = None,
    ws_close_code: int | None = None,
    ws_close_reason: str | None = None,
) -> None:
    """Emit one structured forensic line (error level so Railway retains it)."""
    state = get_or_create_forensics(
        tenant_id=tenant_id,
        call_id=call_id,
        openai_call_id=openai_call_id,
    )
    if reconnect_http_status is not None:
        state.note_reconnect_http(
            status=reconnect_http_status,
            body_preview=reconnect_body_preview,
        )
    if ws_close_code is not None or ws_close_reason:
        state.note_ws_close(code=ws_close_code, reason=ws_close_reason)

    recent = state.recent_event_types()
    log_call_event(
        "SIDEBAND_FORENSIC",
        tenant_id=tenant_id,
        call_id=call_id,
        openai_call_id=openai_call_id,
        twilio_call_sid=twilio_call_sid,
        lifecycle_state=lifecycle_state,
        sideband_disconnect_reason=reason,
        ws_close_code=state.ws_close_code,
        ws_close_reason=state.ws_close_reason,
        sideband_attached_once=state.attached_once if attached is None else bool(attached),
        recent_events=";".join(recent[-50:]),
        recent_event_count=len(recent),
        last_user_transcript_at=state.last_user_transcript_at,
        last_assistant_audio_at=state.last_assistant_audio_at,
        last_response_done_at=state.last_response_done_at,
        last_openai_error=state.last_error,
        active_tool_call=state.active_tool_call,
        was_hangup_requested_by_backend=was_hangup_requested_by_backend,
        concurrency_slot_held=concurrency_slot_held,
        railway_instance=railway_instance_id(),
        reconnect_http_status=state.last_reconnect_http_status,
        reconnect_body_preview=state.last_reconnect_body_preview,
        openai_session_confirmed_gone=state.session_confirmed_gone,
    )
