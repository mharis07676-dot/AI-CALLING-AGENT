"""Reliability regressions for the inbound SIP drop + blocked redial."""

from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus

if "asyncpg" not in sys.modules:
    sys.modules["asyncpg"] = types.ModuleType("asyncpg")

from app.config import Settings
from app.models import CallStatus
from app.voice.call_lifecycle import (
    CallLifecycle,
    can_transition,
    prior_call_blocks_new_invite,
    redact_secrets,
)
from app.voice.realtime import accept_realtime_call
from app.voice.session_monitor import _monitor_with_retries


def _event(call_id: str, event_id: str = "evt_1") -> dict:
    return {
        "id": event_id,
        "type": "realtime.call.incoming",
        "data": {
            "call_id": call_id,
            "sip_headers": [
                {"name": "From", "value": "sip:+14255550100@sip.example.com"},
                {"name": "To", "value": "sip:+18005550199@sip.example.com"},
            ],
        },
    }


def _calls(*, existing=None, prior=None) -> AsyncMock:
    calls = AsyncMock()
    calls.get_by_openai_session_id = AsyncMock(return_value=existing)
    calls.get_open_for_caller = AsyncMock(return_value=prior)
    calls.reopen_failed_accept = AsyncMock(return_value=False)
    calls.apply_lifecycle = AsyncMock(return_value=True)
    calls.ensure_conversation = AsyncMock()
    calls.add_event = AsyncMock()
    return calls


def _manager(call):
    manager = AsyncMock()
    manager.admit_inbound = AsyncMock(
        return_value=(SimpleNamespace(accepted=True, reason="ok"), call)
    )
    manager.can_accept = AsyncMock(return_value=SimpleNamespace(accepted=True, reason="ok"))
    return manager


@pytest.mark.asyncio
async def test_a_sideband_disconnect_does_not_hang_up_sip():
    """Accepted call, unexpected sideband close: backend must not hang up."""
    hangup = AsyncMock(return_value={"ok": True})
    mark_completed = AsyncMock()

    async def _session(**kwargs):
        kwargs["attached"]["ok"] = True
        raise ConnectionClosed(None, None)

    with (
        patch("app.voice.realtime.hangup_realtime_call", hangup),
        patch("app.voice.session_monitor._run_sideband_session", _session),
        patch("app.voice.session_monitor._mark_completed", mark_completed),
        patch("app.voice.session_monitor._mark_session_gone_terminal", AsyncMock()),
        patch("app.voice.session_monitor._mark_control_disconnected", AsyncMock()),
        patch("app.voice.session_monitor._emit_disconnect_forensics", AsyncMock()),
        patch("app.voice.session_monitor._call_is_terminal", AsyncMock(return_value=False)),
        patch("app.voice.session_monitor._handoff_owns_leg", AsyncMock(return_value=False)),
        patch("app.voice.session_monitor.RETRY_DELAY_SECONDS", 0),
        patch("app.voice.session_monitor.MAX_WS_RETRIES", 2),
    ):
        await _monitor_with_retries(
            tenant_id=uuid4(),
            call_id=uuid4(),
            openai_call_id="rtc_live",
        )

    hangup.assert_not_awaited()
    mark_completed.assert_not_awaited()


