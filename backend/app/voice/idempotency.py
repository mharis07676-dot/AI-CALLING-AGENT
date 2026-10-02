"""Idempotency helpers for OpenAI webhooks and tool events."""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import IdempotencyKey


async def claim_idempotency(
    db: AsyncSession,
    *,
    scope: str,
    key: str,
    tenant_id: UUID | None = None,
    call_id: UUID | None = None,
) -> bool:
    """Attempt to claim a unique key. Returns False if already processed."""
    stmt = (
        insert(IdempotencyKey)
        .values(
            id=uuid4(),
            tenant_id=tenant_id,
            call_id=call_id,
            scope=scope,
            key=key,
        )
        .on_conflict_do_nothing(constraint="uq_idempotency_scope_key")
        .returning(IdempotencyKey.id)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none() is not None


async def release_idempotency(db: AsyncSession, *, scope: str, key: str) -> None:
    """Release a claim so a failed accept can be retried safely."""
    await db.execute(
        delete(IdempotencyKey).where(IdempotencyKey.scope == scope, IdempotencyKey.key == key)
    )
