"""Call lifecycle transitions and redacted structured logs.

SIP audio stays up when the sideband drops. Terminal states never move back
to ACTIVE.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# A brief sideband blip is a reconnect, not a new call.
RECONNECT_GRACE_SECONDS = 8

_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}|whsec_[A-Za-z0-9+/=]+|Bearer\s+\S+|api[_-]?key[\"']?\s*[:=]\s*[\"']?\S+)",
    re.IGNORECASE,
)


class CallLifecycle(str, Enum):
    PENDING = "PENDING"
    ACCEPTING = "ACCEPTING"
    ACTIVE = "ACTIVE"
    ENDING = "ENDING"
    ENDED = "ENDED"
    FAILED = "FAILED"


_ALLOWED: dict[CallLifecycle, set[CallLifecycle]] = {
    CallLifecycle.PENDING: {
        CallLifecycle.ACCEPTING,
        CallLifecycle.ACTIVE,
        CallLifecycle.ENDING,
        CallLifecycle.ENDED,
        CallLifecycle.FAILED,
    },
    CallLifecycle.ACCEPTING: {
        CallLifecycle.ACTIVE,
        CallLifecycle.ENDING,
        CallLifecycle.ENDED,
        CallLifecycle.FAILED,
    },
    CallLifecycle.ACTIVE: {
        CallLifecycle.ENDING,
        CallLifecycle.ENDED,
        CallLifecycle.FAILED,
    },
    CallLifecycle.ENDING: {CallLifecycle.ENDED, CallLifecycle.FAILED},
    CallLifecycle.ENDED: set(),
    CallLifecycle.FAILED: set(),
}

TERMINAL_LIFECYCLES = {CallLifecycle.ENDED, CallLifecycle.FAILED}

_ERROR_EVENTS = {"CALL_FAILED", "SIDEBAND_FORENSIC", "SIDEBAND_DISCONNECTED"}


def can_transition(current: CallLifecycle, new: CallLifecycle) -> bool:
    """Same-state writes are idempotent. Terminal states stay terminal."""
    if current == new:
        return True
    return new in _ALLOWED[current]


def redact_secrets(value: str, *, limit: int = 500) -> str:
    text = _SECRET_RE.sub("[redacted]", value or "")
    return text.replace("\n", " ")[:limit]


def log_call_event(event: str, **fields: Any) -> None:
    parts = [event]
    for key, value in fields.items():
        if value is None:
            continue
        rendered = redact_secrets(str(value), limit=300)
        parts.append(f"{key}={rendered}")
    line = " ".join(parts)
    if event in _ERROR_EVENTS:
        logger.error(line)
    else:
        logger.info(line)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def lifecycle_from_call(call: Any) -> CallLifecycle:
    explicit = getattr(call, "lifecycle_state", None)
    parsed = _parse_lifecycle(explicit)
    if parsed is not None:
        return parsed
    meta = getattr(call, "metadata_json", None)
    if isinstance(meta, dict):
        parsed = _parse_lifecycle(meta.get("lifecycle_state"))
        if parsed is not None:
            return parsed
    status = getattr(getattr(call, "status", None), "value", None) or str(getattr(call, "status", "") or "")
    mapped = {
        "ringing": CallLifecycle.PENDING,
        "queued": CallLifecycle.PENDING,
        "active": CallLifecycle.ACTIVE,
        "completed": CallLifecycle.ENDED,
        "transferred": CallLifecycle.ENDED,
        "failed": CallLifecycle.FAILED,
        "rejected": CallLifecycle.FAILED,
    }.get(status.lower())
    return mapped or CallLifecycle.PENDING


def _parse_lifecycle(value: Any) -> CallLifecycle | None:
    if not isinstance(value, str):
        return None
    try:
        return CallLifecycle(value)
    except ValueError:
        return None


def control_channel(call: Any) -> str | None:
    meta = getattr(call, "metadata_json", None)
    if not isinstance(meta, dict):
        return None
    value = meta.get("control_channel")
    return value if isinstance(value, str) else None


def call_meta_flag(call: Any, key: str) -> bool:
    meta = getattr(call, "metadata_json", None)
    if not isinstance(meta, dict):
        return False
    return bool(meta.get(key))


def openai_session_confirmed_gone(call: Any) -> bool:
    """Positive evidence OpenAI no longer has this rtc session (e.g. reconnect 404).

    Checks persisted call metadata. In-process forensics registry is checked by
    callers via ``realtime_forensics.is_openai_session_confirmed_gone``.
    """
    return call_meta_flag(call, "openai_session_confirmed_gone")


def prior_call_blocks_new_invite(call: Any, *, now: datetime | None = None) -> bool:
    """True only with live sideband evidence — never from a DB row / phone alone.

    A) sideband connected + ACTIVE → block (603)
    B) disconnected within reconnect grace, session not confirmed gone → block
    C/D) session confirmed gone, past grace, or terminal → do not block
    """
    lifecycle = lifecycle_from_call(call)
    if lifecycle in {CallLifecycle.ENDING, CallLifecycle.ENDED, CallLifecycle.FAILED}:
        return False
    if openai_session_confirmed_gone(call):
        return False
    channel = control_channel(call)
    if channel == "connected" and lifecycle == CallLifecycle.ACTIVE:
        return True
    if channel == "disconnected":
        return _within_reconnect_grace(call, now=now)
    # RINGING / unanswered / no control-channel proof: do not 603 on DB row alone.
    return False


def _within_reconnect_grace(call: Any, *, now: datetime | None) -> bool:
    meta = getattr(call, "metadata_json", None)
    if not isinstance(meta, dict):
        return False
    raw = meta.get("sideband_disconnected_at")
    if not isinstance(raw, str) or not raw:
        return False
    try:
        seen = datetime.fromisoformat(raw)
    except ValueError:
        return False
    moment = now or utc_now()
    return (moment - as_utc(seen)).total_seconds() < RECONNECT_GRACE_SECONDS
