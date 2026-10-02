"""OpenAI webhook signature verification (Standard Webhooks)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
from dataclasses import dataclass
from typing import Any


class InvalidOpenAIWebhookSignature(Exception):
    """Raised when webhook signature verification fails."""


_SIP_USER_RE = re.compile(r"sip:([^@;>]+)", re.IGNORECASE)


@dataclass(frozen=True)
class IncomingSipCall:
    event_id: str
    webhook_id: str
    openai_call_id: str
    from_number: str
    to_number: str
    provider_call_id: str | None
    sip_headers: list[dict[str, str]]
    raw_event: dict[str, Any]


def verify_openai_webhook_signature(
    *,
    payload: str | bytes,
    headers: dict[str, str],
    secret: str,
    tolerance_seconds: int = 300,
) -> None:
    """Verify OpenAI webhook authenticity using Standard Webhooks signing.

    Signed payload format: ``{webhook-id}.{webhook-timestamp}.{body}``.
    """
    if not secret:
        raise InvalidOpenAIWebhookSignature("OPENAI_WEBHOOK_SECRET is not configured")

    normalized = {str(k).lower(): str(v) for k, v in headers.items()}
    signature_header = normalized.get("webhook-signature")
    timestamp = normalized.get("webhook-timestamp")
    webhook_id = normalized.get("webhook-id")
    if not signature_header or not timestamp or not webhook_id:
        raise InvalidOpenAIWebhookSignature("Missing webhook signature headers")

    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise InvalidOpenAIWebhookSignature("Invalid webhook timestamp") from exc

    if abs(int(time.time()) - ts) > tolerance_seconds:
        raise InvalidOpenAIWebhookSignature("Webhook timestamp outside tolerance")

    signatures: list[str] = []
    for part in signature_header.split():
        if part.startswith("v1,"):
            signatures.append(part[3:])
        else:
            signatures.append(part)
    if not signatures:
        raise InvalidOpenAIWebhookSignature("Empty webhook signature")

    if secret.startswith("whsec_"):
        decoded_secret = base64.b64decode(secret[6:])
    else:
        decoded_secret = secret.encode("utf-8")

    body = payload.decode("utf-8") if isinstance(payload, bytes) else payload
    signed_payload = f"{webhook_id}.{timestamp}.{body}".encode("utf-8")
    expected = base64.b64encode(hmac.new(decoded_secret, signed_payload, hashlib.sha256).digest()).decode(
        "utf-8"
    )
    if not any(hmac.compare_digest(expected, sig) for sig in signatures):
        raise InvalidOpenAIWebhookSignature("Webhook signature mismatch")


def extract_phone_from_sip_header(value: str | None) -> str:
    """Extract a phone-like identifier from a SIP From/To header value.

    SIP headers are untrusted metadata only — never authorization.
    """
    if not value:
        return "unknown"
    match = _SIP_USER_RE.search(value)
    if match:
        user = match.group(1).strip()
        if user.lower().startswith("tel:"):
            user = user[4:]
        return user or "unknown"
    # Fallback: strip display name / angle brackets
    cleaned = value
    if "<" in cleaned and ">" in cleaned:
        cleaned = cleaned[cleaned.find("<") + 1 : cleaned.find(">")]
    cleaned = cleaned.replace("tel:", "").strip()
    return cleaned or "unknown"


def parse_sip_headers(headers: list[Any] | None) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in headers or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        if name:
            parsed[name.lower()] = value
    return parsed


def parse_realtime_incoming_event(
    event: dict[str, Any],
    *,
    webhook_id: str | None,
) -> IncomingSipCall:
    """Parse ``realtime.call.incoming`` into a safe internal structure."""
    if event.get("type") != "realtime.call.incoming":
        raise ValueError("unsupported_event_type")

    data = event.get("data") or {}
    openai_call_id = data.get("call_id")
    if not openai_call_id or not isinstance(openai_call_id, str):
        raise ValueError("missing_call_id")

    event_id = str(event.get("id") or "")
    if not event_id:
        raise ValueError("missing_event_id")

    sip_headers_raw = data.get("sip_headers") or []
    if not isinstance(sip_headers_raw, list):
        raise ValueError("malformed_sip_headers")

    headers = parse_sip_headers(sip_headers_raw)
    from_number = extract_phone_from_sip_header(headers.get("from"))
    to_number = extract_phone_from_sip_header(headers.get("to"))
    provider_call_id = headers.get("call-id")

    return IncomingSipCall(
        event_id=event_id,
        webhook_id=webhook_id or event_id,
        openai_call_id=openai_call_id,
        from_number=from_number,
        to_number=to_number,
        provider_call_id=provider_call_id,
        sip_headers=[{"name": k, "value": v} for k, v in headers.items()],
        raw_event=event,
    )