@pytest.mark.asyncio
async def test_b_redial_is_accepted_after_previous_call_is_not_live():
    """Call A is no longer live. Call B from the same number is accepted."""
    from app.voice.inbound_sip import handle_realtime_incoming_sip

    tenant_id = uuid4()
    prior = SimpleNamespace(
        id=uuid4(),
        status=CallStatus.ACTIVE,
        answered_at=datetime.now(timezone.utc),
        openai_session_id="rtc_call_a",
        customer_id=None,
        lifecycle_state="ACTIVE",
        metadata_json={
            "control_channel": "disconnected",
            "sideband_disconnected_at": (
                datetime.now(timezone.utc) - timedelta(seconds=30)
            ).isoformat(),
        },
    )
    call_b = SimpleNamespace(id=uuid4(), status=CallStatus.RINGING, customer_id=None)
    calls = _calls(prior=prior)
    accept = AsyncMock(return_value={"ok": True, "status_code": 200})
    hangup = AsyncMock()

    with (
        patch("app.voice.inbound_sip.claim_idempotency", AsyncMock(return_value=True)),
        patch("app.voice.inbound_sip.CallService", return_value=calls),
        patch("app.voice.inbound_sip.CallManager", return_value=_manager(call_b)),
        patch("app.voice.inbound_sip.accept_realtime_call", accept),
        patch("app.voice.inbound_sip.start_sideband_monitor", AsyncMock(return_value=True)),
        patch("app.voice.inbound_sip.release_concurrency_slot", AsyncMock()) as release,
        patch("app.voice.realtime.hangup_realtime_call", hangup),
        patch(
            "app.voice.inbound_sip.get_settings",
            lambda: Settings(openai_api_key="sk-test", openai_realtime_model="gpt-realtime-2.1"),
        ),
        patch("app.voice.inbound_sip.AgentConfigService") as agent_cls,
        patch("app.voice.inbound_sip._caller_preferred_language", AsyncMock(return_value=None)),
    ):
        agent_cls.return_value.get = AsyncMock(
            return_value=SimpleNamespace(voice="marin", system_instructions="hello")
        )
        result = await handle_realtime_incoming_sip(
            AsyncMock(),
            tenant_id=tenant_id,
            event=_event("rtc_call_b", "evt_b"),
            webhook_id="wh_b",
        )

    assert result["accepted"] is True
    assert result["openai_call_id"] == "rtc_call_b"
    accept.assert_awaited_once()
    hangup.assert_not_awaited()
    release.assert_not_awaited()
    ended = calls.apply_lifecycle.await_args_list[0]
    assert ended.args[0] == prior.id
    assert ended.args[1] == CallLifecycle.ENDED
    assert ended.kwargs["hangup_requested_by_backend"] is False


@pytest.mark.asyncio
async def test_c_duplicate_webhook_accepts_once():
    from app.voice.inbound_sip import handle_realtime_incoming_sip

    tenant_id = uuid4()
    call = SimpleNamespace(id=uuid4(), status=CallStatus.RINGING, customer_id=None)
    calls = _calls()
    claimed: set[tuple[str, str]] = set()
    accepts = {"n": 0}
    monitors = {"n": 0}

    async def _claim(_db, *, scope, key, tenant_id=None, call_id=None):  # noqa: ARG001
        token = (scope, key)
        if token in claimed:
            return False
        claimed.add(token)
        return True

    async def _accept(**kwargs):  # noqa: ARG001
        accepts["n"] += 1
        return {"ok": True, "status_code": 200}

    async def _monitor(**kwargs):  # noqa: ARG001
        monitors["n"] += 1
        return True

    with (
        patch("app.voice.inbound_sip.claim_idempotency", _claim),
        patch("app.voice.inbound_sip.CallService", return_value=calls),
        patch("app.voice.inbound_sip.CallManager", return_value=_manager(call)),
        patch("app.voice.inbound_sip.accept_realtime_call", _accept),
        patch("app.voice.inbound_sip.start_sideband_monitor", _monitor),
        patch(
            "app.voice.inbound_sip.get_settings",
            lambda: Settings(openai_api_key="sk-test", openai_realtime_model="gpt-realtime-2.1"),
        ),
        patch("app.voice.inbound_sip.AgentConfigService") as agent_cls,
        patch("app.voice.inbound_sip._caller_preferred_language", AsyncMock(return_value=None)),
    ):
        agent_cls.return_value.get = AsyncMock(
            return_value=SimpleNamespace(voice="marin", system_instructions="")
        )
        event = _event("rtc_dup", "evt_dup")
        first = await handle_realtime_incoming_sip(
            AsyncMock(), tenant_id=tenant_id, event=event, webhook_id="wh_dup"
        )
        second = await handle_realtime_incoming_sip(
            AsyncMock(), tenant_id=tenant_id, event=event, webhook_id="wh_dup"
        )

    assert first["accepted"] is True
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert accepts["n"] == 1
    assert monitors["n"] == 1


