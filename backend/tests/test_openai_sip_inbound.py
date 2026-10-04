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
from websockets.exceptions import ConnectionClosed, InvalidStatus

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
from app.ai.voice_agent_prompt import BRAND_PRONUNCIATION_GUIDANCE
from app.voice.realtime import (
    BILINGUAL_GREETING,
    ENGLISH_GREETING,
    URDU_GREETING,
    accept_realtime_call,
    build_accept_payload,
    greeting_speak_instructions,
    normalize_preferred_language,
    select_initial_greeting,
)
from app.voice.session_monitor import _LatencyProbe, _handle_event, _monitor_with_retries


def _sign(payload: str, *, secret: str, webhook_id: str, timestamp: str) -> str:
    if secret.startswith("whsec_"):
        key = base64.b64decode(secret[6:])
    else:
        key = secret.encode("utf-8")
    signed = f"{webhook_id}.{timestamp}.{payload}".encode("utf-8")
    digest = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("utf-8")
    return f"v1,{digest}"


def _invalid_status(code: int) -> InvalidStatus:
    exc = InvalidStatus.__new__(InvalidStatus)
    exc.response = SimpleNamespace(status_code=code)
    return exc


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
    with patch(
        "app.voice.realtime.get_settings",
        return_value=Settings(
            openai_api_key="sk-test",
            openai_realtime_model="gpt-realtime",
            openai_realtime_voice="alloy",
        ),
    ):
        payload = build_accept_payload(tenant_id=tenant_id, call_id=call_id, voice="echo")
    assert payload["type"] == "realtime"
    assert payload["audio"]["output"]["voice"] == "alloy"
    td = payload["audio"]["input"]["turn_detection"]
    assert td["type"] == "server_vad"
    assert td["threshold"] == 0.65
    assert td["prefix_padding_ms"] == 200
    assert td["silence_duration_ms"] == 250
    # Unknown language at accept: gate first reply on transcript lock.
    assert td["create_response"] is False
    assert td["interrupt_response"] is True
    assert payload["audio"]["input"]["noise_reduction"] == {"type": "near_field"}
    assert payload.get("reasoning") == {"effort": "low"}
    assert "modalities" not in payload
    assert "output_modalities" not in payload
    assert "metadata" not in payload
    assert "Synas Labs" in payload["instructions"]
    assert BRAND_PRONUNCIATION_GUIDANCE in payload["instructions"]
    assert "Saaw-ay-nus" not in BILINGUAL_GREETING
    assert "NEVER invent" in payload["instructions"] or "Never invent" in payload["instructions"] or "Never make up" in payload["instructions"]
    assert "call_language: unknown" in payload["instructions"]
    assert "Never choose or change the conversation language yourself." in payload["instructions"]
    assert "match their language" not in payload["instructions"]
    assert "language" not in payload["audio"]["input"]["transcription"]
    assert "Do not translate" in payload["audio"]["input"]["transcription"]["prompt"]
    assert BILINGUAL_GREETING in payload["instructions"]
    assert "# Conversation Style" in payload["instructions"] or "NATURAL VOICE BEHAVIOR" in payload["instructions"]
    assert str(tenant_id) in payload["instructions"]
    assert str(call_id) in payload["instructions"]
    assert any(t["name"] == "register_opt_out" for t in payload["tools"])
    assert any(t["name"] == "wait_for_user" for t in payload["tools"])
    dumped = json.dumps(payload)
    assert "sk-" not in dumped
    assert "password" not in dumped.lower() or "sip_password" not in dumped.lower()


