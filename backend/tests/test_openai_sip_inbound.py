"""Tests for OpenAI Realtime SIP inbound accept + sideband wiring."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import time
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from websockets.exceptions import ConnectionClosed

# Python 3.14 local envs may lack asyncpg wheels; stub before app.db imports.
if "asyncpg" not in sys.modules:
    sys.modules["asyncpg"] = types.ModuleType("asyncpg")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.webhooks import router as webhooks_router
from app.config import Settings
from app.db.session import get_db
from app.models import CallStatus
from app.voice.openai_webhook import (
    InvalidOpenAIWebhookSignature,
    extract_phone_from_sip_header,
    parse_realtime_incoming_event,
    verify_openai_webhook_signature,
)
from app.voice.realtime import accept_realtime_call, build_accept_payload
from app.voice.session_monitor import _handle_event, _monitor_with_retries


def _sign(payload: str, *, secret: str, webhook_id: str, timestamp: str) -> str:
    if secret.startswith("whsec_"):
        key = base64.b64decode(secret[6:])
    else:
        key = secret.encode("utf-8")
    signed = f"{webhook_id}.{timestamp}.{payload}".encode("utf-8")
    digest = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("utf-8")
    return f"v1,{digest}"


def _incoming_event(call_id: str = "rtc_test_call") -> dict:
    return {
        "object": "event",
        "id": "evt_test_incoming_1",
        "type": "realtime.call.incoming",
        "created_at": int(time.time()),
        "data": {
            "call_id": call_id,
            "sip_headers": [
                {"name": "From", "value": "sip:+14255550100@sip.example.com"},
                {"name": "To", "value": "sip:+18005550100@sip.example.com"},
                {"name": "Call-ID", "value": "provider-call-abc"},
            ],
        },
    }


def test_verify_valid_signature():
    secret = "whsec_" + base64.b64encode(b"test-secret").decode("utf-8")
    payload = json.dumps(_incoming_event())
    webhook_id = "wh_abc"
    timestamp = str(int(time.time()))
    signature = _sign(payload, secret=secret, webhook_id=webhook_id, timestamp=timestamp)
    verify_openai_webhook_signature(
        payload=payload,
        headers={
            "webhook-id": webhook_id,
            "webhook-timestamp": timestamp,
            "webhook-signature": signature,
        },
        secret=secret,
    )


def test_verify_invalid_signature():
    secret = "whsec_" + base64.b64encode(b"test-secret").decode("utf-8")
    payload = json.dumps(_incoming_event())
    with pytest.raises(InvalidOpenAIWebhookSignature):
        verify_openai_webhook_signature(
            payload=payload,
            headers={
                "webhook-id": "wh_abc",
                "webhook-timestamp": str(int(time.time())),
                "webhook-signature": "v1,not-a-real-signature",
            },
            secret=secret,
        )


def test_parse_incoming_extracts_call_and_phones():
    incoming = parse_realtime_incoming_event(_incoming_event("rtc_123"), webhook_id="wh_1")
    assert incoming.openai_call_id == "rtc_123"
    assert incoming.from_number == "+14255550100"
    assert incoming.to_number == "+18005550100"
    assert incoming.provider_call_id == "provider-call-abc"
    assert extract_phone_from_sip_header("sip:+1999@x") == "+1999"


def test_accept_payload_uses_synas_instructions():
    tenant_id = uuid4()
    call_id = uuid4()
    payload = build_accept_payload(tenant_id=tenant_id, call_id=call_id, voice="alloy")
    assert payload["type"] == "realtime"
    assert payload["audio"]["output"]["voice"] == "alloy"
    assert payload["audio"]["input"]["turn_detection"]["type"] == "server_vad"
    assert "modalities" not in payload
    assert "output_modalities" not in payload
    assert "metadata" not in payload
    assert "Synas Labs" in payload["instructions"]
    assert "NEVER invent" in payload["instructions"] or "Never make up" in payload["instructions"]
    assert str(tenant_id) in payload["instructions"]
    assert str(call_id) in payload["instructions"]
    assert any(t["name"] == "register_opt_out" for t in payload["tools"])
    dumped = json.dumps(payload)
    assert "sk-" not in dumped
    assert "password" not in dumped.lower() or "sip_password" not in dumped.lower()


@pytest.mark.asyncio
async def test_accept_realtime_call_success():
    mock_response = MagicMock()
    mock_response.status_code = 200
    client = AsyncMock()
    client.post = AsyncMock(return_value=mock_response)

    with patch("app.voice.realtime.get_settings") as gs:
        gs.return_value = Settings(
            openai_api_key="sk-test",
            openai_sip_project_id="proj_MoUy5Ex56hBm3sf6DtZ4XIFF",
        )
        result = await accept_realtime_call(
            openai_call_id="rtc_1",
            session_config={"type": "realtime", "model": "gpt-realtime"},
            client=client,
        )
    assert result["ok"] is True
    client.post.assert_awaited()
    args, kwargs = client.post.await_args
    assert args[0].endswith("/realtime/calls/rtc_1/accept")
    assert "Authorization" in kwargs["headers"]
    assert "sk-test" in kwargs["headers"]["Authorization"]
    assert kwargs["headers"]["OpenAI-Project"] == "proj_MoUy5Ex56hBm3sf6DtZ4XIFF"


@pytest.mark.asyncio
async def test_accept_realtime_call_failure():
    mock_response = MagicMock()
    mock_response.status_code = 500
    client = AsyncMock()
    client.post = AsyncMock(return_value=mock_response)

    with patch("app.voice.realtime.get_settings") as gs:
        gs.return_value = Settings(openai_api_key="sk-test")
        result = await accept_realtime_call(
            openai_call_id="rtc_1",
            session_config={"type": "realtime"},
            client=client,
        )
    assert result["ok"] is False
    assert result["error"] == "openai_accept_rejected"


def _make_client(*, tenant, settings, handler_result=None) -> tuple[TestClient, list]:
    app = FastAPI()
    app.include_router(webhooks_router, prefix="/api/v1")

    async def override_db():
        db = AsyncMock()
        db.execute = AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=MagicMock(return_value=tenant))
        )
        yield db

    app.dependency_overrides[get_db] = override_db
    patches = [patch("app.api.webhooks.get_settings", return_value=settings)]
    if handler_result is not None:
        patches.append(
            patch(
                "app.api.webhooks.handle_realtime_incoming_sip",
                new=AsyncMock(return_value=handler_result),
            )
        )
    for p in patches:
        p.start()
    return TestClient(app), patches


def _stop_patches(patches: list) -> None:
    for p in patches:
        p.stop()


def test_webhook_valid_incoming_accepts():
    secret = "whsec_" + base64.b64encode(b"test-secret").decode("utf-8")
    payload = json.dumps(_incoming_event("rtc_ok"))
    webhook_id = "wh_ok"
    timestamp = str(int(time.time()))
    signature = _sign(payload, secret=secret, webhook_id=webhook_id, timestamp=timestamp)
    tenant = SimpleNamespace(id=uuid4(), slug="synas", is_active=True)
    call_id = uuid4()

    client, patches = _make_client(
        tenant=tenant,
        settings=Settings(openai_webhook_secret=secret, openai_api_key="sk-test"),
        handler_result={
            "ok": True,
            "accepted": True,
            "duplicate": False,
            "call_id": str(call_id),
            "openai_call_id": "rtc_ok",
            "status": "active",
            "message": "accepted",
        },
    )
    try:
        response = client.post(
            "/api/v1/webhooks/openai/synas/inbound",
            content=payload,
            headers={
                "content-type": "application/json",
                "webhook-id": webhook_id,
                "webhook-timestamp": timestamp,
                "webhook-signature": signature,
            },
        )
    finally:
        _stop_patches(patches)

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["openai_call_id"] == "rtc_ok"
    assert "sk-test" not in response.text
    assert secret not in response.text


def test_webhook_invalid_signature_rejected():
    secret = "whsec_" + base64.b64encode(b"test-secret").decode("utf-8")
    payload = json.dumps(_incoming_event())
    tenant = SimpleNamespace(id=uuid4(), slug="synas", is_active=True)
    client, patches = _make_client(
        tenant=tenant,
        settings=Settings(openai_webhook_secret=secret),
    )
    try:
        response = client.post(
            "/api/v1/webhooks/openai/synas/inbound",
            content=payload,
            headers={
                "content-type": "application/json",
                "webhook-id": "wh_x",
                "webhook-timestamp": str(int(time.time())),
                "webhook-signature": "v1,bad",
            },
        )
    finally:
        _stop_patches(patches)
    assert response.status_code == 400
    assert "Invalid webhook signature" in response.text
    assert secret not in response.text


def test_webhook_duplicate_delivery():
    secret = "whsec_" + base64.b64encode(b"test-secret").decode("utf-8")
    payload = json.dumps(_incoming_event("rtc_dup"))
    webhook_id = "wh_dup"
    timestamp = str(int(time.time()))
    signature = _sign(payload, secret=secret, webhook_id=webhook_id, timestamp=timestamp)
    tenant = SimpleNamespace(id=uuid4(), slug="synas", is_active=True)

    client, patches = _make_client(
        tenant=tenant,
        settings=Settings(openai_webhook_secret=secret, openai_api_key="sk-test"),
        handler_result={
            "ok": True,
            "accepted": True,
            "duplicate": True,
            "call_id": str(uuid4()),
            "openai_call_id": "rtc_dup",
            "message": "Duplicate webhook delivery ignored",
        },
    )
    try:
        response = client.post(
            "/api/v1/webhooks/openai/synas/inbound",
            content=payload,
            headers={
                "content-type": "application/json",
                "webhook-id": webhook_id,
                "webhook-timestamp": timestamp,
                "webhook-signature": signature,
            },
        )
    finally:
        _stop_patches(patches)
    assert response.status_code == 200
    assert response.json()["duplicate"] is True


def test_webhook_accept_failure_returns_502():
    secret = "whsec_" + base64.b64encode(b"test-secret").decode("utf-8")
    payload = json.dumps(_incoming_event("rtc_fail"))
    webhook_id = "wh_fail"
    timestamp = str(int(time.time()))
    signature = _sign(payload, secret=secret, webhook_id=webhook_id, timestamp=timestamp)
    tenant = SimpleNamespace(id=uuid4(), slug="synas", is_active=True)

    client, patches = _make_client(
        tenant=tenant,
        settings=Settings(openai_webhook_secret=secret, openai_api_key="sk-test"),
        handler_result={
            "ok": False,
            "accepted": False,
            "call_id": str(uuid4()),
            "openai_call_id": "rtc_fail",
            "error": "openai_accept_rejected",
            "message": "OpenAI accept failed",
        },
    )
    try:
        response = client.post(
            "/api/v1/webhooks/openai/synas/inbound",
            content=payload,
            headers={
                "content-type": "application/json",
                "webhook-id": webhook_id,
                "webhook-timestamp": timestamp,
                "webhook-signature": signature,
            },
        )
    finally:
        _stop_patches(patches)
    assert response.status_code == 502


@pytest.mark.asyncio
async def test_duplicate_tool_event_does_not_execute_twice():
    tenant_id = uuid4()
    call_id = uuid4()
    ws = AsyncMock()
    event = {
        "type": "response.function_call_arguments.done",
        "event_id": "evt_tool_1",
        "call_id": "fc_1",
        "name": "search_properties",
        "arguments": json.dumps({"purpose": "rent"}),
    }

    claim = AsyncMock(side_effect=[True, False])
    executor = AsyncMock()
    executor.execute = AsyncMock(
        return_value=SimpleNamespace(
            success=True,
            speakable_summary="ok",
            data={"count": 0},
            error=None,
        )
    )

    with (
        patch("app.voice.session_monitor.claim_idempotency", claim),
        patch("app.voice.session_monitor.ToolExecutor", return_value=executor),
        patch("app.voice.session_monitor.AsyncSessionLocal") as session_cm,
    ):
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        session.commit = AsyncMock()
        session_cm.return_value = session

        await _handle_event(
            ws=ws,
            event=event,
            tenant_id=tenant_id,
            call_id=call_id,
            openai_call_id="rtc_x",
        )
        await _handle_event(
            ws=ws,
            event=event,
            tenant_id=tenant_id,
            call_id=call_id,
            openai_call_id="rtc_x",
        )

    assert executor.execute.await_count == 1


@pytest.mark.asyncio
async def test_user_transcript_creates_message():
    tenant_id = uuid4()
    call_id = uuid4()
    ws = AsyncMock()
    calls = AsyncMock()
    calls.add_message = AsyncMock()

    with (
        patch("app.voice.session_monitor.claim_idempotency", AsyncMock(return_value=True)),
        patch("app.voice.session_monitor.CallService", return_value=calls),
        patch("app.voice.session_monitor.AsyncSessionLocal") as session_cm,
    ):
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        session.commit = AsyncMock()
        session_cm.return_value = session

        await _handle_event(
            ws=ws,
            event={
                "type": "conversation.item.input_audio_transcription.completed",
                "event_id": "evt_user_1",
                "transcript": "Mujhe DHA mein ghar chahiye",
            },
            tenant_id=tenant_id,
            call_id=call_id,
            openai_call_id="rtc_x",
        )

    calls.add_message.assert_awaited_with(
        call_id, role="user", content="Mujhe DHA mein ghar chahiye"
    )


@pytest.mark.asyncio
async def test_assistant_transcript_creates_message():
    tenant_id = uuid4()
    call_id = uuid4()
    ws = AsyncMock()
    calls = AsyncMock()
    calls.add_message = AsyncMock()

    with (
        patch("app.voice.session_monitor.claim_idempotency", AsyncMock(return_value=True)),
        patch("app.voice.session_monitor.CallService", return_value=calls),
        patch("app.voice.session_monitor.AsyncSessionLocal") as session_cm,
    ):
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        session.commit = AsyncMock()
        session_cm.return_value = session

        await _handle_event(
            ws=ws,
            event={
                "type": "response.audio_transcript.done",
                "event_id": "evt_asst_1",
                "transcript": "Bilkul. Budget kitna hai?",
            },
            tenant_id=tenant_id,
            call_id=call_id,
            openai_call_id="rtc_x",
        )

    calls.add_message.assert_awaited_with(
        call_id, role="assistant", content="Bilkul. Budget kitna hai?"
    )


@pytest.mark.asyncio
async def test_failed_websocket_does_not_fake_completion():
    tenant_id = uuid4()
    call_id = uuid4()
    mark_completed = AsyncMock()
    mark_failed = AsyncMock()

    with (
        patch(
            "app.voice.session_monitor._run_sideband_session",
            AsyncMock(side_effect=RuntimeError("ws down")),
        ),
        patch("app.voice.session_monitor._mark_completed", mark_completed),
        patch("app.voice.session_monitor._mark_monitor_failed", mark_failed),
        patch("app.voice.session_monitor.hangup_realtime_call", AsyncMock(return_value={"ok": True})),
        patch("app.voice.session_monitor.RETRY_DELAY_SECONDS", 0),
        patch("app.voice.session_monitor.MAX_WS_RETRIES", 2),
    ):
        await _monitor_with_retries(
            tenant_id=tenant_id,
            call_id=call_id,
            openai_call_id="rtc_ws_fail",
        )

    mark_completed.assert_not_awaited()
    mark_failed.assert_awaited()


@pytest.mark.asyncio
async def test_sideband_close_after_attach_marks_completed():
    tenant_id = uuid4()
    call_id = uuid4()
    mark_completed = AsyncMock()
    mark_failed = AsyncMock()
    hangup = AsyncMock()

    async def _close(**kwargs):
        kwargs["attached"]["ok"] = True
        raise ConnectionClosed(None, None)

    with (
        patch("app.voice.session_monitor._run_sideband_session", _close),
        patch("app.voice.session_monitor._mark_completed", mark_completed),
        patch("app.voice.session_monitor._mark_monitor_failed", mark_failed),
        patch("app.voice.session_monitor.hangup_realtime_call", hangup),
    ):
        await _monitor_with_retries(
            tenant_id=tenant_id,
            call_id=call_id,
            openai_call_id="rtc_talked",
        )

    mark_completed.assert_awaited_once()
    mark_failed.assert_not_awaited()
    hangup.assert_not_awaited()


@pytest.mark.asyncio
async def test_sideband_error_after_attach_does_not_fail_the_call():
    """A tool/DB error mid-call must not be stored as realtime_websocket_attach_failed."""
    tenant_id = uuid4()
    call_id = uuid4()
    mark_completed = AsyncMock()
    mark_failed = AsyncMock()
    hangup = AsyncMock()

    async def _boom(**kwargs):
        kwargs["attached"]["ok"] = True
        raise RuntimeError("leads_customer_id_fkey")

    with (
        patch("app.voice.session_monitor._run_sideband_session", _boom),
        patch("app.voice.session_monitor._mark_completed", mark_completed),
        patch("app.voice.session_monitor._mark_monitor_failed", mark_failed),
        patch("app.voice.session_monitor.hangup_realtime_call", hangup),
        patch("app.voice.session_monitor.RETRY_DELAY_SECONDS", 0),
        patch("app.voice.session_monitor.MAX_WS_RETRIES", 2),
    ):
        await _monitor_with_retries(
            tenant_id=tenant_id,
            call_id=call_id,
            openai_call_id="rtc_talked_then_db_error",
        )

    mark_completed.assert_awaited_once()
    mark_failed.assert_not_awaited()
    hangup.assert_not_awaited()


@pytest.mark.asyncio
async def test_tenant_isolation_on_inbound_handler():
    """Handler always uses the tenant resolved from the URL slug, not SIP headers."""
    from app.voice.inbound_sip import handle_realtime_incoming_sip

    tenant_a = uuid4()
    tenant_b = uuid4()
    db = AsyncMock()

    manager = AsyncMock()
    call = SimpleNamespace(id=uuid4(), status=CallStatus.RINGING)
    manager.admit_inbound = AsyncMock(
        return_value=(SimpleNamespace(accepted=True, reason="ok"), call)
    )

    calls = AsyncMock()
    calls.add_event = AsyncMock()
    calls.set_status = AsyncMock()
    calls.ensure_conversation = AsyncMock()
    calls.get_by_openai_session_id = AsyncMock(return_value=None)
    calls.get_open_for_caller = AsyncMock(return_value=None)

    agent = SimpleNamespace(
        voice="alloy",
        system_instructions="Synas Labs",
        supported_languages=["English"],
    )

    with (
        patch("app.voice.inbound_sip.claim_idempotency", AsyncMock(return_value=True)),
        patch("app.voice.inbound_sip.CallManager", return_value=manager) as cm_cls,
        patch("app.voice.inbound_sip.CallService", return_value=calls),
        patch("app.voice.inbound_sip.AgentConfigService") as agent_cls,
        patch(
            "app.voice.inbound_sip.accept_realtime_call",
            AsyncMock(return_value={"ok": True, "status_code": 200}),
        ),
        patch("app.voice.inbound_sip.start_sideband_monitor", AsyncMock(return_value=True)),
        patch(
            "app.voice.inbound_sip.get_settings",
            lambda: Settings(openai_api_key="sk-test", openai_realtime_model="gpt-realtime"),
        ),
    ):
        agent_cls.return_value.get = AsyncMock(return_value=agent)
        result = await handle_realtime_incoming_sip(
            db,
            tenant_id=tenant_a,
            event=_incoming_event("rtc_tenant"),
            webhook_id="wh_tenant",
        )

    assert result["accepted"] is True
    assert tenant_a != tenant_b
    cm_cls.assert_called_with(db, tenant_a)
    agent_cls.assert_called_with(db, tenant_a)


def test_legacy_sip_route_rejects_non_openai_payload():
    tenant = SimpleNamespace(id=uuid4(), slug="synas", is_active=True)
    client, patches = _make_client(
        tenant=tenant,
        settings=Settings(openai_webhook_secret="whsec_x"),
    )
    try:
        response = client.post(
            "/api/v1/webhooks/sip/synas/inbound",
            json={"from_number": "+1", "to_number": "+2"},
        )
    finally:
        _stop_patches(patches)
    assert response.status_code == 410
    assert "openai" in response.text.lower()


def test_secrets_never_appear_in_webhook_error_response():
    secret = "whsec_" + base64.b64encode(b"super-secret-value").decode("utf-8")
    payload = json.dumps(_incoming_event("rtc_sec"))
    webhook_id = "wh_sec"
    timestamp = str(int(time.time()))
    signature = _sign(payload, secret=secret, webhook_id=webhook_id, timestamp=timestamp)
    tenant = SimpleNamespace(id=uuid4(), slug="synas", is_active=True)

    client, patches = _make_client(
        tenant=tenant,
        settings=Settings(
            openai_webhook_secret=secret,
            openai_api_key="sk-live-SHOULD-NOT-LEAK",
        ),
        handler_result={
            "ok": False,
            "accepted": False,
            "error": "openai_accept_rejected",
            "message": "OpenAI accept failed",
            "call_id": str(uuid4()),
            "openai_call_id": "rtc_sec",
        },
    )
    try:
        response = client.post(
            "/api/v1/webhooks/openai/synas/inbound",
            content=payload,
            headers={
                "content-type": "application/json",
                "webhook-id": webhook_id,
                "webhook-timestamp": timestamp,
                "webhook-signature": signature,
            },
        )
    finally:
        _stop_patches(patches)
    assert "sk-live-SHOULD-NOT-LEAK" not in response.text
    assert "super-secret-value" not in response.text
    assert secret not in response.text
