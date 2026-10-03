"""Human handoff transfer entrypoint used by tools and telephony adapters."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.voice.handoff import prepare_handoff, redirect_active_call


class TransferService:
    async def transfer_to_human(
        self,
        db: AsyncSession,
        *,
        tenant_id: UUID,
        call_id: UUID,
        reason: str,
        department: str | None = None,
        context: dict | None = None,
        redirect_now: bool = False,
    ) -> dict:
        prepared = await prepare_handoff(
            db,
            tenant_id=tenant_id,
            call_id=call_id,
            reason=reason,
            department=department,
            context=context,
        )
        if not prepared.get("ok"):
            return prepared
        if redirect_now:
            redirected = await redirect_active_call(tenant_id=tenant_id, call_id=call_id)
            return {**prepared, "redirect": redirected}
        return prepared