def test_preferred_language_greeting_selection():
    assert normalize_preferred_language("roman_urdu") is None
    assert normalize_preferred_language(None) is None
    assert normalize_preferred_language("english") == "en"
    assert normalize_preferred_language("urdu") == "ur"

    assert select_initial_greeting(None) == ("unknown", BILINGUAL_GREETING)
    assert select_initial_greeting("roman_urdu") == ("unknown", BILINGUAL_GREETING)
    assert select_initial_greeting("english") == ("en", ENGLISH_GREETING)
    assert select_initial_greeting("urdu") == ("ur", URDU_GREETING)

    for greeting in (BILINGUAL_GREETING, ENGLISH_GREETING, URDU_GREETING):
        assert "Synas Labs" in greeting
        assert "Saaw-ay-nus" not in greeting
        spoken = greeting_speak_instructions(greeting)
        assert f'Greeting: "{greeting}"' in spoken
        assert BRAND_PRONUNCIATION_GUIDANCE in spoken
        assert "this is Saaw-ay-nus" not in spoken

    en_payload = build_accept_payload(
        tenant_id=uuid4(), call_id=uuid4(), preferred_language="english"
    )
    assert 'call_language: "en"' in en_payload["instructions"]
    assert "Respond only in natural English." in en_payload["instructions"]
    assert f'"{ENGLISH_GREETING}"' in en_payload["instructions"]
    assert en_payload["audio"]["input"]["turn_detection"]["create_response"] is True

    ur_payload = build_accept_payload(
        tenant_id=uuid4(), call_id=uuid4(), preferred_language="urdu"
    )
    assert 'call_language: "ur"' in ur_payload["instructions"]
    assert "Respond in natural Pakistani Urdu." in ur_payload["instructions"]
    assert f'"{URDU_GREETING}"' in ur_payload["instructions"]
    assert ur_payload["audio"]["input"]["turn_detection"]["create_response"] is True


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
async def test_latency_probe_tracks_speech_to_first_audio():
    tenant_id = uuid4()
    call_id = uuid4()
    ws = AsyncMock()
    latency = _LatencyProbe()

    for event_type in (
        "input_audio_buffer.speech_started",
        "input_audio_buffer.speech_stopped",
        "response.created",
        "response.output_audio.delta",
        "response.output_audio.delta",
    ):
        await _handle_event(
            ws=ws,
            event={"type": event_type, "delta": "x"},
            tenant_id=tenant_id,
            call_id=call_id,
            openai_call_id="rtc_latency",
            latency=latency,
        )

    assert latency.speech_started_at is not None
    assert latency.speech_stopped_at is not None
    assert latency.response_created_at is not None
    assert latency.first_audio_delta_at is not None
    assert latency.first_audio_delta_at >= latency.response_created_at
    assert latency.vad_to_response_ms is not None
    assert latency.response_to_first_audio_ms is not None
    assert latency.turn_end_to_first_audio_ms is not None
    assert latency.eos_to_created_ms == latency.vad_to_response_ms
    assert latency.turn_end_to_first_audio_ms >= latency.vad_to_response_ms
    assert latency.logged is True


