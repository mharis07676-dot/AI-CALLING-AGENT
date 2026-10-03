"""OpenAI Realtime / SIP session helpers.

Runtime path: Realtime Call API (not Live session API).
- Incoming webhook: realtime.call.incoming
- Accept: POST /v1/realtime/calls/{call_id}/accept
- Sideband: wss://api.openai.com/v1/realtime?call_id={call_id}

Credentials are read from environment via Settings — never hardcoded.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import httpx

from app.ai.tools import TOOL_DEFINITIONS
from app.ai.voice_agent_prompt import BRAND_PRONUNCIATION_GUIDANCE, VOICE_AGENT_SYSTEM_PROMPT
from app.config import get_settings
from app.voice.language_control import (
    apply_language_control,
    preference_to_call_language,
    state_from_preference,
)

logger = logging.getLogger(__name__)

OPENAI_API_BASE = "https://api.openai.com/v1"
REALTIME_WS_BASE = "wss://api.openai.com/v1/realtime"

# Spoken once at call start. Keep short and human — not an IVR menu.
BILINGUAL_GREETING = (
    "Hello, Assalam-o-Alaikum — this is Synas Labs. "
    "You can speak in Urdu or English, whichever you prefer."
)
URDU_GREETING = (
    "Assalam-o-Alaikum, Synas Labs se baat ho rahi hai. "
    "Main aapki kis tarah madad kar sakta hoon?"
)
ENGLISH_GREETING = "Hello, this is Synas Labs. How can I help you?"


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


def build_language_session_update(instructions: str) -> dict[str, Any]:
    """session.update that changes only instructions. VAD, tools, and voice stay."""
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": instructions,
        },
    }


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
    voice: str = "echo",
    preferred_language: str | None = None,
    language_hint: str | None = None,
    instructions: str | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    prompt = instructions or VOICE_AGENT_SYSTEM_PROMPT
    preference = preferred_language if preferred_language is not None else language_hint
    language_state = state_from_preference(preference)
    _, greeting = select_initial_greeting(preference)
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
                # server_vad still ends the turn. create_response is false so the
                # sideband can lock call_language from the transcript and only then
                # send response.create. Otherwise the model answers before detection.
                #
                # silence_duration_ms=300 is the current baseline.
                # If live logs show eos_to_created_ms still too high, try 250ms next.
                # Only then consider 200ms. Going too low interrupts natural pauses
                # (e.g. "Actually mujhe... ek package...").
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 300,
                    "create_response": False,
                    "interrupt_response": True,
                },
            },
            "output": {"voice": voice},
        },
        "tools": TOOL_DEFINITIONS,
        "tool_choice": "auto",
    }


def build_accept_payload(
    *,
    tenant_id: UUID,
    call_id: UUID,
    voice: str = "echo",
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


async def accept_realtime_call(
    *,
    openai_call_id: str,
    session_config: dict[str, Any],
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Accept an inbound SIP call via Realtime Calls API.

    POST /v1/realtime/calls/{call_id}/accept
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
    logger.info(
        "OpenAI accept call_id=%s project_header_set=%s",
        openai_call_id,
        bool(headers.get("OpenAI-Project")),
    )

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=30.0)
    try:
        response = await http.post(url, headers=headers, json=session_config)
    except httpx.HTTPError:
        logger.exception("OpenAI accept request failed for call_id=%s", openai_call_id)
        return {
            "ok": False,
            "error": "openai_accept_request_failed",
            "message": "OpenAI accept request failed",
        }
    finally:
        if owns_client:
            await http.aclose()

    if response.status_code >= 400:
        # Keep error text short; never log full response bodies (may contain secrets).
        body_preview = (response.text or "")[:180].replace("\n", " ")
        logger.error(
            "OpenAI accept rejected call_id=%s status=%s body=%s",
            openai_call_id,
            response.status_code,
            body_preview,
        )
        return {
            "ok": False,
            "error": "openai_accept_rejected",
            "message": f"OpenAI accept rejected with HTTP {response.status_code}",
            "status_code": response.status_code,
        }

    return {
        "ok": True,
        "openai_call_id": openai_call_id,
        "status_code": response.status_code,
        "message": "OpenAI realtime call accepted",
    }


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
    http = client or httpx.AsyncClient(timeout=30.0)
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
    http = client or httpx.AsyncClient(timeout=30.0)
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
