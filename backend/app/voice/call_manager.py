from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Call, CallStatus, Tenant
from app.services import CallService
from app.voice.concurrency import acquire_concurrency_slot

logger = logging.getLogger(__name__)


@dataclass
class AdmissionDecision:
    accepted: bool
    reason: str
    active_calls: int
    limit: int


class CallManager:
    """Controls simultaneous calls per tenant. AI does not decide capacity."""

    def __init__(self, db: AsyncSession, tenant_id: UUID):
        self.db = db
        self.tenant_id = tenant_id
        self.calls = CallService(db, tenant_id)
        self.settings = get_settings()

    async def _limit(self) -> int:
        result = await self.db.execute(select(Tenant).where(Tenant.id == self.tenant_id))
        tenant = result.scalar_one_or_none()
        if tenant is None:
            return self.settings.max_concurrent_calls
        return min(tenant.max_concurrent_calls, self.settings.max_concurrent_calls)

    async def can_accept(self) -> AdmissionDecision:
        expired = await self.calls.reconcile_stale_sessions()
        if expired:
            logger.warning(
                "Expired %s stale capacity holds for tenant=%s before admission check",
                expired,
                self.tenant_id,
            )
        active = await self.calls.count_active()
        limit = await self._limit()
        if active >= limit:
            logger.warning(
                "Rejecting inbound call tenant=%s active=%s limit=%s (SIP will see Busy/486)",
                self.tenant_id,
                active,
                limit,
            )
            return AdmissionDecision(
                accepted=False,
                reason="concurrent_call_limit_reached",
                active_calls=active,
                limit=limit,
            )
        return AdmissionDecision(
            accepted=True,
            reason="ok",
            active_calls=active,
            limit=limit,
        )

    async def admit_inbound(
        self,
        *,
        from_number: str,
        to_number: str,
        provider_call_id: str | None = None,
        openai_session_id: str | None = None,
    ) -> tuple[AdmissionDecision, Call | None]:
        decision = await self.can_accept()
        if not decision.accepted:
            # Persist rejected attempt for ops visibility
            call = await self.calls.create_inbound(
                from_number=from_number,
                to_number=to_number,
                provider_call_id=provider_call_id,
                openai_session_id=openai_session_id,
            )
            await self.calls.set_status(
                call.id,
                CallStatus.REJECTED,
                failure_reason=decision.reason,
            )
            return decision, call

        call = await self.calls.create_inbound(
            from_number=from_number,
            to_number=to_number,
            provider_call_id=provider_call_id,
            openai_session_id=openai_session_id,
        )
        await acquire_concurrency_slot(tenant_id=self.tenant_id, call_id=call.id)
        # Stay PENDING/RINGING only until accept returns. The handler promotes ACTIVE.
        return decision, call
