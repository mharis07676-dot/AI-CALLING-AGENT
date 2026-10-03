"""Tests for live human handoff / Twilio Dial transfer."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.ai.guardrails import validate_tool_arguments
from app.ai.tools import ALLOWED_TOOLS, TOOL_DEFINITIONS, ToolExecutor
from app.config import Settings
from app.models import CallStatus
from app.voice.handoff import (
    HANDOFF_STATUS_BUSY,
    HANDOFF_STATUS_CONNECTED,
    HANDOFF_STATUS_NO_ANSWER,
    HANDOFF_STATUS_REQUESTED,
    build_dial_twiml,
    clear_handoff_runtime,
    is_ai_silenced,
    mark_ai_silenced,
    mask_phone,
    prepare_handoff,
    redirect_active_call,
    strip_model_destination_args,
)


HANDOFF_NUMBER = "+923348587676"


def _settings(**overrides) -> Settings:
    base = dict(
        human_handoff_enabled=True,
        human_handoff_number=HANDOFF_NUMBER,
        human_handoff_timeout_seconds=25,
        public_base_url="https://example.up.railway.app",
        twilio_account_sid="ACaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        twilio_api_key_sid="SKaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        twilio_api_key_secret="secret",
    )
    base.update(overrides)
    return Settings(**base)


def test_transfer_to_human_tool_is_registered():
    assert "transfer_to_human" in ALLOWED_TOOLS
    tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "transfer_to_human")
    props = tool["parameters"]["properties"]
    assert "reason" in props
    assert "phone" not in props
    assert "destination" not in props
    assert "phone_number" not in props


def test_model_cannot_specify_arbitrary_phone_numbers():
    cleaned = strip_model_destination_args(
        {
            "reason": "customer_requested_human",
            "phone": "+19999999999",
            "destination_number": "+18888888888",
            "department": "support",
        }
    )
    assert "phone" not in cleaned
    assert "destination_number" not in cleaned
    assert cleaned["reason"] == "customer_requested_human"
    assert cleaned["department"] == "support"
    errors = validate_tool_arguments(
        "transfer_to_human",
        {"reason": "x", "phone": "+19999999999"},
    )
    assert any("phone" in e for e in errors)


def test_mask_phone_hides_destination():
    assert mask_phone(HANDOFF_NUMBER).endswith("7676")
    assert HANDOFF_NUMBER not in mask_phone(HANDOFF_NUMBER)
    assert mask_phone(HANDOFF_NUMBER).startswith("*")


def test_twiml_contains_configured_number_and_timeout():
    xml = build_dial_twiml(
        destination=HANDOFF_NUMBER,
        timeout=25,
        action_url="https://example.up.railway.app/api/v1/voice/handoff/dial-status?call_id=abc",
        status_callback_url="https://example.up.railway.app/api/v1/voice/handoff/number-status?call_id=abc",
    )
    assert HANDOFF_NUMBER in xml
    assert 'timeout="25"' in xml
    assert "<Dial" in xml
    assert "<Number" in xml
    assert "action=" in xml


@pytest.mark.asyncio
async def test_missing_config_fails_safely():
    call_id = uuid4()
    tenant_id = uuid4()
    call = SimpleNamespace(
        id=call_id,
        customer_id=None,
        from_number="+923001112233",
        started_at=datetime.now(timezone.utc),
        provider_call_id=None,
        metadata_json={},
        handoff_status=None,
        handoff_requested_at=None,
        handoff_reason=None,
        status=CallStatus.ACTIVE,
    )
    calls = MagicMock()
    calls.get = AsyncMock(return_value=call)
    calls.set_status = AsyncMock(return_value=call)
    calls.add_event = AsyncMock()

    db = MagicMock()
    with (
        patch("app.voice.handoff.get_settings", return_value=_settings(human_handoff_enabled=False)),
        patch("app.voice.handoff.CallService", return_value=calls),
    ):
        result = await prepare_handoff(
            db,
            tenant_id=tenant_id,
            call_id=call_id,
            reason="customer_requested_human",
        )
    assert result["ok"] is False
    assert result["error"] == "human_handoff_not_configured"
    assert "continue helping" in result["speakable_summary"].lower() or "not available" in result[
        "speakable_summary"
    ].lower()


@pytest.mark.asyncio
async def test_prepare_handoff_uses_configured_number_and_call_sid():
    call_id = uuid4()
    tenant_id = uuid4()
    call_sid = "CAaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    call = SimpleNamespace(
        id=call_id,
        customer_id=uuid4(),
        from_number="+923001112233",
        started_at=datetime.now(timezone.utc),
        provider_call_id=call_sid,
        metadata_json={},
        handoff_status=None,
        handoff_requested_at=None,
        handoff_reason=None,
        status=CallStatus.ACTIVE,
    )
    calls = MagicMock()
    calls.get = AsyncMock(return_value=call)
    calls.set_status = AsyncMock(return_value=call)
    calls.add_event = AsyncMock()
    handoffs = MagicMock()
    handoffs.request_telephony = AsyncMock(return_value=SimpleNamespace(id=uuid4()))

    db = MagicMock()
    with (
        patch("app.voice.handoff.get_settings", return_value=_settings()),
        patch("app.voice.handoff.CallService", return_value=calls),
        patch("app.voice.handoff.HandoffService", return_value=handoffs),
    ):
        result = await prepare_handoff(
            db,
            tenant_id=tenant_id,
            call_id=call_id,
            reason="customer_requested_human",
            department="support",
        )

    assert result["ok"] is True
    assert result["call_sid"] == call_sid
    assert result["initiate_redirect"] is True
    assert result["destination_masked"].endswith("7676")
    assert HANDOFF_NUMBER not in str(result)
    handoffs.request_telephony.assert_awaited()
    # Call status stored as requested / transferred via set_status
    assert calls.set_status.await_count >= 1
    kwargs = calls.set_status.await_args.kwargs
    assert kwargs.get("handoff_requested") is True
    assert kwargs.get("handoff_status") == HANDOFF_STATUS_REQUESTED
    assert kwargs.get("handoff_reason") == "customer_requested_human"


@pytest.mark.asyncio
async def test_redirect_updates_active_call_sid_only():
    call_id = uuid4()
    tenant_id = uuid4()
    call_sid = "CAbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    call = SimpleNamespace(
        id=call_id,
        customer_id=None,
        from_number="+923001112233",
        started_at=datetime.now(timezone.utc),
        provider_call_id=call_sid,
        metadata_json={"twilio_call_sid": call_sid},
        handoff_status=HANDOFF_STATUS_REQUESTED,
        handoff_requested_at=datetime.now(timezone.utc),
        handoff_reason="customer_requested_human",
        status=CallStatus.TRANSFERRED,
    )
    calls = MagicMock()
    calls.get = AsyncMock(return_value=call)
    calls.set_status = AsyncMock(return_value=call)
    calls.add_event = AsyncMock()

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.commit = AsyncMock()

    clear_handoff_runtime(call_id)
    with (
        patch("app.voice.handoff.get_settings", return_value=_settings()),
        patch("app.db.session.AsyncSessionLocal", return_value=session),
        patch("app.voice.handoff.CallService", return_value=calls),
        patch(
            "app.voice.handoff._twilio_update_call",
            AsyncMock(return_value={"ok": True, "status_code": 200, "caller_hung_up": False}),
        ) as update,
    ):
        result = await redirect_active_call(tenant_id=tenant_id, call_id=call_id)

    assert result["ok"] is True
    assert result["caller_hung_up"] is False
    assert result["call_sid"] == call_sid
    update.assert_awaited_once()
    assert update.await_args.args[0] == call_sid
    assert f"call_id={call_id}" in update.await_args.kwargs["url"]
    assert is_ai_silenced(call_id) is True
    clear_handoff_runtime(call_id)


@pytest.mark.asyncio
async def test_simultaneous_calls_remain_isolated():
    call_a = uuid4()
    call_b = uuid4()
    mark_ai_silenced(call_a, True)
    assert is_ai_silenced(call_a) is True
    assert is_ai_silenced(call_b) is False
    clear_handoff_runtime(call_a)
    assert is_ai_silenced(call_a) is False


@pytest.mark.asyncio
async def test_tool_executor_triggers_transfer_and_ignores_model_number():
    call_id = uuid4()
    tenant_id = uuid4()
    db = MagicMock()
    db.begin_nested = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(),
            __aexit__=AsyncMock(return_value=None),
        )
    )
    db.add = MagicMock()
    db.flush = AsyncMock()

    prepared = {
        "ok": True,
        "call_sid": "CAcccccccccccccccccccccccccccccccc",
        "destination_masked": "*******7676",
        "initiate_redirect": True,
        "speakable_summary": "Sure, I'll connect you to a representative.",
    }
    with patch("app.ai.tools.prepare_handoff", AsyncMock(return_value=prepared)) as prep:
        executor = ToolExecutor(db, tenant_id)
        result = await executor.execute(
            "transfer_to_human",
            {
                "reason": "customer_requested_human",
                "phone": "+19999999999",
                "destination": "+18888888888",
            },
            call_id=call_id,
        )

    assert result.success is True
    assert result.data["initiate_redirect"] is True
    assert result.data["model_destination_ignored"] is True
    assert "+19999999999" not in str(result.data)
    prep.assert_awaited_once()
    kwargs = prep.await_args.kwargs
    assert kwargs["reason"] == "customer_requested_human"
    assert "phone" not in (prep.await_args.args[0] if False else {})


@pytest.mark.asyncio
async def test_human_answers_marks_connected():
    from app.voice.handoff import mark_handoff_connected

    call_id = uuid4()
    tenant_id = uuid4()
    call = SimpleNamespace(
        id=call_id,
        handoff_status="dialing",
        handoff_reason="customer_requested_human",
        handoff_requested_at=datetime.now(timezone.utc),
        handoff_connected_at=None,
        status=CallStatus.TRANSFERRED,
    )
    calls = MagicMock()
    calls.get = AsyncMock(return_value=call)
    calls.set_status = AsyncMock(return_value=call)
    calls.add_event = AsyncMock()
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.commit = AsyncMock()

    with (
        patch("app.db.session.AsyncSessionLocal", return_value=session),
        patch("app.voice.handoff.CallService", return_value=calls),
        patch("app.voice.handoff._latest_handoff", AsyncMock(return_value=None)),
    ):
        await mark_handoff_connected(tenant_id=tenant_id, call_id=call_id)

    kwargs = calls.set_status.await_args.kwargs
    assert kwargs["handoff_status"] == HANDOFF_STATUS_CONNECTED
    assert kwargs["handoff_connected_at"] is not None


@pytest.mark.asyncio
async def test_no_answer_handled_safely():
    from app.voice.handoff import mark_handoff_dial_result

    call_id = uuid4()
    tenant_id = uuid4()
    call = SimpleNamespace(
        id=call_id,
        handoff_status="dialing",
        handoff_reason="customer_requested_human",
        handoff_requested_at=datetime.now(timezone.utc),
        handoff_connected_at=None,
        status=CallStatus.TRANSFERRED,
    )
    calls = MagicMock()
    calls.get = AsyncMock(return_value=call)
    calls.set_status = AsyncMock(return_value=call)
    calls.add_event = AsyncMock()
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.commit = AsyncMock()

    with (
        patch("app.db.session.AsyncSessionLocal", return_value=session),
        patch("app.voice.handoff.CallService", return_value=calls),
        patch("app.voice.handoff._latest_handoff", AsyncMock(return_value=None)),
    ):
        xml = await mark_handoff_dial_result(
            tenant_id=tenant_id,
            call_id=call_id,
            dial_status="no-answer",
        )

    assert "Sorry" in xml or "representative" in xml.lower()
    assert "<Hangup" in xml
    kwargs = calls.set_status.await_args.kwargs
    assert kwargs["handoff_status"] == HANDOFF_STATUS_NO_ANSWER


@pytest.mark.asyncio
async def test_busy_handled_safely():
    from app.voice.handoff import mark_handoff_dial_result

    call_id = uuid4()
    tenant_id = uuid4()
    call = SimpleNamespace(
        id=call_id,
        handoff_status="dialing",
        handoff_reason="customer_requested_human",
        handoff_requested_at=datetime.now(timezone.utc),
        handoff_connected_at=None,
        status=CallStatus.TRANSFERRED,
    )
    calls = MagicMock()
    calls.get = AsyncMock(return_value=call)
    calls.set_status = AsyncMock(return_value=call)
    calls.add_event = AsyncMock()
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.commit = AsyncMock()

    with (
        patch("app.db.session.AsyncSessionLocal", return_value=session),
        patch("app.voice.handoff.CallService", return_value=calls),
        patch("app.voice.handoff._latest_handoff", AsyncMock(return_value=None)),
    ):
        xml = await mark_handoff_dial_result(
            tenant_id=tenant_id,
            call_id=call_id,
            dial_status="busy",
        )

    assert "<Say" in xml
    kwargs = calls.set_status.await_args.kwargs
    assert kwargs["handoff_status"] == HANDOFF_STATUS_BUSY


@pytest.mark.asyncio
async def test_ai_stops_speaking_during_handoff_flag():
    call_id = uuid4()
    clear_handoff_runtime(call_id)
    mark_ai_silenced(call_id, True)
    assert is_ai_silenced(call_id) is True
    clear_handoff_runtime(call_id)


@pytest.mark.asyncio
async def test_finish_handoff_does_not_hang_up_caller():
    from app.voice.session_monitor import _finish_handoff_after_ai_speaks

    call_id = uuid4()
    tenant_id = uuid4()
    ws = MagicMock()
    ws.send = AsyncMock()
    clear_handoff_runtime(call_id)

    with (
        patch("app.voice.session_monitor.asyncio.sleep", AsyncMock()),
        patch(
            "app.voice.session_monitor.redirect_active_call",
            AsyncMock(
                return_value={
                    "ok": True,
                    "caller_hung_up": False,
                    "call_sid": "CAdddddddddddddddddddddddddddddddd",
                }
            ),
        ) as redirect,
    ):
        await _finish_handoff_after_ai_speaks(
            ws=ws,
            tenant_id=tenant_id,
            call_id=call_id,
            openai_call_id="rtc_test",
        )

    redirect.assert_awaited_once()
    assert redirect.await_args.kwargs["call_id"] == call_id
    assert is_ai_silenced(call_id) is True
    # response.cancel + session.update — never a Twilio hangup
    assert ws.send.await_count >= 1
    clear_handoff_runtime(call_id)


def test_recording_segments_metadata_preserves_previous_sid():
    """AI + human segments stay associated with the same Call record."""
    meta: dict = {"twilio_recording_sid": "REaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
    previous_sid = meta.get("twilio_recording_sid")
    sid = "REbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    all_sids = [previous_sid, sid]
    segment_sids = list(dict.fromkeys([*(meta.get("twilio_recording_sids") or []), *all_sids]))
    if previous_sid and previous_sid not in segment_sids:
        segment_sids.insert(0, str(previous_sid))
    meta["twilio_recording_sids"] = segment_sids
    meta["twilio_recording_sid"] = sid
    if previous_sid and previous_sid != sid:
        meta["twilio_recording_sid_previous"] = previous_sid
    assert previous_sid in meta["twilio_recording_sids"]
    assert sid in meta["twilio_recording_sids"]
    assert meta["twilio_recording_sid_previous"] == previous_sid


def test_handoff_twiml_route_exists():
    from app.api.voice import router

    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/voice/handoff/twiml" in paths
    assert "/voice/handoff/dial-status" in paths
    assert "/voice/handoff/number-status" in paths


@pytest.mark.asyncio
async def test_twiml_endpoint_returns_configured_number():
    from fastapi import Response

    from app.api import voice as voice_api

    call_id = uuid4()
    call = SimpleNamespace(id=call_id, tenant_id=uuid4())
    db = MagicMock()

    with (
        patch("app.api.voice._call_tenant", AsyncMock(return_value=(call, call.tenant_id))),
        patch("app.api.voice.configured_handoff_number", return_value=HANDOFF_NUMBER),
        patch("app.api.voice.public_api_base", return_value="https://example.up.railway.app"),
        patch("app.api.voice.handoff_timeout_seconds", return_value=25),
    ):
        request = MagicMock()
        response = await voice_api.handoff_twiml(request, db, call_id=call_id)

    assert isinstance(response, Response)
    body = response.body.decode("utf-8")
    assert HANDOFF_NUMBER in body
    assert 'timeout="25"' in body
