"""Realtime VAD / noise reduction / wait_for_user / prompt consolidation tests."""

from __future__ import annotations

import json
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

# Python 3.14 local envs may lack asyncpg wheels; stub before app.db imports.
if "asyncpg" not in sys.modules:
    sys.modules["asyncpg"] = types.ModuleType("asyncpg")

import pytest

from app.ai.tools import ALLOWED_TOOLS, TOOL_DEFINITIONS, ToolExecutor
from app.ai.voice_agent_prompt import VOICE_AGENT_SYSTEM_PROMPT
from app.config import Settings
from app.schemas import ToolExecutionResult
from app.voice.language_control import CallLanguageState
from app.voice.realtime import (
    build_accept_payload,
    build_noise_reduction,
    build_reasoning_config,
    build_turn_detection,
)
from app.voice.session_monitor import _LatencyProbe, _handle_event, _handle_tool_call, _percentile


def test_noise_reduction_near_field_default():
    with patch(
        "app.voice.realtime.get_settings",
        return_value=Settings(voice_noise_reduction="near_field"),
    ):
        assert build_noise_reduction() == {"type": "near_field"}
        payload = build_accept_payload(tenant_id=uuid4(), call_id=uuid4())
    assert payload["audio"]["input"]["noise_reduction"] == {"type": "near_field"}


def test_noise_reduction_can_be_disabled():
    with patch(
        "app.voice.realtime.get_settings",
        return_value=Settings(voice_noise_reduction="off"),
    ):
        assert build_noise_reduction() is None
        payload = build_accept_payload(tenant_id=uuid4(), call_id=uuid4())
    assert "noise_reduction" not in payload["audio"]["input"]


def test_vad_production_defaults():
    with patch(
        "app.voice.realtime.get_settings",
        return_value=Settings(
            voice_vad_mode="server_vad",
            voice_vad_threshold=0.65,
            voice_vad_prefix_padding_ms=200,
            voice_vad_silence_duration_ms=200,
        ),
    ):
        td = build_turn_detection(create_response=True)
    assert td["type"] == "server_vad"
    assert td["threshold"] == 0.65
    assert td["prefix_padding_ms"] == 200
    assert td["silence_duration_ms"] == 200
    assert td["create_response"] is True
    assert td["interrupt_response"] is True


def test_semantic_vad_is_opt_in():
    with patch(
        "app.voice.realtime.get_settings",
        return_value=Settings(
            voice_vad_mode="semantic_vad",
            voice_semantic_vad_eagerness="high",
        ),
    ):
        td = build_turn_detection(create_response=True)
    assert td == {
        "type": "semantic_vad",
        "eagerness": "high",
        "create_response": True,
        "interrupt_response": True,
    }
    assert "threshold" not in td
    assert "silence_duration_ms" not in td


def test_minimal_reasoning_is_the_default():
    with patch(
        "app.voice.realtime.get_settings",
        return_value=Settings(voice_reasoning_effort="minimal"),
    ):
        assert build_reasoning_config() == {"effort": "minimal"}
        payload = build_accept_payload(tenant_id=uuid4(), call_id=uuid4())
    assert payload["reasoning"] == {"effort": "minimal"}
    assert payload["audio"]["input"]["turn_detection"]["create_response"] is True
    assert "max_output_tokens" not in payload


def test_max_output_tokens_omitted_unless_set():
    with patch(
        "app.voice.realtime.get_settings",
        return_value=Settings(voice_max_output_tokens=250),
    ):
        payload = build_accept_payload(tenant_id=uuid4(), call_id=uuid4())
    assert payload["max_output_tokens"] == 250


def test_low_reasoning_effort_configured():
    with patch(
        "app.voice.realtime.get_settings",
        return_value=Settings(voice_reasoning_effort="low"),
    ):
        assert build_reasoning_config() == {"effort": "low"}
        payload = build_accept_payload(tenant_id=uuid4(), call_id=uuid4())
    assert payload["reasoning"] == {"effort": "low"}


def test_reasoning_effort_can_be_omitted():
    with patch(
        "app.voice.realtime.get_settings",
        return_value=Settings(voice_reasoning_effort="off"),
    ):
        assert build_reasoning_config() is None
        payload = build_accept_payload(tenant_id=uuid4(), call_id=uuid4())
    assert "reasoning" not in payload


def test_wait_for_user_tool_exists_and_is_silent():
    assert "wait_for_user" in ALLOWED_TOOLS
    tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "wait_for_user")
    assert "background" in tool["description"].lower() or "nearby" in tool["description"].lower()
    assert "hang up" not in tool["description"].lower() or "Does not hang" in tool["description"]
    assert tool["parameters"]["required"] == []


@pytest.mark.asyncio
async def test_wait_for_user_does_not_mutate_crm():
    db = AsyncMock()
    db.begin_nested = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=None),
        __aexit__=AsyncMock(return_value=None),
    ))
    executor = ToolExecutor(db, uuid4())
    with patch.object(executor, "_record", AsyncMock(side_effect=lambda *a, **k: a[2])):
        result = await executor._dispatch("wait_for_user", {}, call_id=uuid4())
    assert isinstance(result, ToolExecutionResult)
    assert result.success is True
    assert result.speakable_summary == ""
    assert result.data.get("silent") is True
    assert result.data.get("no_response") is True


