"""OpenAI Realtime / GPT-Live session helpers.

Direct OpenAI Realtime + SIP first (not Retell/Vapi by default).
Sideband control and tool execution stay in Synas backend.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.ai.instructions import SYSTEM_INSTRUCTIONS
from app.ai.tools import TOOL_DEFINITIONS
from app.config import get_settings


def build_realtime_session_config(
    *,
    tenant_id: UUID,
    call_id: UUID,
    voice: str = "alloy",
    language_hint: str = "roman_urdu",
) -> dict[str, Any]:
    settings = get_settings()
    return {
        "model": settings.openai_realtime_model,
        "voice": voice,
        "modalities": ["audio", "text"],
        "instructions": SYSTEM_INSTRUCTIONS
        + f"\n\nTenant: {tenant_id}\nCall: {call_id}\nLanguage hint: {language_hint}",
        "tools": TOOL_DEFINITIONS,
        "tool_choice": "auto",
        "input_audio_transcription": {"model": "gpt-4o-transcribe"},
        "turn_detection": {"type": "server_vad"},
        "metadata": {
            "tenant_id": str(tenant_id),
            "call_id": str(call_id),
            "owner": "synas_labs",
        },
    }


async def create_realtime_session_stub(
    *,
    tenant_id: UUID,
    call_id: UUID,
) -> dict[str, Any]:
    """Return session config. Live OpenAI session create is enabled when API key is set."""
    settings = get_settings()
    config = build_realtime_session_config(tenant_id=tenant_id, call_id=call_id)
    if not settings.openai_api_key:
        return {
            "ok": True,
            "mode": "stub",
            "session": config,
            "message": "OPENAI_API_KEY not set; returning local session config only",
        }
    # Live HTTP session creation will be wired against OpenAI Realtime SIP APIs
    # once org SIP + trunk credentials are provisioned.
    return {
        "ok": True,
        "mode": "config_ready",
        "session": config,
        "message": "Session config ready for OpenAI Realtime SIP attachment",
    }