@pytest.mark.asyncio
async def test_d_post_accept_init_failure_keeps_call_and_next_invite():
    from app.voice.inbound_sip import handle_realtime_incoming_sip

    tenant_id = uuid4()
    first_call = SimpleNamespace(id=uuid4(), status=CallStatus.RINGING, customer_id=None)
    second_call = SimpleNamespace(id=uuid4(), status=CallStatus.RINGING, customer_id=None)
    calls = _calls()
    calls.ensure_conversation = AsyncMock(side_effect=RuntimeError("db down"))
    release = AsyncMock()
    accept = AsyncMock(return_value={"ok": True, "status_code": 200})
    managers = iter([_manager(first_call), _manager(second_call)])

    with (
        patch("app.voice.inbound_sip.claim_idempotency", AsyncMock(return_value=True)),
        patch("app.voice.inbound_sip.release_idempotency", AsyncMock()),
        patch("app.voice.inbound_sip.CallService", return_value=calls),
        patch("app.voice.inbound_sip.CallManager", side_effect=lambda *a, **k: next(managers)),
        patch("app.voice.inbound_sip.accept_realtime_call", accept),
        patch("app.voice.inbound_sip.release_concurrency_slot", release),
        patch("app.voice.inbound_sip.start_sideband_monitor", AsyncMock(return_value=True)),
        patch(
            "app.voice.inbound_sip.get_settings",
            lambda: Settings(openai_api_key="sk-test", openai_realtime_model="gpt-realtime-2.1"),
        ),
        patch("app.voice.inbound_sip.AgentConfigService") as agent_cls,
        patch("app.voice.inbound_sip._caller_preferred_language", AsyncMock(return_value=None)),
    ):
        agent_cls.return_value.get = AsyncMock(
            return_value=SimpleNamespace(voice="marin", system_instructions="x")
        )
        failed_init = await handle_realtime_incoming_sip(
            AsyncMock(),
            tenant_id=tenant_id,
            event=_event("rtc_d1", "evt_d1"),
            webhook_id="wh_d1",
        )
        calls.ensure_conversation = AsyncMock()
        nxt = await handle_realtime_incoming_sip(
            AsyncMock(),
            tenant_id=tenant_id,
            event=_event("rtc_d2", "evt_d2"),
            webhook_id="wh_d2",
        )

    assert failed_init["accepted"] is True
    assert failed_init["status"] == CallStatus.ACTIVE.value
    assert nxt["accepted"] is True
    assert nxt["openai_call_id"] == "rtc_d2"
    release.assert_not_awaited()
    assert accept.await_count == 2
    failed_marks = [
        c
        for c in calls.apply_lifecycle.await_args_list
        if c.args[1] == CallLifecycle.FAILED
    ]
    assert failed_marks == []


@pytest.mark.asyncio
async def test_e_accept_failure_marks_failed_and_releases_slot():
    from app.voice.inbound_sip import handle_realtime_incoming_sip

    tenant_id = uuid4()
    call = SimpleNamespace(id=uuid4(), status=CallStatus.RINGING, customer_id=None)
    calls = _calls()
    release = AsyncMock()
    release_claim = AsyncMock()
    accept = AsyncMock(
        return_value={
            "ok": False,
            "error": "openai_accept_rejected",
            "message": "OpenAI accept rejected with HTTP 503",
            "status_code": 503,
            "body_preview": "upstream unavailable",
        }
    )

    with (
        patch("app.voice.inbound_sip.claim_idempotency", AsyncMock(return_value=True)),
        patch("app.voice.inbound_sip.release_idempotency", release_claim),
        patch("app.voice.inbound_sip.CallService", return_value=calls),
        patch("app.voice.inbound_sip.CallManager", return_value=_manager(call)),
        patch("app.voice.inbound_sip.accept_realtime_call", accept),
        patch("app.voice.inbound_sip.release_concurrency_slot", release),
        patch("app.voice.inbound_sip.start_sideband_monitor", AsyncMock(return_value=True)),
        patch(
            "app.voice.inbound_sip.get_settings",
            lambda: Settings(openai_api_key="sk-test", openai_realtime_model="gpt-realtime-2.1"),
        ),
    ):
        result = await handle_realtime_incoming_sip(
            AsyncMock(),
            tenant_id=tenant_id,
            event=_event("rtc_fail", "evt_fail"),
            webhook_id="wh_fail",
        )

    assert result["accepted"] is False
    assert result["error"] == "openai_accept_rejected"
    release.assert_awaited()
    assert release_claim.await_count == 2
    failed = [
        c
        for c in calls.apply_lifecycle.await_args_list
        if c.args[1] == CallLifecycle.FAILED
    ]
    assert len(failed) == 1
    assert failed[0].kwargs["hangup_requested_by_backend"] is False
    assert "sk-" not in str(failed[0].kwargs)