@pytest.mark.asyncio
async def test_wait_for_user_handler_does_not_speak_or_redirect():
    ws = AsyncMock()
    tenant_id = uuid4()
    call_id = uuid4()
    tool_result = ToolExecutionResult(
        success=True,
        tool_name="wait_for_user",
        data={"silent": True, "no_response": True},
        speakable_summary="",
    )
    with (
        patch("app.voice.session_monitor.claim_idempotency", AsyncMock(return_value=True)),
        patch("app.voice.session_monitor.ToolExecutor") as executor_cls,
        patch("app.voice.session_monitor.AsyncSessionLocal") as session_cm,
        patch("app.voice.session_monitor.log_call_event"),
    ):
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        session.commit = AsyncMock()
        session_cm.return_value = session
        executor_cls.return_value.execute = AsyncMock(return_value=tool_result)
        await _handle_tool_call(
            ws=ws,
            event={
                "type": "response.function_call_arguments.done",
                "call_id": "fc_wait",
                "name": "wait_for_user",
                "arguments": "{}",
                "event_id": "evt_wait",
            },
            tenant_id=tenant_id,
            call_id=call_id,
            openai_call_id="rtc_wait",
        )

    sent = [__import__("json").loads(c.args[0]) for c in ws.send.await_args_list]
    assert sent[0]["type"] == "conversation.item.create"
    assert sent[1]["type"] == "response.create"
    assert "Remain completely silent" in sent[1]["response"]["instructions"]
    assert len(sent) == 2


def test_primary_caller_instructions_are_consolidated_once():
    prompt = VOICE_AGENT_SYSTEM_PROMPT
    assert prompt.count("# Primary Caller") == 1
    assert prompt.count("CRITICAL SPEAKER RULE") == 1
    assert "PRIMARY CALLER VOICE FOCUS" not in prompt  # old verbose section removed
    assert "one-to-one" in prompt.lower()
    assert "wait_for_user" in prompt
    assert "background" in prompt.lower()
    assert "Do not invent voice fingerprints" in prompt
    assert "Let me think." in prompt  # listed as forbidden preamble
    assert "yes, no, DHA, 500k" in prompt or "500k" in prompt


def test_language_and_handoff_rules_remain():
    prompt = VOICE_AGENT_SYSTEM_PROMPT
    assert "Never choose or change" in prompt
    assert "Pakistani Urdu" in prompt
    assert "transfer_to_human" in prompt
    assert "Synas Labs" in prompt
    assert "Saaw-ay-nus" in prompt
    assert "# Response Speed" in prompt
    assert "respond immediately" in prompt.lower()


def _db_session():
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.commit = AsyncMock()
    return session


async def _transcript_turn(transcript: str, state: CallLanguageState) -> list[dict]:
    ws = AsyncMock()
    calls = AsyncMock()
    calls.add_message = AsyncMock()
    calls.set_conversation_language = AsyncMock()
    with (
        patch("app.voice.session_monitor.claim_idempotency", AsyncMock(return_value=True)),
        patch("app.voice.session_monitor.CallService", return_value=calls),
        patch("app.voice.session_monitor.AsyncSessionLocal", return_value=_db_session()),
    ):
        await _handle_event(
            ws=ws,
            event={
                "type": "conversation.item.input_audio_transcription.completed",
                "event_id": f"evt-{transcript[:12]}",
                "transcript": transcript,
            },
            tenant_id=uuid4(),
            call_id=uuid4(),
            openai_call_id="rtc_latency",
            language_state=state,
            session_instructions="BASE",
        )
    return [json.loads(call.args[0]) for call in ws.send.await_args_list]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transcript",
    [
        "Yes, I'm looking for a house in Dallas.",
        "Mujhe aapki service ke bare mein batain.",
        "yes",
        "no",
        "DHA",
        "mujhe house chahiye in DHA",
    ],
)
async def test_ordinary_turns_do_not_wait_for_transcript_to_respond(transcript: str):
    """Auto-response owns the turn. Transcription must not send response.create."""
    sent = await _transcript_turn(transcript, CallLanguageState())
    assert all(item["type"] != "response.create" for item in sent)


@pytest.mark.asyncio
async def test_consecutive_locked_turns_do_not_response_create():
    state = CallLanguageState(call_language="en", language_locked=True)
    first = await _transcript_turn("Can you explain your service?", state)
    second = await _transcript_turn("What is the monthly price?", state)
    assert first == []
    assert second == []


def test_unknown_language_tells_the_model_to_answer_now():
    from app.voice.language_control import language_control_block

    block = language_control_block(CallLanguageState())
    assert "Respond immediately" in block
    assert "Do not wait for a transcript" in block
    assert "full sales answer" not in block


def test_call_summary_separates_no_tool_and_tool_turns():
    probe = _LatencyProbe(tenant_id=uuid4(), call_id=uuid4())
    probe.on_speech_started()
    probe.on_speech_stopped()
    probe.on_turn_committed()
    probe.on_response_created()
    probe.on_first_audio_delta()
    probe.on_response_done()

    probe.on_speech_started()
    probe.on_speech_stopped()
    probe.on_response_created()
    probe.on_tool_call_started("search_properties")
    probe.on_tool_handler_started()
    probe.on_tool_handler_completed()
    probe.on_tool_output_sent()
    probe.on_response_created()
    probe.on_first_audio_delta()
    probe.on_response_done()

    assert len(probe.turns) == 2
    assert probe.turns[0]["tool_used"] is False
    assert probe.turns[1]["tool_used"] is True
    assert probe.post_tool_response_created_at is not None
    assert probe.post_tool_first_audio_at is not None
    assert _percentile([100, 200, 900], 50) == 200
    assert _percentile([100, 200, 900], 95) == 900


def test_barge_in_stays_enabled_for_both_vad_modes():
    for mode in ("server_vad", "semantic_vad"):
        with patch(
            "app.voice.realtime.get_settings",
            return_value=Settings(voice_vad_mode=mode),
        ):
            td = build_turn_detection(create_response=True)
        assert td["interrupt_response"] is True
        assert td["create_response"] is True
