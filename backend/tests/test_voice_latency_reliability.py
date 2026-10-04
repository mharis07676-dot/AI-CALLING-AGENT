"""Realtime VAD / noise reduction / wait_for_user / prompt consolidation tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.ai.tools import ALLOWED_TOOLS, TOOL_DEFINITIONS, ToolExecutor
from app.ai.voice_agent_prompt import VOICE_AGENT_SYSTEM_PROMPT
from app.config import Settings
from app.schemas import ToolExecutionResult
from app.voice.realtime import (
    build_accept_payload,
    build_noise_reduction,
    build_reasoning_config,
    build_turn_detection,
)


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
            voice_vad_threshold=0.65,
            voice_vad_prefix_padding_ms=200,
            voice_vad_silence_duration_ms=250,
        ),
    ):
        td = build_turn_detection(create_response=True)
    assert td["type"] == "server_vad"
    assert td["threshold"] == 0.65
    assert td["prefix_padding_ms"] == 200
    assert td["silence_duration_ms"] == 250
    assert td["create_response"] is True
    assert td["interrupt_response"] is True


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
    from app.voice.session_monitor import _handle_tool_call

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
