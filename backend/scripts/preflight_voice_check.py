#!/usr/bin/env python3
"""Safe pre-call wiring check for OpenAI Realtime SIP inbound.

Does NOT place a real Twilio/OpenAI phone call.
Does NOT print secret values.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Allow `python backend/scripts/preflight_voice_check.py` from repo root
# and `python scripts/preflight_voice_check.py` from backend/.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

REQUIRED_ENV = [
    "OPENAI_API_KEY",
    "OPENAI_REALTIME_MODEL",
    "OPENAI_SIP_PROJECT_ID",
    "OPENAI_WEBHOOK_SECRET",
    "SIP_TRUNK_ID",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_API_KEY_SID",
    "TWILIO_API_KEY_SECRET",
    "DATABASE_URL",
    "REDIS_URL",
]

OPTIONAL_ENV = [
    "DATABASE_URL_SYNC",
    "MAX_CONCURRENT_CALLS",
    "TWILIO_PHONE_NUMBER",
    "TWILIO_AUTH_TOKEN",
    "SIP_PROVIDER_BASE_URL",
    "SIP_PROVIDER_API_KEY",
    "SIP_PROVIDER_SIP_URL",
    "SIP_USERNAME",
    "SIP_PASSWORD",
    "SIP_CALLER_NUMBER",
]

REQUIRED_TOOLS = {
    "search_properties",
    "get_property_details",
    "create_or_update_lead",
    "check_appointment_availability",
    "book_appointment",
    "request_human_handoff",
    "register_opt_out",
}

REQUIRED_CALL_COLUMNS = (
    "answered_at",
    "openai_session_id",
    "ended_at",
    "status",
)
REQUIRED_TOOL_COLUMNS = (
    "provider_tool_call_id",
    "started_at",
    "completed_at",
)
REQUIRED_TABLES = (
    "idempotency_keys",
    "conversations",
    "messages",
    "calls",
    "tool_calls",
)


def _is_set(value: str | None) -> bool:
    if value is None:
        return False
    normalized = str(value).strip().lower()
    return normalized not in {"", "change-me-in-production", "change-me-jwt-secret"}


def _pass(label: str) -> None:
    print(f"[PASS] {label}")


def _fail(label: str, detail: str = "") -> None:
    suffix = f" — {detail}" if detail else ""
    print(f"[FAIL] {label}{suffix}")


def _info(label: str, detail: str = "") -> None:
    suffix = f" — {detail}" if detail else ""
    print(f"[INFO] {label}{suffix}")


def _settings_map() -> dict[str, str]:
    """Map env names to Settings fields without printing values."""
    from app.config import get_settings

    get_settings.cache_clear()
    s = get_settings()
    return {
        "OPENAI_API_KEY": s.openai_api_key,
        "OPENAI_REALTIME_MODEL": s.openai_realtime_model,
        "OPENAI_SIP_PROJECT_ID": s.openai_sip_project_id,
        "OPENAI_WEBHOOK_SECRET": s.openai_webhook_secret,
        "SIP_TRUNK_ID": s.sip_trunk_id,
        "TWILIO_ACCOUNT_SID": s.twilio_account_sid,
        "TWILIO_API_KEY_SID": s.twilio_api_key_sid,
        "TWILIO_API_KEY_SECRET": s.twilio_api_key_secret,
        "DATABASE_URL": s.database_url,
        "DATABASE_URL_SYNC": s.database_url_sync,
        "REDIS_URL": s.redis_url,
        "MAX_CONCURRENT_CALLS": str(s.max_concurrent_calls),
        "TWILIO_PHONE_NUMBER": s.twilio_phone_number,
        "TWILIO_AUTH_TOKEN": s.twilio_auth_token,
        "SIP_PROVIDER_BASE_URL": s.sip_provider_base_url,
        "SIP_PROVIDER_API_KEY": s.sip_provider_api_key,
        "SIP_PROVIDER_SIP_URL": s.sip_provider_sip_url,
        "SIP_USERNAME": s.sip_username,
        "SIP_PASSWORD": s.sip_password,
        "SIP_CALLER_NUMBER": s.sip_caller_number,
    }


def check_env_names() -> bool:
    ok = True
    values = _settings_map()

    for name in REQUIRED_ENV:
        if _is_set(values.get(name)):
            _pass(f"{name} configured")
        else:
            # Prefer process env presence for Railway-style injects
            if _is_set(os.environ.get(name)):
                _pass(f"{name} configured")
            else:
                _fail(f"{name} configured", "missing or placeholder")
                ok = False

    for name in OPTIONAL_ENV:
        if _is_set(values.get(name)) or _is_set(os.environ.get(name)):
            _info(f"{name} present (optional/legacy)")
        else:
            _info(f"{name} absent (optional for inbound SIP)")

    proj = (values.get("OPENAI_SIP_PROJECT_ID") or os.environ.get("OPENAI_SIP_PROJECT_ID") or "").strip()
    if proj.startswith("proj_"):
        _pass("OPENAI_SIP_PROJECT_ID format starts with proj_")
    elif proj:
        _fail("OPENAI_SIP_PROJECT_ID format starts with proj_", "unexpected prefix")
        ok = False
    else:
        _fail("OPENAI_SIP_PROJECT_ID format starts with proj_", "missing")
        ok = False

    trunk = (values.get("SIP_TRUNK_ID") or os.environ.get("SIP_TRUNK_ID") or "").strip()
    if trunk.startswith("TK"):
        _pass("SIP_TRUNK_ID format looks like TK...")
    elif trunk:
        _fail("SIP_TRUNK_ID format looks like TK...", "unexpected prefix")
        ok = False
    else:
        _fail("SIP_TRUNK_ID format looks like TK...", "missing")
        ok = False

    if _is_set(values.get("OPENAI_WEBHOOK_SECRET")) or _is_set(os.environ.get("OPENAI_WEBHOOK_SECRET")):
        _pass("webhook secret exists")
    else:
        _fail("webhook secret exists")
        ok = False

    twilio_ok = (
        _is_set(values.get("TWILIO_API_KEY_SID")) or _is_set(os.environ.get("TWILIO_API_KEY_SID"))
    ) and (
        _is_set(values.get("TWILIO_API_KEY_SECRET")) or _is_set(os.environ.get("TWILIO_API_KEY_SECRET"))
    )
    if twilio_ok:
        _pass("Twilio API credentials exist")
    else:
        _fail("Twilio API credentials exist")
        ok = False

    return ok


async def check_database() -> bool:
    try:
        import asyncpg
    except ImportError:
        _fail("Database connection", "asyncpg not installed")
        return False

    from app.config import get_settings, to_sync_postgres_url

    settings = get_settings()
    dsn = to_sync_postgres_url(settings.database_url)
    try:
        conn = await asyncpg.connect(dsn)
    except Exception as exc:  # noqa: BLE001 - report connectivity only
        _fail("Database connection", type(exc).__name__)
        return False

    ok = True
    try:
        _pass("Database connection")
        for col in REQUIRED_CALL_COLUMNS:
            exists = await conn.fetchval(
                """
                SELECT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'calls' AND column_name = $1
                )
                """,
                col,
            )
            if exists:
                _pass(f"calls.{col} exists")
            else:
                _fail(f"calls.{col} exists")
                ok = False

        for col in REQUIRED_TOOL_COLUMNS:
            exists = await conn.fetchval(
                """
                SELECT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'tool_calls' AND column_name = $1
                )
                """,
                col,
            )
            if exists:
                _pass(f"tool_calls.{col} exists")
            else:
                _fail(f"tool_calls.{col} exists")
                ok = False

        for table in REQUIRED_TABLES:
            exists = await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f"public.{table}")
            if exists:
                _pass(f"table {table} exists")
            else:
                _fail(f"table {table} exists")
                ok = False

        tenants = await conn.fetch(
            "SELECT id::text, slug, name, is_active FROM tenants ORDER BY created_at ASC LIMIT 10"
        )
        if tenants:
            _pass(f"Tenant records found ({len(tenants)})")
            for row in tenants:
                print(
                    f"       tenant id={row['id']} slug={row['slug']} "
                    f"name={row['name']} active={row['is_active']}"
                )
        else:
            _fail("Tenant records found", "no tenants — create one before configuring webhook URL")
            ok = False
    finally:
        await conn.close()
    return ok


async def check_redis() -> bool:
    try:
        import redis.asyncio as redis
    except ImportError:
        _fail("Redis connection", "redis package not installed")
        return False

    from app.config import get_settings

    settings = get_settings()
    client = redis.from_url(settings.redis_url)
    try:
        pong = await client.ping()
        if pong:
            _pass("Redis connection")
            return True
        _fail("Redis connection", "PING returned false")
        return False
    except Exception as exc:  # noqa: BLE001 - report connectivity only
        _fail("Redis connection", type(exc).__name__)
        return False
    finally:
        await client.aclose()


def check_fastapi_route() -> bool:
    try:
        from app.main import create_app
    except Exception as exc:  # noqa: BLE001
        _fail("OpenAI webhook route registered", type(exc).__name__)
        return False

    app = create_app()
    paths = {getattr(route, "path", "") for route in app.routes}
    expected = "/api/v1/webhooks/openai/{tenant_slug}/inbound"
    if expected in paths:
        _pass("OpenAI webhook route registered")
        return True
    _fail("OpenAI webhook route registered", f"missing {expected}")
    return False


def check_voice_wiring() -> bool:
    ok = True
    try:
        from app.ai.voice_agent_prompt import VOICE_AGENT_SYSTEM_PROMPT
        from app.ai.tools import ALLOWED_TOOLS, TOOL_DEFINITIONS
        from app.config import get_settings
        from app.voice.realtime import (
            accept_realtime_call,
            build_accept_payload,
            realtime_sideband_url,
        )
        from app.voice.session_monitor import start_sideband_monitor
        from app.voice.inbound_sip import handle_realtime_incoming_sip
        from app.voice.openai_webhook import verify_openai_webhook_signature
    except Exception as exc:  # noqa: BLE001
        _fail("Voice modules importable", type(exc).__name__)
        return False

    settings = get_settings()
    if settings.openai_realtime_model:
        _pass(f"Voice model configured ({settings.openai_realtime_model})")
    else:
        _fail("Voice model configured")
        ok = False

    prompt = VOICE_AGENT_SYSTEM_PROMPT or ""
    required_phrases = (
        "Synas Labs",
        "English",
        "Urdu",
        "Roman Urdu",
        "NEVER invent",
        "Don't call me again",
        "human",
    )
    if all(p in prompt for p in required_phrases) or (
        "Synas Labs" in prompt and "NEVER invent" in prompt and "Roman Urdu" in prompt
    ):
        _pass("Voice prompt loaded")
    else:
        _fail("Voice prompt loaded", "missing expected Synas behavior markers")
        ok = False

    tool_names = {t.get("name") for t in TOOL_DEFINITIONS}
    missing = REQUIRED_TOOLS - tool_names
    if not missing and REQUIRED_TOOLS.issubset(ALLOWED_TOOLS):
        _pass("Realtime tools loaded")
    else:
        _fail("Realtime tools loaded", f"missing={sorted(missing)}")
        ok = False

    from uuid import uuid4

    payload = build_accept_payload(tenant_id=uuid4(), call_id=uuid4(), voice="alloy")
    if "instructions" in payload and "tools" in payload and payload.get("type") == "realtime":
        _pass("Realtime accept payload builds with prompt + tools")
    else:
        _fail("Realtime accept payload builds with prompt + tools")
        ok = False

    sideband = realtime_sideband_url("call_test")
    if sideband.startswith("wss://api.openai.com/v1/realtime?call_id="):
        _pass("Sideband WebSocket URL format")
    else:
        _fail("Sideband WebSocket URL format", sideband)
        ok = False

    # Confirm callable wiring exists (not invoked against live providers).
    for name, obj in (
        ("accept_realtime_call", accept_realtime_call),
        ("start_sideband_monitor", start_sideband_monitor),
        ("handle_realtime_incoming_sip", handle_realtime_incoming_sip),
        ("verify_openai_webhook_signature", verify_openai_webhook_signature),
    ):
        if callable(obj):
            _pass(f"{name} available")
        else:
            _fail(f"{name} available")
            ok = False

    return ok


async def async_main() -> int:
    print("Synas Labs AI Calling Agent — preflight voice check")
    print("No real phone call will be placed.\n")

    # Load .env via Settings when present
    try:
        from app.config import get_settings

        get_settings.cache_clear()
        get_settings()
    except Exception:  # noqa: BLE001
        pass

    results = [
        check_env_names(),
        await check_database(),
        await check_redis(),
        check_fastapi_route(),
        check_voice_wiring(),
    ]
    ready = all(results)
    print()
    print(f"READY_FOR_MANUAL_CALL={'true' if ready else 'false'}")
    return 0 if ready else 1


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
