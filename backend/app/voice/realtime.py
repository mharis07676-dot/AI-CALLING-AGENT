"""OpenAI Realtime / SIP session helpers.

Runtime path: Realtime Call API (not Live session API).
- Incoming webhook: realtime.call.incoming
- Accept: POST /v1/realtime/calls/{call_id}/accept
- Sideband: wss://api.openai.com/v1/realtime?call_id={call_id}

Credentials are read from environment via Settings — never hardcoded.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any
from uuid import UUID

import httpx

from app.ai.tools import TOOL_DEFINITIONS
from app.ai.voice_agent_prompt import BRAND_PRONUNCIATION_GUIDANCE, VOICE_AGENT_SYSTEM_PROMPT
from app.config import get_settings
from app.voice.call_lifecycle import redact_secrets
from app.voice.language_control import (
    apply_language_control,
    preference_to_call_language,
    state_from_preference,
)

logger = logging.getLogger(__name__)

OPENAI_API_BASE = "https://api.openai.com/v1"
REALTIME_WS_BASE = "wss://api.openai.com/v1/realtime"
OPENAI_HTTP_TIMEOUT = httpx.Timeout(5.0, connect=3.0)
_TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_MAX_ACCEPT_BACKOFF_SECONDS = 0.4

# Voices supported by OpenAI Realtime (gpt-realtime). Do not invent names.
REALTIME_VOICES = frozenset(
    {
        "alloy",
        "ash",
        "ballad",
        "coral",
        "echo",
        "sage",
        "shimmer",
        "verse",
        "marin",
        "cedar",
    }
)

# Spoken once at call start. Keep short and human — not an IVR menu.
BILINGUAL_GREETING = (
    "Hello, Assalam-o-Alaikum — this is Synas Labs. "
    "You can speak in Urdu or English, whichever you prefer."
)
URDU_GREETING = (
    "Assalam-o-Alaikum, Synas Labs se baat ho rahi hai. "
    "Main aapki kis tarah madad kar sakti hoon?"
)
ENGLISH_GREETING = "Hello, this is Synas Labs. How can I help you?"


def resolve_realtime_voice(preferred: str | None = None) -> str:
    """Pick a supported Realtime voice.

    Priority: OPENAI_REALTIME_VOICE env → preferred/agent voice → marin.
    ``marin`` / ``cedar`` are OpenAI's recommended natural voices for gpt-realtime.
    """
    settings = get_settings()
    for candidate in (
        settings.openai_realtime_voice,
        preferred,
        "marin",
    ):
        key = (candidate or "").strip().lower()
        if key in REALTIME_VOICES:
            return key
    return "marin"


def normalize_preferred_language(raw: str | None) -> str | None:
    """Return 'en' | 'ur' when preference is explicit; else None (unknown).

    ``Customer.language`` defaults to ``roman_urdu`` for new contacts, so that
    value alone is treated as unknown — not a confirmed Urdu preference.
    """
    return preference_to_call_language(raw)


def greeting_speak_instructions(greeting: str) -> str:
    """Instructions for the one-shot greeting response.create.

    Those instructions replace the session prompt for that turn only, so the
    pronunciation hint has to be included here. ``greeting`` stays written
    "Synas Labs".
    """
    return (
        "Speak this greeting now, naturally and briefly, then stop and listen. "
        "Conversation language is controlled by the application. Do not add another language. "
        "Do not add an IVR language menu or ask them to choose a language. "
        f"{BRAND_PRONUNCIATION_GUIDANCE}\n"
        f'Greeting: "{greeting}"'
    )


def select_initial_greeting(preferred_language: str | None) -> tuple[str, str]:
    """Return (call_language_or_unknown, greeting_text). call language is en|ur."""
    lang = preference_to_call_language(preferred_language)
    if lang == "en":
        return "en", ENGLISH_GREETING
    if lang == "ur":
        return "ur", URDU_GREETING
    return "unknown", BILINGUAL_GREETING


def build_turn_detection(*, create_response: bool) -> dict[str, Any]:
    """server_vad tuned for phone turns. interrupt_response enables barge-in."""
    return {
        "type": "server_vad",
        "threshold": 0.5,
        "prefix_padding_ms": 300,
        # 300ms is the low end of natural EOS silence; lower risks cutting mid-thought.
        "silence_duration_ms": 300,
        "create_response": create_response,
        "interrupt_response": True,
    }


def build_language_session_update(
    instructions: str,
    *,
    create_response: bool | None = None,
) -> dict[str, Any]:
    """session.update for instructions; optionally flip auto-response after language lock."""
    session: dict[str, Any] = {
        "type": "realtime",
        "instructions": instructions,
    }
    if create_response is not None:
        session["audio"] = {
            "input": {
                "turn_detection": build_turn_detection(create_response=create_response),
            }
        }
    return {"type": "session.update", "session": session}


def openai_auth_headers(*, content_type: str | None = "application/json") -> dict[str, str]:
    """Auth headers for Realtime Call API + sideband WebSocket.

    ``OpenAI-Project`` must match the SIP destination project
    (``sip:{OPENAI_SIP_PROJECT_ID}@sip.api.openai.com``). Without it, accept/WS
    can 404 when the API key resolves to a different default project.
    """
    settings = get_settings()
    headers: dict[str, str] = {
        "Authorization": f"Bearer {settings.openai_api_key}",
    }
    if content_type:
        headers["Content-Type"] = content_type
    project_id = (settings.openai_sip_project_id or "").strip()
    if project_id:
        headers["OpenAI-Project"] = project_id
    return headers


def build_realtime_session_config(
    *,
    tenant_id: UUID,
    call_id: UUID,
    voice: str | None = None,
    preferred_language: str | None = None,
    language_hint: str | None = None,
    instructions: str | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    prompt = instructions or VOICE_AGENT_SYSTEM_PROMPT
    preference = preferred_language if preferred_language is not None else language_hint
    language_state = state_from_preference(preference)
    _, greeting = select_initial_greeting(preference)
    selected_voice = resolve_realtime_voice(voice)
    # Keep accept payload close to OpenAI SIP docs. Extra/unknown fields have caused
    # accept=200 with an immediately-dead session (sideband HTTP 404).
    # No transcription "language" field: a fixed code would bias Urdu or English
    # and can translate instead of transcribing.
    body = prompt + (
        f"\n\nTenant: {tenant_id}\nCall: {call_id}\n"
        f"INITIAL GREETING (speak once at call start, then stop and listen):\n"
        f'"{greeting}"\n'
        "Do not repeat this greeting. Do not ask them to choose a language."
    )
    return {
        "type": "realtime",
        "model": settings.openai_realtime_model,
        "instructions": apply_language_control(body, language_state),
        "audio": {
            "input": {
                "transcription": {
                    "model": "gpt-4o-transcribe",
                    "prompt": (
                        "Transcribe the caller's words in the original language. "
                        "Do not translate. Keep Urdu in Urdu script and Roman Urdu in Latin letters."
                    ),
                },
                # Auto-respond once language is known. If unknown, sideband waits for
                # the first transcript lock then enables create_response (see monitor).
                # silence_duration_ms=300: natural phone EOS without cutting mid-pause.
                "turn_detection": build_turn_detection(
                    create_response=bool(
                        language_state.language_locked and language_state.call_language
                    ),
                ),
            },
            "output": {"voice": selected_voice},
        },
        "tools": TOOL_DEFINITIONS,
        "tool_choice": "auto",
    }


def build_accept_payload(
    *,
    tenant_id: UUID,
    call_id: UUID,
    voice: str | None = None,
    preferred_language: str | None = None,
    language_hint: str | None = None,
    instructions: str | None = None,
) -> dict[str, Any]:
    """Session config for POST /v1/realtime/calls/{call_id}/accept."""
    return build_realtime_session_config(
        tenant_id=tenant_id,
        call_id=call_id,
        voice=voice,
        preferred_language=preferred_language,
        language_hint=language_hint,
        instructions=instructions,
    )


async def create_realtime_session_stub(
    *,
    tenant_id: UUID,
    call_id: UUID,
) -> dict[str, Any]:
    """Legacy helper kept for tests; live attach uses accept_realtime_call."""
    settings = get_settings()
    config = build_realtime_session_config(tenant_id=tenant_id, call_id=call_id)
    if not settings.openai_api_key:
        return {
            "ok": True,
            "mode": "stub",
            "session": config,
            "message": "OPENAI_API_KEY not set; returning local session config only",
        }
    return {
        "ok": True,
        "mode": "config_ready",
        "session": config,
        "openai_realtime_model": settings.openai_realtime_model,
        "message": "Session config ready for OpenAI Realtime SIP attachment",
    }


async def _accept_backoff(attempt: int) -> None:
    base = min(0.1 * (2 ** (attempt - 1)), _MAX_ACCEPT_BACKOFF_SECONDS)
    await asyncio.sleep(base * (0.5 + random.random()))


def _response_preview(response: httpx.Response) -> str:
    raw = getattr(response, "text", "") or ""
    if not isinstance(raw, str):
        raw = ""
    return redact_secrets(raw, limit=500)


async def accept_realtime_call(
    *,
    openai_call_id: str,
    session_config: dict[str, Any],
    client: httpx.AsyncClient | None = None,
    attempts: int = 1,
) -> dict[str, Any]:
    """Accept an inbound SIP call via Realtime Calls API.

    POST /v1/realtime/calls/{call_id}/accept

    Transient HTTP failures retry with bounded backoff. A call that already
    succeeded must not be accepted again by the caller — ``attempts`` only
    repeats this HTTP request while it is still failing.
    """
    settings = get_settings()
    if not settings.openai_api_key:
        return {
            "ok": False,
            "error": "openai_api_key_missing",
            "message": "OPENAI_API_KEY is not configured",
        }

    url = f"{OPENAI_API_BASE}/realtime/calls/{openai_call_id}/accept"
    headers = openai_auth_headers()
    total = max(1, attempts)
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=OPENAI_HTTP_TIMEOUT)
    last: dict[str, Any] = {
        "ok": False,
        "error": "openai_accept_request_failed",
        "message": "OpenAI accept request failed",
    }
    try:
        for attempt in range(1, total + 1):
            try:
                response = await http.post(url, headers=headers, json=session_config)
            except httpx.HTTPError:
                logger.exception(
                    "OpenAI accept request failed call_id=%s attempt=%s",
                    openai_call_id,
                    attempt,
                )
                last = {
                    "ok": False,
                    "error": "openai_accept_request_failed",
                    "message": "OpenAI accept request failed",
                }
                if attempt >= total:
                    return last
                await _accept_backoff(attempt)
                continue

            if response.status_code < 400:
                return {
                    "ok": True,
                    "openai_call_id": openai_call_id,
                    "status_code": response.status_code,
                    "message": "OpenAI realtime call accepted",
                }

            body_preview = _response_preview(response)
            logger.error(
                "OpenAI accept rejected call_id=%s status=%s attempt=%s body=%s",
                openai_call_id,
                response.status_code,
                attempt,
                body_preview,
            )
            last = {
                "ok": False,
                "error": "openai_accept_rejected",
                "message": f"OpenAI accept rejected with HTTP {response.status_code}",
                "status_code": response.status_code,
                "body_preview": body_preview,
            }
            # 409 is not retried: the call was already accepted or is no longer acceptable.
            if response.status_code not in _TRANSIENT_HTTP_STATUSES or attempt >= total:
                return last
            await _accept_backoff(attempt)
        return last
    finally:
        if owns_client:
            await http.aclose()


async def reject_realtime_call(
    *,
    openai_call_id: str,
    status_code: int = 603,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Reject an inbound SIP call via Realtime Calls API.

    Prefer 603 Decline. 486 Busy is what Twilio displays as "user is busy".
    """
    settings = get_settings()
    if not settings.openai_api_key:
        return {
            "ok": False,
            "error": "openai_api_key_missing",
            "message": "OPENAI_API_KEY is not configured",
        }

    url = f"{OPENAI_API_BASE}/realtime/calls/{openai_call_id}/reject"
    headers = openai_auth_headers()
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=OPENAI_HTTP_TIMEOUT)
    try:
        response = await http.post(url, headers=headers, json={"status_code": status_code})
    except httpx.HTTPError:
        logger.exception("OpenAI reject request failed for call_id=%s", openai_call_id)
        return {
            "ok": False,
            "error": "openai_reject_request_failed",
            "message": "OpenAI reject request failed",
        }
    finally:
        if owns_client:
            await http.aclose()

    if response.status_code >= 400:
        logger.error(
            "OpenAI reject rejected call_id=%s status=%s",
            openai_call_id,
            response.status_code,
        )
        return {
            "ok": False,
            "error": "openai_reject_rejected",
            "message": f"OpenAI reject rejected with HTTP {response.status_code}",
            "status_code": response.status_code,
        }

    return {
        "ok": True,
        "openai_call_id": openai_call_id,
        "status_code": response.status_code,
        "message": "OpenAI realtime call rejected",
    }


