"""Per-call concurrency slots.

Capacity is counted in PostgreSQL. Redis keys exist so a crashed process cannot
pin a slot forever: every key has a TTL and is deleted in a finally path.
There is no caller-number lock. A redial is a new call id.
"""

from __future__ import annotations

import logging
from uuid import UUID

import redis.asyncio as redis

from app.config import get_settings
from app.voice.call_lifecycle import log_call_event

logger = logging.getLogger(__name__)

SLOT_TTL_SECONDS = 7200
_PREFIX = "voice:call-slot"


def slot_key(tenant_id: UUID, call_id: UUID) -> str:
    return f"{_PREFIX}:{tenant_id}:{call_id}"


async def _client():
    settings = get_settings()
    return redis.from_url(
        settings.redis_url,
        socket_connect_timeout=0.5,
        socket_timeout=0.5,
    )


async def acquire_concurrency_slot(*, tenant_id: UUID, call_id: UUID) -> bool:
    """Mark this call id as holding a slot. Fail open if Redis is down."""
    client = None
    try:
        client = await _client()
        stored = await client.set(slot_key(tenant_id, call_id), "1", nx=True, ex=SLOT_TTL_SECONDS)
        log_call_event(
            "CONCURRENCY_SLOT_ACQUIRED",
            tenant_id=tenant_id,
            call_id=call_id,
            redis=bool(stored),
        )
        return True
    except Exception:  # noqa: BLE001 - DB capacity remains authoritative
        logger.warning(
            "CONCURRENCY_SLOT_ACQUIRED tenant_id=%s call_id=%s redis=unavailable",
            tenant_id,
            call_id,
        )
        return True
    finally:
        if client is not None:
            await client.aclose()


async def release_concurrency_slot(*, tenant_id: UUID, call_id: UUID) -> None:
    """Idempotent. A missing key is a successful release."""
    client = None
    try:
        client = await _client()
        await client.delete(slot_key(tenant_id, call_id))
        log_call_event(
            "CONCURRENCY_SLOT_RELEASED",
            tenant_id=tenant_id,
            call_id=call_id,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "CONCURRENCY_SLOT_RELEASED tenant_id=%s call_id=%s redis=unavailable",
            tenant_id,
            call_id,
        )
    finally:
        if client is not None:
            await client.aclose()


async def reconcile_redis_slots(*, active_call_ids: set[str]) -> int:
    """Delete slot keys whose call is no longer active. Returns removed count."""
    client = None
    removed = 0
    try:
        client = await _client()
        async for key in client.scan_iter(match=f"{_PREFIX}:*", count=100):
            raw = key.decode("utf-8") if isinstance(key, bytes) else str(key)
            call_id = raw.rsplit(":", 1)[-1]
            if call_id in active_call_ids:
                continue
            await client.delete(key)
            removed += 1
        if removed:
            log_call_event("CONCURRENCY_SLOT_RELEASED", reconciled=removed)
        return removed
    except Exception:  # noqa: BLE001
        logger.warning("Redis slot reconciliation skipped")
        return 0
    finally:
        if client is not None:
            await client.aclose()