def test_terminal_lifecycle_cannot_return_to_active():
    assert can_transition(CallLifecycle.ENDED, CallLifecycle.ACTIVE) is False
    assert can_transition(CallLifecycle.FAILED, CallLifecycle.ACTIVE) is False
    assert can_transition(CallLifecycle.ACTIVE, CallLifecycle.ENDED) is True
    assert can_transition(CallLifecycle.ENDED, CallLifecycle.ENDED) is True
    ended = SimpleNamespace(status=CallStatus.COMPLETED, lifecycle_state="ENDED", metadata_json={})
    assert prior_call_blocks_new_invite(ended) is False


def test_active_row_without_sideband_does_not_block_invite():
    """A DB ACTIVE row alone is not proof the OpenAI session is live."""
    stale = SimpleNamespace(
        status=CallStatus.ACTIVE,
        answered_at=datetime.now(timezone.utc),
        lifecycle_state="ACTIVE",
        openai_session_id="rtc_stale",
        metadata_json={},
    )
    assert prior_call_blocks_new_invite(stale) is False


def test_connected_active_sideband_blocks_invite():
    live = SimpleNamespace(
        status=CallStatus.ACTIVE,
        answered_at=datetime.now(timezone.utc),
        lifecycle_state="ACTIVE",
        openai_session_id="rtc_live",
        metadata_json={"control_channel": "connected"},
    )
    assert prior_call_blocks_new_invite(live) is True