async def hangup_realtime_call(
    *,
    openai_call_id: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Hang up an accepted Realtime SIP call."""
    settings = get_settings()
    if not settings.openai_api_key:
        return {
            "ok": False,
            "error": "openai_api_key_missing",
            "message": "OPENAI_API_KEY is not configured",
        }

    url = f"{OPENAI_API_BASE}/realtime/calls/{openai_call_id}/hangup"
    headers = openai_auth_headers()
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=OPENAI_HTTP_TIMEOUT)
    try:
        response = await http.post(url, headers=headers)
    except httpx.HTTPError:
        logger.exception("OpenAI hangup request failed for call_id=%s", openai_call_id)
        return {
            "ok": False,
            "error": "openai_hangup_request_failed",
            "message": "OpenAI hangup request failed",
        }
    finally:
        if owns_client:
            await http.aclose()

    if response.status_code >= 400:
        logger.error(
            "OpenAI hangup rejected call_id=%s status=%s",
            openai_call_id,
            response.status_code,
        )
        return {
            "ok": False,
            "error": "openai_hangup_rejected",
            "message": f"OpenAI hangup rejected with HTTP {response.status_code}",
            "status_code": response.status_code,
        }

    return {
        "ok": True,
        "openai_call_id": openai_call_id,
        "status_code": response.status_code,
        "message": "OpenAI realtime call hung up",
    }


def realtime_sideband_url(openai_call_id: str) -> str:
    return f"{REALTIME_WS_BASE}?call_id={openai_call_id}"