def test_accept_payload_keeps_silence_duration_baseline_250():
    payload = build_accept_payload(tenant_id=uuid4(), call_id=uuid4())
    assert payload["audio"]["input"]["turn_detection"]["silence_duration_ms"] == 250
    assert payload["audio"]["input"]["turn_detection"]["type"] == "server_vad"
    assert "semantic_vad" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_failed_websocket_does_not_fake_completion():
    tenant_id = uuid4()
    call_id = uuid4()
    mark_completed = AsyncMock()
    mark_failed = AsyncMock()
    hangup = AsyncMock(return_value={"ok": True})

    with (
        patch(
            "app.voice.session_monitor._run_sideband_session",
            AsyncMock(side_effect=RuntimeError("ws down")),
        ),
        patch("app.voice.session_monitor._mark_completed", mark_completed),
        patch("app.voice.session_monitor._mark_monitor_failed", mark_failed),
        patch("app.voice.session_monitor._mark_control_disconnected", AsyncMock()),
        patch("app.voice.session_monitor._call_is_terminal", AsyncMock(return_value=False)),
        patch("app.voice.session_monitor.RETRY_DELAY_SECONDS", 0),
        patch("app.voice.session_monitor.MAX_WS_RETRIES", 2),
    ):
        await _monitor_with_retries(
            tenant_id=tenant_id,
            call_id=call_id,
            openai_call_id="rtc_ws_fail",
        )

    mark_completed.assert_not_awaited()
    mark_failed.assert_not_awaited()
    hangup.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_sideband_404_retries_without_hangup():
    tenant_id = uuid4()
    call_id = uuid4()
    mark_completed = AsyncMock()
    mark_failed = AsyncMock()
    hangup = AsyncMock()
    attempts = {"n": 0}

    async def _session(**kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _invalid_status(404)
        kwargs["attached"]["ok"] = True
        raise ConnectionClosed(None, None)

    with (
        patch("app.voice.session_monitor._run_sideband_session", _session),
        patch("app.voice.session_monitor._mark_completed", mark_completed),
        patch("app.voice.session_monitor._mark_monitor_failed", mark_failed),
        patch("app.voice.session_monitor._mark_control_disconnected", AsyncMock()),
        patch("app.voice.session_monitor._call_is_terminal", AsyncMock(return_value=False)),
        patch("app.voice.session_monitor._handoff_owns_leg", AsyncMock(return_value=False)),
        patch("app.voice.session_monitor.RETRY_DELAY_SECONDS", 0),
        patch("app.voice.session_monitor.MAX_WS_RETRIES", 3),
    ):
        await _monitor_with_retries(
            tenant_id=tenant_id,
            call_id=call_id,
            openai_call_id="rtc_404_then_up",
        )

    assert attempts["n"] == 3
    hangup.assert_not_awaited()
    mark_failed.assert_not_awaited()
    mark_completed.assert_not_awaited()


@pytest.mark.asyncio
async def test_unanswered_attach_404_does_not_hang_up_after_retries():
    tenant_id = uuid4()
    call_id = uuid4()
    mark_completed = AsyncMock()
    mark_failed = AsyncMock()
    hangup = AsyncMock(return_value={"ok": True})
    attempts = {"n": 0}

    async def _session(**kwargs):
        attempts["n"] += 1
        raise _invalid_status(404)

    with (
        patch("app.voice.session_monitor._run_sideband_session", _session),
        patch("app.voice.session_monitor._mark_completed", mark_completed),
        patch("app.voice.session_monitor._mark_monitor_failed", mark_failed),
        patch("app.voice.session_monitor._mark_control_disconnected", AsyncMock()),
        patch("app.voice.session_monitor._call_is_terminal", AsyncMock(return_value=False)),
        patch("app.voice.session_monitor.RETRY_DELAY_SECONDS", 0),
        patch("app.voice.session_monitor.MAX_WS_RETRIES", 3),
    ):
        await _monitor_with_retries(
            tenant_id=tenant_id,
            call_id=call_id,
            openai_call_id="rtc_never_attached",
        )

    assert attempts["n"] == 3
    hangup.assert_not_awaited()
    mark_failed.assert_not_awaited()
    mark_completed.assert_awaited_once()


@pytest.mark.asyncio
async def test_answered_call_is_not_hung_up_when_sideband_never_attaches():
    tenant_id = uuid4()
    call_id = uuid4()
    mark_completed = AsyncMock()
    mark_failed = AsyncMock()
    hangup = AsyncMock()

    with (
        patch(
            "app.voice.session_monitor._run_sideband_session",
            AsyncMock(side_effect=RuntimeError("ws down")),
        ),
        patch("app.voice.session_monitor._mark_completed", mark_completed),
        patch("app.voice.session_monitor._mark_monitor_failed", mark_failed),
        patch("app.voice.session_monitor._mark_control_disconnected", AsyncMock()),
        patch("app.voice.session_monitor._call_is_terminal", AsyncMock(return_value=False)),
        patch("app.voice.session_monitor.RETRY_DELAY_SECONDS", 0),
        patch("app.voice.session_monitor.MAX_WS_RETRIES", 2),
    ):
        await _monitor_with_retries(
            tenant_id=tenant_id,
            call_id=call_id,
            openai_call_id="rtc_already_answered",
        )

    hangup.assert_not_awaited()
    mark_failed.assert_not_awaited()
    mark_completed.assert_not_awaited()


@pytest.mark.asyncio
async def test_sideband_close_then_404_marks_completed():
    tenant_id = uuid4()
    call_id = uuid4()
    mark_completed = AsyncMock()
    mark_failed = AsyncMock()
    hangup = AsyncMock()
    attempts = {"n": 0}

    async def _session(**kwargs):
        attempts["n"] += 1
        kwargs["attached"]["ok"] = True
        if attempts["n"] == 1:
            raise ConnectionClosed(None, None)
        raise _invalid_status(404)

    with (
        patch("app.voice.session_monitor._run_sideband_session", _session),
        patch("app.voice.session_monitor._mark_completed", mark_completed),
        patch("app.voice.session_monitor._mark_monitor_failed", mark_failed),
        patch("app.voice.session_monitor._mark_control_disconnected", AsyncMock()),
        patch("app.voice.session_monitor._call_is_terminal", AsyncMock(return_value=False)),
        patch("app.voice.session_monitor._handoff_owns_leg", AsyncMock(return_value=False)),
        patch("app.voice.session_monitor.RETRY_DELAY_SECONDS", 0),
        patch("app.voice.session_monitor.MAX_WS_RETRIES", 3),
    ):
        await _monitor_with_retries(
            tenant_id=tenant_id,
            call_id=call_id,
            openai_call_id="rtc_talked",
        )

    assert attempts["n"] == 2
    mark_completed.assert_awaited_once()
    mark_failed.assert_not_awaited()
    hangup.assert_not_awaited()


@pytest.mark.asyncio
async def test_handoff_stops_sideband_reconnect():
    tenant_id = uuid4()
    call_id = uuid4()
    mark_completed = AsyncMock()
    hangup = AsyncMock()
    attempts = {"n": 0}

    async def _session(**kwargs):
        attempts["n"] += 1
        kwargs["attached"]["ok"] = True
        raise ConnectionClosed(None, None)

    with (
        patch("app.voice.session_monitor._run_sideband_session", _session),
        patch("app.voice.session_monitor._mark_completed", mark_completed),
        patch("app.voice.session_monitor._mark_control_disconnected", AsyncMock()),
        patch("app.voice.session_monitor._call_is_terminal", AsyncMock(return_value=False)),
        patch("app.voice.session_monitor._handoff_owns_leg", AsyncMock(return_value=True)),
        patch("app.voice.session_monitor.RETRY_DELAY_SECONDS", 0),
        patch("app.voice.session_monitor.MAX_WS_RETRIES", 5),
    ):
        await _monitor_with_retries(
            tenant_id=tenant_id,
            call_id=call_id,
            openai_call_id="rtc_handoff",
        )

    assert attempts["n"] == 1
    mark_completed.assert_not_awaited()
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
        patch("app.voice.session_monitor._mark_control_disconnected", AsyncMock()),
        patch("app.voice.session_monitor._call_is_terminal", AsyncMock(return_value=False)),
        patch("app.voice.session_monitor._handoff_owns_leg", AsyncMock(return_value=False)),
        patch("app.voice.session_monitor.RETRY_DELAY_SECONDS", 0),
        patch("app.voice.session_monitor.MAX_WS_RETRIES", 2),
    ):
        await _monitor_with_retries(
            tenant_id=tenant_id,
            call_id=call_id,
            openai_call_id="rtc_talked_then_db_error",
        )

    mark_completed.assert_not_awaited()
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
    call = SimpleNamespace(id=uuid4(), status=CallStatus.RINGING, customer_id=None)
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


