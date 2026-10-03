"""Application-owned call language. The Realtime model is not the detector."""

import json
import sys
import types
from unittest.mock import AsyncMock, patch
from uuid import uuid4

# Python 3.14 local envs may lack asyncpg wheels; stub before app.db imports.
if "asyncpg" not in sys.modules:
    sys.modules["asyncpg"] = types.ModuleType("asyncpg")

import pytest

from app.voice.language_control import (
    CallLanguageState,
    apply_language_control,
    language_control_block,
    observe_caller_transcript,
    state_from_preference,
)
from app.voice.realtime import BILINGUAL_GREETING, build_language_session_update, select_initial_greeting
from app.voice.session_monitor import _handle_event

ENGLISH_1 = "Can you explain your service?"
ENGLISH_2 = "What is the monthly price?"
ROMAN_URDU_1 = "Mujhe aapki service ke bare mein batain."
ROMAN_URDU_2 = "Aap kaise hain aur mujhe ghar chahiye."
URDU_SCRIPT = "مجھے آپ کی سروس کے بارے میں بتائیں"


def _locked(language: str) -> CallLanguageState:
    return CallLanguageState(call_language=language, language_locked=True)


def test_english_caller_stays_english():
    state = CallLanguageState()
    first = observe_caller_transcript(state, ENGLISH_1)
    assert "LANGUAGE_LOCKED language=en" in first
    observe_caller_transcript(state, ENGLISH_2)
    observe_caller_transcript(state, "I want to rent a house.")
    assert state.call_language == "en"
    assert state.language_locked is True
    assert state.language_switch_count == 0
    block = language_control_block(state)
    assert "Respond only in natural English." in block
    assert "Do not use Roman Urdu." in block
    assert "Respond in natural Pakistani Urdu." not in block


def test_urdu_script_stays_urdu():
    state = CallLanguageState()
    logs = observe_caller_transcript(state, URDU_SCRIPT)
    assert "LANGUAGE_DETECTED candidate=ur" in logs
    assert "LANGUAGE_LOCKED language=ur" in logs
    observe_caller_transcript(state, "مجھے گھر چاہیے")
    assert state.call_language == "ur"
    assert "Keep the main sentence structure Urdu." in language_control_block(state)


def test_roman_urdu_is_urdu():
    state = CallLanguageState()
    for utterance in (
        "mujhe service ke bare mein batain",
        "aap kaise hain",
        "mujhe appointment chahiye",
    ):
        fresh = CallLanguageState()
        logs = observe_caller_transcript(fresh, utterance)
        assert fresh.call_language == "ur", utterance
        assert "LANGUAGE_LOCKED language=ur" in logs


def test_urdu_caller_okay_stays_urdu():
    state = _locked("ur")
    assert observe_caller_transcript(state, "Okay.") == []
    assert state.call_language == "ur"
    assert state.language_switch_count == 0


def test_urdu_caller_yes_stays_urdu():
    state = _locked("ur")
    assert observe_caller_transcript(state, "yes") == []
    assert state.call_language == "ur"


def test_english_caller_acha_stays_english():
    state = _locked("en")
    assert observe_caller_transcript(state, "Acha.") == []
    assert state.call_language == "en"
    assert state.language_switch_candidate is None


def test_english_caller_theek_hai_does_not_switch():
    state = _locked("en")
    assert observe_caller_transcript(state, "theek hai") == []
    assert state.call_language == "en"
    assert state.language_switch_count == 0


def test_urdu_switches_after_two_clear_english_utterances():
    state = _locked("ur")
    first = observe_caller_transcript(state, ENGLISH_1)
    assert state.call_language == "ur"
    assert "LANGUAGE_SWITCH_CANDIDATE en count=1" in first
    second = observe_caller_transcript(state, ENGLISH_2)
    assert "LANGUAGE_SWITCH_CONFIRMED language=en" in second
    assert state.call_language == "en"
    assert state.language_locked is True
    assert state.language_switch_count == 0
    assert "Respond only in natural English." in language_control_block(state)


