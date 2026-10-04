"""Production voice health. Reports configuration presence, never secret values."""

from __future__ import annotations

import logging
from typing import Any

import redis.asyncio as redis
from sqlalchemy import func, select

from app.config import get_settings
from app.db.session import AsyncSessionLocal
from app.models import Call, CallStatus
from app.voice.call_lifecycle import redact_secrets
from app.voice.concurrency import reconcile_redis_slots

logger = logging.getLogger(__name__)

WEBHOOK_PATH_TEMPLATE = "/api/v1/webhooks/openai/{tenant_slug}/inbound"
WEBHOOK_EVENT = "realtime.call.incoming"


def _configured(value: str) -> bool:
    text = (value or "").strip()
    if not text:
        return False
    lowered = text.lower()
    return lowered not in {"change-me-in-production", "change-me-jwt-secret"} and not lowered.startswith(
        "change-me"
    )


async def _postgres_ok() -> bool:
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(select(1))
        return True
    except Exception:  # noqa: BLE001
        logger.exception("Voice health postgres check failed")
        return False


async def _redis_ok() -> bool:
    client = None
    try:
        settings = get_settings()
        client = redis.from_url(
            settings.redis_url,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
        return bool(await client.ping())
    except Exception:  # noqa: BLE001
        logger.warning("Voice health redis check failed")
        return False
    finally:
        if client is not None:
            await client.aclose()


async def _call_counts() -> dict[str, int]:
    try:
        async with AsyncSessionLocal() as db:
            active = await db.execute(
                select(func.count()).select_from(Call).where(Call.status == CallStatus.ACTIVE)
            )
            ringing = await db.execute(
                select(func.count()).select_from(Call).where(Call.status == CallStatus.RINGING)
            )
            live_ids = await db.execute(
                select(Call.id).where(
                    Call.status.in_([CallStatus.ACTIVE, CallStatus.RINGING, CallStatus.QUEUED])
                )
            )
            active_ids = {str(call_id) for call_id in live_ids.scalars().all()}
            active_n = int(active.scalar_one())
            ringing_n = int(ringing.scalar_one())
        reconciled = await reconcile_redis_slots(active_call_ids=active_ids)
        return {
            "active_calls": active_n,
            "ringing_calls": ringing_n,
            "reconciled_slots": reconciled,
        }
    except Exception:  # noqa: BLE001
        logger.exception("Voice health call count failed")
        return {"active_calls": -1, "ringing_calls": -1, "reconciled_slots": 0}


async def build_voice_health() -> dict[str, Any]:
    settings = get_settings()
    postgres_ok = await _postgres_ok()
    redis_ok = await _redis_ok()
    counts = await _call_counts()
    openai_key = _configured(settings.openai_api_key)
    webhook_secret = _configured(settings.openai_webhook_secret)
    project = _configured(settings.openai_sip_project_id)
    model = (settings.openai_realtime_model or "").strip()
    webhook_valid = bool(webhook_secret and openai_key and project and model)
    ok = bool(postgres_ok and redis_ok and webhook_valid and counts["active_calls"] >= 0)
    report: dict[str, Any] = {
        "ok": ok,
        "status": "ok" if ok else "degraded",
        "railway_api": "ok",
        "postgres": "ok" if postgres_ok else "error",
        "redis": "ok" if redis_ok else "error",
        "openai": {
            "api_key_configured": openai_key,
            "sip_project_configured": project,
            "realtime_model": model or None,
        },
        "webhook": {
            "secret_configured": webhook_secret,
            "expected_event": WEBHOOK_EVENT,
            "path_template": WEBHOOK_PATH_TEMPLATE,
            "valid": webhook_valid,
        },
        "concurrency": {
            "max_concurrent_calls": settings.max_concurrent_calls,
            "active_calls": counts["active_calls"],
            "ringing_calls": counts["ringing_calls"],
            "reconciled_slots": counts.get("reconciled_slots", 0),
        },
    }
    # Defense: a future field must not leak a credential into this payload.
    redacted = redact_secrets(str(report), limit=4000)
    if "sk-" in redacted or "whsec_" in redacted:
        logger.error("Voice health report contained a secret-like token and was withheld")
        return {"ok": False, "status": "degraded", "error": "health_report_withheld"}
    return report