@pytest.mark.asyncio
async def test_stuck_ringing_invite_is_superseded():
    """Unanswered RINGING holds free the slot without hanging up the SIP leg."""
    from app.voice.inbound_sip import handle_realtime_incoming_sip

    tenant_id = uuid4()
    db = AsyncMock()
    stuck = SimpleNamespace(
        id=uuid4(),
        status=CallStatus.RINGING,
        answered_at=None,
        openai_session_id="rtc_stuck",
        customer_id=None,
    )
    new_call = SimpleNamespace(id=uuid4(), status=CallStatus.RINGING, customer_id=None)

    manager = AsyncMock()
    manager.admit_inbound = AsyncMock(
        return_value=(SimpleNamespace(accepted=True, reason="ok"), new_call)
    )

    calls = AsyncMock()
    calls.add_event = AsyncMock()
    calls.set_status = AsyncMock()
    calls.ensure_conversation = AsyncMock()
    calls.get_by_openai_session_id = AsyncMock(return_value=None)
    calls.get_open_for_caller = AsyncMock(return_value=stuck)

    agent = SimpleNamespace(
        voice="alloy",
        system_instructions="Synas Labs",
        supported_languages=["English"],
    )

    with (
        patch("app.voice.inbound_sip.claim_idempotency", AsyncMock(return_value=True)),
        patch("app.voice.inbound_sip.CallManager", return_value=manager),
        patch("app.voice.inbound_sip.CallService", return_value=calls),
        patch("app.voice.inbound_sip.AgentConfigService") as agent_cls,
        patch(
            "app.voice.inbound_sip.accept_realtime_call",
            AsyncMock(return_value={"ok": True, "status_code": 200}),
        ) as accept,
        patch("app.voice.inbound_sip.start_sideband_monitor", AsyncMock(return_value=True)),
        patch("app.voice.inbound_sip.release_concurrency_slot", AsyncMock()),
        patch(
            "app.voice.inbound_sip.get_settings",
            lambda: Settings(openai_api_key="sk-test", openai_realtime_model="gpt-realtime"),
        ),
        patch(
            "app.voice.inbound_sip._caller_preferred_language",
            AsyncMock(return_value=None),
        ),
    ):
        agent_cls.return_value.get = AsyncMock(return_value=agent)
        result = await handle_realtime_incoming_sip(
            db,
            tenant_id=tenant_id,
            event=_incoming_event("rtc_retry"),
            webhook_id="wh_retry",
        )

    assert result["accepted"] is True
    assert result["openai_call_id"] == "rtc_retry"
    released = calls.apply_lifecycle.await_args_list[0]
    assert released.args[0] == stuck.id
    assert released.args[1].value == "ENDED"
    assert released.kwargs["hangup_requested_by_backend"] is False
    accept.assert_awaited()
    assert accept.await_args.kwargs["openai_call_id"] == "rtc_retry"