def test_english_switches_after_two_clear_urdu_utterances():
    state = _locked("en")
    first = observe_caller_transcript(state, ROMAN_URDU_1)
    assert state.call_language == "en"
    assert "LANGUAGE_SWITCH_CANDIDATE ur count=1" in first
    observe_caller_transcript(state, "acha")
    assert state.language_switch_count == 1
    second = observe_caller_transcript(state, ROMAN_URDU_2)
    assert "LANGUAGE_SWITCH_CONFIRMED language=ur" in second
    assert state.call_language == "ur"


def test_unknown_caller_gets_one_bilingual_greeting_then_locks():
    state = state_from_preference("roman_urdu")
    assert state.call_language is None
    assert state.language_locked is False
    label, greeting = select_initial_greeting("roman_urdu")
    assert label == "unknown"
    assert greeting == BILINGUAL_GREETING
    assert select_initial_greeting(None)[1] == greeting
    unknown = language_control_block(state)
    assert "call_language: unknown" in unknown
    assert "must not be repeated" in unknown
    logs = observe_caller_transcript(state, "mujhe service ke bare mein batain")
    assert "LANGUAGE_LOCKED language=ur" in logs
    locked = language_control_block(state)
    assert 'call_language: "ur"' in locked
    assert "call_language: unknown" not in locked


def test_reliable_preference_starts_locked_and_can_switch():
    english = state_from_preference("english")
    assert english.call_language == "en"
    assert english.language_locked is True
    assert 'call_language: "en"' in language_control_block(english)
    observe_caller_transcript(english, "acha")
    assert english.call_language == "en"
    observe_caller_transcript(english, ROMAN_URDU_1)
    assert english.call_language == "en"
    assert english.language_switch_count == 1
    observe_caller_transcript(english, ROMAN_URDU_2)
    assert english.call_language == "ur"

    urdu = state_from_preference("urdu")
    assert urdu.call_language == "ur"
    assert urdu.language_locked is True
    assert state_from_preference("roman_urdu").call_language is None
    assert state_from_preference(None).call_language is None


def test_language_block_overrides_older_detect_wording():
    rendered = apply_language_control(
        "Detect the language of the caller's latest utterance and switch immediately.",
        _locked("en"),
    )
    assert rendered.endswith(language_control_block(_locked("en")))
    assert "Never choose or change the conversation language yourself." in rendered
    again = apply_language_control(rendered, _locked("ur"))
    assert again.count("CONVERSATION LANGUAGE (APPLICATION CONTROLLED)") == 1
    assert 'call_language: "ur"' in again
    assert 'call_language: "en"' not in again


@pytest.mark.asyncio
async def test_transcript_updates_realtime_instructions_before_reply():
    tenant_id = uuid4()
    call_id = uuid4()
    ws = AsyncMock()
    calls = AsyncMock()
    calls.add_message = AsyncMock()
    calls.set_conversation_language = AsyncMock()
    state = CallLanguageState()

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
                "event_id": "evt_lang_1",
                "transcript": ENGLISH_1,
            },
            tenant_id=tenant_id,
            call_id=call_id,
            openai_call_id="rtc_lang",
            language_state=state,
            session_instructions="BASE PROMPT",
        )

    assert state.call_language == "en"
    calls.set_conversation_language.assert_awaited_with(call_id, "en")
    sent = [json.loads(call.args[0]) for call in ws.send.await_args_list]
    assert sent[0] == build_language_session_update(sent[0]["session"]["instructions"])
    assert "BASE PROMPT" in sent[0]["session"]["instructions"]
    assert 'call_language: "en"' in sent[0]["session"]["instructions"]
    assert sent[1]["type"] == "response.create"
    assert 'call_language: "en"' in sent[1]["response"]["instructions"]
    assert "Never choose or change the conversation language yourself." in sent[1]["response"]["instructions"]

    ws.send.reset_mock()
    calls.set_conversation_language.reset_mock()
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
                "event_id": "evt_lang_2",
                "transcript": "okay",
            },
            tenant_id=tenant_id,
            call_id=call_id,
            openai_call_id="rtc_lang",
            language_state=state,
            session_instructions="BASE PROMPT",
        )

    assert state.call_language == "en"
    calls.set_conversation_language.assert_not_awaited()
    followup = [json.loads(call.args[0]) for call in ws.send.await_args_list]
    assert [item["type"] for item in followup] == ["response.create"]
    assert 'call_language: "en"' in followup[0]["response"]["instructions"]
