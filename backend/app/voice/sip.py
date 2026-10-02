"""SIP provider adapter stubs.

Wire a compatible telephony/SIP provider here. OpenAI Realtime receives media
via SIP; Synas backend owns auth, tools, transfer, and business decisions.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import get_settings


@dataclass
class SipInboundCall:
    provider_call_id: str
    from_number: str
    to_number: str


@dataclass
class SipTransferRequest:
    provider_call_id: str
    destination: str


class SipClient:
    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def configured(self) -> bool:
        return bool(self.settings.sip_provider_api_key and self.settings.sip_provider_base_url)

    async def acknowledge_inbound(self, call: SipInboundCall) -> dict:
        if not self.configured:
            return {
                "ok": True,
                "mode": "stub",
                "message": "SIP provider not configured; acknowledged locally",
                "call": call.__dict__,
            }
        # Placeholder for provider REST/webhook ack
        return {"ok": True, "mode": "provider", "call": call.__dict__}

    async def transfer(self, request: SipTransferRequest) -> dict:
        if not self.configured:
            return {
                "ok": True,
                "mode": "stub",
                "message": "SIP transfer stub — configure provider to enable live transfer",
                "request": request.__dict__,
            }
        return {"ok": True, "mode": "provider", "request": request.__dict__}

    async def hangup(self, provider_call_id: str) -> dict:
        if not self.configured:
            return {"ok": True, "mode": "stub", "provider_call_id": provider_call_id}
        return {"ok": True, "mode": "provider", "provider_call_id": provider_call_id}