@pytest.mark.asyncio
async def test_live_active_invite_is_still_ignored():
    """Do not tear down a live ACTIVE call when Twilio retries the INVITE."""
    from app.voice.inbound_sip import handle_realtime_incoming_sip

    tenant_id = uuid4()
    db = AsyncMock()
    live = SimpleNamespace(
        id=uuid4(),
        status=CallStatus.ACTIVE,
        answered_at=object(),
        openai_session_id="rtc_live",
        customer_id=None,
        lifecycle_state="ACTIVE",
        metadata_json={"control_channel": "connected"},
    )

    calls = AsyncMock()
    calls.get_by_openai_session_id = AsyncMock(return_value=None)
    calls.get_open_for_caller = AsyncMock(return_value=live)
    accept = AsyncMock()
    reject = AsyncMock(return_value={"ok": True})

    with (
        patch("app.voice.inbound_sip.claim_idempotency", AsyncMock(return_value=True)),
        patch("app.voice.inbound_sip.CallService", return_value=calls),
        patch("app.voice.inbound_sip.reject_realtime_call", reject),
        patch("app.voice.inbound_sip.accept_realtime_call", accept),
        patch("app.voice.inbound_sip.CallManager") as cm_cls,
    ):
        result = await handle_realtime_incoming_sip(
            db,
            tenant_id=tenant_id,
            event=_incoming_event("rtc_parallel"),
            webhook_id="wh_parallel",
        )

    assert result["accepted"] is False
    assert result["duplicate"] is True
    assert result["reason"] == "caller_already_in_progress"
    reject.assert_awaited_once()
    accept.assert_not_awaited()
    cm_cls.assert_not_called()


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
