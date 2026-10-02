from app.voice.sip import SipClient, SipInboundCall, SipTransferRequest


class TransferService:
    def __init__(self) -> None:
        self.sip = SipClient()

    async def transfer_to_human(self, *, provider_call_id: str, destination: str) -> dict:
        return await self.sip.transfer(
            SipTransferRequest(provider_call_id=provider_call_id, destination=destination)
        )
