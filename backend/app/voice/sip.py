"""SIP / Twilio telephony adapter.

Credentials are read from environment via Settings. Values are never logged.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

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
        return self.settings.telephony_hangup_configured or self.settings.sip_adapter_configured

    async def acknowledge_inbound(self, call: SipInboundCall) -> dict:
        if not self.configured:
            return {
                "ok": True,
                "mode": "stub",
                "message": "SIP provider not configured; acknowledged locally",
                "call": call.__dict__,
            }
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

    async def hangup(self, provider_call_id: str | None) -> dict:
        """End an active call via Twilio when configured.

        Returns ok=False on provider failure. Never includes secret values.
        """
        if not provider_call_id:
            return {
                "ok": False,
                "mode": "error",
                "error": "provider_call_id_missing",
                "message": "Cannot hang up without a provider call id",
            }

        if self.settings.twilio_hangup_configured:
            return await self._twilio_hangup(provider_call_id)

        if self.settings.sip_adapter_configured:
            # Generic SIP provider REST hangup is provider-specific; acknowledge attempt.
            return {
                "ok": True,
                "mode": "sip_provider",
                "provider_call_id": provider_call_id,
                "message": "SIP provider hangup acknowledged",
            }

        # Safe local fallback when telephony is not configured (dev/test).
        return {
            "ok": True,
            "mode": "stub",
            "provider_call_id": provider_call_id,
            "message": "Telephony not configured; local hangup fallback applied",
        }

    async def _twilio_hangup(self, provider_call_id: str) -> dict:
        account_sid = self.settings.twilio_account_sid
        url = (
            f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}"
            f"/Calls/{provider_call_id}.json"
        )
        if self.settings.twilio_api_key_sid and self.settings.twilio_api_key_secret:
            auth = (self.settings.twilio_api_key_sid, self.settings.twilio_api_key_secret)
        else:
            auth = (account_sid, self.settings.twilio_auth_token)

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(url, data={"Status": "completed"}, auth=auth)
        except httpx.HTTPError:
            return {
                "ok": False,
                "mode": "twilio",
                "provider_call_id": provider_call_id,
                "error": "twilio_request_failed",
                "message": "Twilio hangup request failed",
            }

        if response.status_code >= 400:
            return {
                "ok": False,
                "mode": "twilio",
                "provider_call_id": provider_call_id,
                "error": "twilio_hangup_rejected",
                "message": f"Twilio hangup rejected with HTTP {response.status_code}",
                "status_code": response.status_code,
            }

        return {
            "ok": True,
            "mode": "twilio",
            "provider_call_id": provider_call_id,
            "message": "Twilio hangup accepted",
            "status_code": response.status_code,
        }
