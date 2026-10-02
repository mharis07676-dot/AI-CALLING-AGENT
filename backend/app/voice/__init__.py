"""Voice / telephony package."""

__all__ = [
    "AdmissionDecision",
    "CallManager",
    "SipClient",
    "SipInboundCall",
    "SipTransferRequest",
    "TransferService",
    "build_realtime_session_config",
    "build_accept_payload",
    "create_realtime_session_stub",
    "accept_realtime_call",
    "reject_realtime_call",
    "handle_realtime_incoming_sip",
    "start_sideband_monitor",
]


def __getattr__(name: str):
    if name in {"AdmissionDecision", "CallManager"}:
        from app.voice.call_manager import AdmissionDecision, CallManager

        return {"AdmissionDecision": AdmissionDecision, "CallManager": CallManager}[name]
    if name in {"SipClient", "SipInboundCall", "SipTransferRequest"}:
        from app.voice import sip

        return getattr(sip, name)
    if name == "TransferService":
        from app.voice.transfer import TransferService

        return TransferService
    if name in {
        "build_realtime_session_config",
        "build_accept_payload",
        "create_realtime_session_stub",
        "accept_realtime_call",
        "reject_realtime_call",
    }:
        from app.voice import realtime

        return getattr(realtime, name)
    if name == "handle_realtime_incoming_sip":
        from app.voice.inbound_sip import handle_realtime_incoming_sip

        return handle_realtime_incoming_sip
    if name == "start_sideband_monitor":
        from app.voice.session_monitor import start_sideband_monitor

        return start_sideband_monitor
    raise AttributeError(f"module {__name__!r} has no attribute {name}")