def test_disconnected_within_grace_blocks_invite():
    recent = SimpleNamespace(
        status=CallStatus.ACTIVE,
        answered_at=datetime.now(timezone.utc),
        lifecycle_state="ACTIVE",
        openai_session_id="rtc_grace",
        metadata_json={
            "control_channel": "disconnected",
            "sideband_disconnected_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    assert prior_call_blocks_new_invite(recent) is True


def test_session_confirmed_gone_does_not_block_invite():
    gone = SimpleNamespace(
        status=CallStatus.ACTIVE,
        answered_at=datetime.now(timezone.utc),
        lifecycle_state="ACTIVE",
        openai_session_id="rtc_gone",
        metadata_json={
            "control_channel": "disconnected",
            "sideband_disconnected_at": datetime.now(timezone.utc).isoformat(),
            "openai_session_confirmed_gone": True,
        },
    )
    assert prior_call_blocks_new_invite(gone) is False


def test_redact_secrets_strips_tokens():
    text = redact_secrets("rejected sk-proj-secretvalue Bearer abc.def whsec_zzzz")
    assert "sk-proj" not in text
    assert "whsec_" not in text
    assert "Bearer abc" not in text


@pytest.mark.asyncio
async def test_active_sideband_second_invite_gets_603():
    from app.voice.inbound_sip import handle_realtime_incoming_sip

    tenant_id = uuid4()
    live = SimpleNamespace(
        id=uuid4(),
        status=CallStatus.ACTIVE,
        answered_at=datetime.now(timezone.utc),
        openai_session_id="rtc_live_a",
        customer_id=None,
        lifecycle_state="ACTIVE",
        metadata_json={"control_channel": "connected"},
    )
    calls = _calls(prior=live)
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
            AsyncMock(),
            tenant_id=tenant_id,
            event=_event("rtc_live_b", "evt_parallel"),
            webhook_id="wh_parallel",
        )

    assert result["accepted"] is False
    assert result["reason"] == "caller_already_in_progress"
    reject.assert_awaited_once()
    assert reject.await_args.kwargs["status_code"] == 603
    accept.assert_not_awaited()
    cm_cls.assert_not_called()


@pytest.mark.asyncio
async def test_grace_disconnect_second_invite_no_duplicate_agent():
    from app.voice.inbound_sip import handle_realtime_incoming_sip

    tenant_id = uuid4()
    prior = SimpleNamespace(
        id=uuid4(),
        status=CallStatus.ACTIVE,
        answered_at=datetime.now(timezone.utc),
        openai_session_id="rtc_grace_a",
        customer_id=None,
        lifecycle_state="ACTIVE",
        metadata_json={
            "control_channel": "disconnected",
            "sideband_disconnected_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    calls = _calls(prior=prior)
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
            AsyncMock(),
            tenant_id=tenant_id,
            event=_event("rtc_grace_b", "evt_grace"),
            webhook_id="wh_grace",
        )

    assert result["accepted"] is False
    assert result["duplicate"] is True
    reject.assert_awaited_once()
    accept.assert_not_awaited()
    cm_cls.assert_not_called()


@pytest.mark.asyncio
async def test_session_gone_404_second_invite_accepted_and_slot_released():
    from app.voice.inbound_sip import handle_realtime_incoming_sip
    from app.voice.realtime_forensics import clear_forensics_for_tests, get_or_create_forensics

    clear_forensics_for_tests()
    tenant_id = uuid4()
    prior_id = uuid4()
    prior = SimpleNamespace(
        id=prior_id,
        status=CallStatus.ACTIVE,
        answered_at=datetime.now(timezone.utc),
        openai_session_id="rtc_gone_a",
        customer_id=None,
        lifecycle_state="ACTIVE",
        metadata_json={
            "control_channel": "disconnected",
            "sideband_disconnected_at": datetime.now(timezone.utc).isoformat(),
            "openai_session_confirmed_gone": True,
        },
    )
    call_b = SimpleNamespace(id=uuid4(), status=CallStatus.RINGING, customer_id=None)
    calls = _calls(prior=prior)
    accept = AsyncMock(return_value={"ok": True, "status_code": 200})
    reject = AsyncMock()
    release = AsyncMock()
    state = get_or_create_forensics(
        tenant_id=tenant_id,
        call_id=prior_id,
        openai_call_id="rtc_gone_a",
    )
    state.mark_session_gone()

    with (
        patch("app.voice.inbound_sip.claim_idempotency", AsyncMock(return_value=True)),
        patch("app.voice.inbound_sip.CallService", return_value=calls),
        patch("app.voice.inbound_sip.CallManager", return_value=_manager(call_b)),
        patch("app.voice.inbound_sip.accept_realtime_call", accept),
        patch("app.voice.inbound_sip.reject_realtime_call", reject),
        patch("app.voice.inbound_sip.release_concurrency_slot", release),
        patch("app.voice.inbound_sip.start_sideband_monitor", AsyncMock(return_value=True)),
        patch(
            "app.voice.inbound_sip.get_settings",
            lambda: Settings(openai_api_key="sk-test", openai_realtime_model="gpt-realtime-2.1"),
        ),
        patch("app.voice.inbound_sip.AgentConfigService") as agent_cls,
        patch("app.voice.inbound_sip._caller_preferred_language", AsyncMock(return_value=None)),
    ):
        agent_cls.return_value.get = AsyncMock(
            return_value=SimpleNamespace(voice="marin", system_instructions="hello")
        )
        result = await handle_realtime_incoming_sip(
            AsyncMock(),
            tenant_id=tenant_id,
            event=_event("rtc_gone_b", "evt_gone"),
            webhook_id="wh_gone",
        )

    assert result["accepted"] is True
    assert result["openai_call_id"] == "rtc_gone_b"
    reject.assert_not_awaited()
    accept.assert_awaited_once()
    ended = calls.apply_lifecycle.await_args_list[0]
    assert ended.args[0] == prior_id
    assert ended.args[1] == CallLifecycle.ENDED
    assert ended.kwargs["termination_reason"] == "openai_session_confirmed_gone"
    clear_forensics_for_tests()


@pytest.mark.asyncio
async def test_active_row_session_gone_in_registry_accepts_new_invite():
    """Old row still ACTIVE, but forensics proves OpenAI session is gone."""
    from app.voice.inbound_sip import handle_realtime_incoming_sip
    from app.voice.realtime_forensics import clear_forensics_for_tests, get_or_create_forensics

    clear_forensics_for_tests()
    tenant_id = uuid4()
    prior_id = uuid4()
    prior = SimpleNamespace(
        id=prior_id,
        status=CallStatus.ACTIVE,
        answered_at=datetime.now(timezone.utc),
        openai_session_id="rtc_registry_gone",
        customer_id=None,
        lifecycle_state="ACTIVE",
        metadata_json={"control_channel": "connected"},
    )
    # Registry wins over stale "connected" metadata.
    get_or_create_forensics(
        tenant_id=tenant_id,
        call_id=prior_id,
        openai_call_id="rtc_registry_gone",
    ).mark_session_gone()
    call_b = SimpleNamespace(id=uuid4(), status=CallStatus.RINGING, customer_id=None)
    calls = _calls(prior=prior)
    accept = AsyncMock(return_value={"ok": True, "status_code": 200})
    reject = AsyncMock()

    with (
        patch("app.voice.inbound_sip.claim_idempotency", AsyncMock(return_value=True)),
        patch("app.voice.inbound_sip.CallService", return_value=calls),
        patch("app.voice.inbound_sip.CallManager", return_value=_manager(call_b)),
        patch("app.voice.inbound_sip.accept_realtime_call", accept),
        patch("app.voice.inbound_sip.reject_realtime_call", reject),
        patch("app.voice.inbound_sip.release_concurrency_slot", AsyncMock()),
        patch("app.voice.inbound_sip.start_sideband_monitor", AsyncMock(return_value=True)),
        patch(
            "app.voice.inbound_sip.get_settings",
            lambda: Settings(openai_api_key="sk-test", openai_realtime_model="gpt-realtime-2.1"),
        ),
        patch("app.voice.inbound_sip.AgentConfigService") as agent_cls,
        patch("app.voice.inbound_sip._caller_preferred_language", AsyncMock(return_value=None)),
    ):
        agent_cls.return_value.get = AsyncMock(
            return_value=SimpleNamespace(voice="marin", system_instructions="")
        )
        result = await handle_realtime_incoming_sip(
            AsyncMock(),
            tenant_id=tenant_id,
            event=_event("rtc_registry_new", "evt_reg"),
            webhook_id="wh_reg",
        )

    assert result["accepted"] is True
    reject.assert_not_awaited()
    clear_forensics_for_tests()


@pytest.mark.asyncio
async def test_repeated_session_gone_cleanup_is_idempotent():
    from app.voice.realtime_forensics import clear_forensics_for_tests
    from app.voice.session_monitor import _mark_session_gone_terminal

    clear_forensics_for_tests()
    tenant_id = uuid4()
    call_id = uuid4()
    call = SimpleNamespace(
        id=call_id,
        status=CallStatus.COMPLETED,
        metadata_json={"openai_session_confirmed_gone": True},
        handoff_requested=False,
        handoff_status=None,
    )
    calls = AsyncMock()
    calls.get = AsyncMock(return_value=call)
    calls.set_status = AsyncMock()
    calls.apply_lifecycle = AsyncMock(return_value=True)

    with (
        patch("app.voice.session_monitor.CallService", return_value=calls),
        patch("app.voice.session_monitor.AsyncSessionLocal") as session_cm,
        patch("app.voice.session_monitor.schedule_finalize_recording"),
    ):
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        session.commit = AsyncMock()
        session_cm.return_value = session
        await _mark_session_gone_terminal(
            tenant_id=tenant_id,
            call_id=call_id,
            openai_call_id="rtc_idem",
            reason="sideband_http_404",
        )
        await _mark_session_gone_terminal(
            tenant_id=tenant_id,
            call_id=call_id,
            openai_call_id="rtc_idem",
            reason="sideband_http_404",
        )

    calls.apply_lifecycle.assert_not_awaited()
    clear_forensics_for_tests()


@pytest.mark.asyncio
async def test_sideband_404_emits_forensic_snapshot():
    from app.voice.realtime_forensics import clear_forensics_for_tests, get_or_create_forensics
    from app.voice.session_monitor import _monitor_with_retries

    clear_forensics_for_tests()
    tenant_id = uuid4()
    call_id = uuid4()
    openai_call_id = "rtc_forensic"
    state = get_or_create_forensics(
        tenant_id=tenant_id,
        call_id=call_id,
        openai_call_id=openai_call_id,
    )
    state.observe({"type": "session.created", "event_id": "e1"})
    state.observe(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "event_id": "e2",
            "transcript": "hello there please help",
        }
    )
    state.observe({"type": "response.output_audio.delta", "event_id": "e3"})
    state.observe({"type": "response.done", "event_id": "e4"})

    response = MagicMock()
    response.status_code = 404
    response.body = b'{"error":{"code":"call_id_not_found"}}'
    exc = InvalidStatus(response)

    async def _session(**kwargs):
        kwargs["attached"]["ok"] = True
        raise exc

    with (
        patch("app.voice.session_monitor._run_sideband_session", _session),
        patch("app.voice.session_monitor._mark_session_gone_terminal", AsyncMock()) as mark_gone,
        patch("app.voice.session_monitor._mark_control_disconnected", AsyncMock()),
        patch("app.voice.session_monitor._call_is_terminal", AsyncMock(return_value=False)),
        patch("app.voice.session_monitor._handoff_owns_leg", AsyncMock(return_value=False)),
        patch("app.voice.session_monitor._emit_disconnect_forensics", AsyncMock()) as emit,
        patch("app.voice.session_monitor.RETRY_DELAY_SECONDS", 0),
        patch("app.voice.session_monitor.MAX_WS_RETRIES", 1),
        patch(
            "app.voice.session_monitor.get_settings",
            lambda: Settings(openai_api_key="sk-test", openai_sip_project_id="proj_x"),
        ),
    ):
        await _monitor_with_retries(
            tenant_id=tenant_id,
            call_id=call_id,
            openai_call_id=openai_call_id,
        )

    mark_gone.assert_awaited_once()
    emit.assert_awaited()
    # Transcript text must never appear in forensic event ring metadata dumps.
    dumped = state.recent_event_types()
    assert any("session.created" in item for item in dumped)
    assert any("response.done" in item for item in dumped)
    assert "hello there" not in str(dumped)
    assert "hello there" not in str(state.events)
    clear_forensics_for_tests()


@pytest.mark.asyncio
async def test_accept_retries_transient_failure_then_stops():
    response_503 = MagicMock(status_code=503, text="unavailable sk-should-not-leak")
    response_200 = MagicMock(status_code=200, text="")
    client = AsyncMock()
    client.post = AsyncMock(side_effect=[response_503, response_200])
    client.aclose = AsyncMock()

    with (
        patch(
            "app.voice.realtime.get_settings",
            return_value=Settings(openai_api_key="sk-test", openai_realtime_model="gpt-realtime-2.1"),
        ),
        patch("app.voice.realtime._accept_backoff", AsyncMock()),
    ):
        result = await accept_realtime_call(
            openai_call_id="rtc_retry_http",
            session_config={"type": "realtime"},
            client=client,
            attempts=3,
        )

    assert result["ok"] is True
    assert client.post.await_count == 2
