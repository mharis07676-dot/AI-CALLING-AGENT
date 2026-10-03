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
from app.ai.voice_agent_prompt import VOICE_AGENT_SYSTEM_PROMPT
from app.config import get_settings

logger = logging.getLogger(__name__)

OPENAI_API_BASE = "https://api.openai.com/v1"
REALTIME_WS_BASE = "wss://api.openai.com/v1/realtime"


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
    voice: str = "alloy",
    language_hint: str = "roman_urdu",
    instructions: str | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    prompt = instructions or VOICE_AGENT_SYSTEM_PROMPT
    return {
        "type": "realtime",
        "model": settings.openai_realtime_model,
        "instructions": prompt
        + f"\n\nTenant: {tenant_id}\nCall: {call_id}\nLanguage hint: {language_hint}",
        "output_modalities": ["audio"],
        "audio": {
            "input": {
                "transcription": {"model": "gpt-4o-transcribe"},
                "turn_detection": {"type": "server_vad"},
            },
            "output": {"voice": voice},
        },
        "tools": TOOL_DEFINITIONS,
        "tool_choice": "auto",
        "metadata": {
            "tenant_id": str(tenant_id),
            "call_id": str(call_id),
            "owner": "synas_labs",
            "openai_sip_project_id_set": bool(settings.openai_sip_project_id),
            "sip_trunk_configured": bool(settings.sip_trunk_id),
        },
    }


def build_accept_payload(
    *,
    tenant_id: UUID,
    call_id: UUID,
    voice: str = "alloy",
    language_hint: str = "roman_urdu",
    instructions: str | None = None,
) -> dict[str, Any]:
    """Session config for POST /v1/realtime/calls/{call_id}/accept."""
    return build_realtime_session_config(
        tenant_id=tenant_id,
        call_id=call_id,
        voice=voice,
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
    status_code: int = 486,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Reject an inbound SIP call via Realtime Calls API."""
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
