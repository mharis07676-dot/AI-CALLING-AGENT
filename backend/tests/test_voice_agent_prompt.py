import importlib.util
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _load_module(module_name: str, relative_path: str):
    path = BACKEND_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_voice_agent_prompt_blocks_hallucinations():
    prompt_module = _load_module("voice_agent_prompt", "app/ai/voice_agent_prompt.py")
    prompt = prompt_module.VOICE_AGENT_SYSTEM_PROMPT
    assert "NEVER invent" in prompt or "Never invent" in prompt
    assert "Never make up a sample property" in prompt


def test_voice_agent_prompt_supports_multilingual():
    prompt_module = _load_module("voice_agent_prompt", "app/ai/voice_agent_prompt.py")
    prompt = prompt_module.VOICE_AGENT_SYSTEM_PROMPT
    assert "English" in prompt
    assert "Urdu" in prompt
    assert "Roman Urdu" in prompt
    assert "Never choose or change" in prompt
    assert "Pakistani Urdu" in prompt
    assert "NATURAL VOICE BEHAVIOR" in prompt
    assert "CONVERSATION FLOW" in prompt
    assert "Certainly" in prompt
    assert "darkhwast" in prompt or "thori si information" in prompt
    assert "subscription plans" in prompt
    assert "INITIAL GREETING LANGUAGE POLICY" in prompt
    assert "You can speak in Urdu or English, whichever you prefer." in prompt
    assert "Would you prefer Urdu or English?" not in prompt or "Do not ask" in prompt
    assert "pretend to be a human" in prompt.lower() or "Never pretend to be a human" in prompt
    assert "kar sakti hoon" in prompt
    assert "1–3 short sentences" in prompt or "1-3 short sentences" in prompt


def test_realtime_voice_resolves_supported_names_only():
    from app.config import Settings
    from app.voice.realtime import REALTIME_VOICES, resolve_realtime_voice

    assert "marin" in REALTIME_VOICES
    assert "cedar" in REALTIME_VOICES
    assert "alloy" in REALTIME_VOICES
    with patch("app.voice.realtime.get_settings", return_value=Settings(openai_realtime_voice="marin")):
        assert resolve_realtime_voice("alloy") == "marin"
        assert resolve_realtime_voice("not-a-voice") == "marin"
    with patch("app.voice.realtime.get_settings", return_value=Settings(openai_realtime_voice="")):
        assert resolve_realtime_voice("cedar") == "cedar"
        assert resolve_realtime_voice("nope") == "marin"


def test_accept_payload_uses_configured_realtime_voice():
    from app.config import Settings
    from app.voice.realtime import build_accept_payload

    with patch("app.voice.realtime.get_settings", return_value=Settings(
        openai_api_key="sk-test",
        openai_realtime_model="gpt-realtime",
        openai_realtime_voice="cedar",
    )):
        payload = build_accept_payload(tenant_id=uuid4(), call_id=uuid4(), voice="alloy")
    assert payload["audio"]["output"]["voice"] == "cedar"
    assert "NATURAL VOICE BEHAVIOR" in payload["instructions"]


def test_realtime_session_config_includes_voice_agent_prompt():
    prompt_module = _load_module("voice_agent_prompt", "app/ai/voice_agent_prompt.py")
    realtime_source = (BACKEND_ROOT / "app" / "voice" / "realtime.py").read_text(encoding="utf-8")

    assert "from app.ai.voice_agent_prompt import BRAND_PRONUNCIATION_GUIDANCE, VOICE_AGENT_SYSTEM_PROMPT" in realtime_source
    assert "VOICE_AGENT_SYSTEM_PROMPT" in realtime_source
    assert "type\": \"realtime\"" in realtime_source or '"type": "realtime"' in realtime_source
    assert "apply_language_control" in realtime_source
    assert "build_turn_detection" in realtime_source
    assert "create_response" in realtime_source
    assert "match their language" not in realtime_source

    prompt = prompt_module.VOICE_AGENT_SYSTEM_PROMPT
    tenant_id = uuid4()
    call_id = uuid4()
    bilingual = (
        "Hello, Assalam-o-Alaikum — this is Synas Labs. "
        "You can speak in Urdu or English, whichever you prefer."
    )
    instructions = (
        prompt
        + f"\n\nTenant: {tenant_id}\nCall: {call_id}\n"
        f'INITIAL GREETING (speak once at call start, then stop and listen):\n"{bilingual}"\n'
        "CONVERSATION LANGUAGE (APPLICATION CONTROLLED)\n"
        "Conversation language is controlled by the application. "
        "Never choose or change the conversation language yourself.\n"
    )
    assert prompt in instructions
    assert "NEVER invent" in instructions
    assert bilingual in instructions
    assert str(tenant_id) in instructions
    assert str(call_id) in instructions


def test_brand_pronunciation_keeps_written_synas_labs():
    prompt_module = _load_module("voice_agent_prompt", "app/ai/voice_agent_prompt.py")
    guidance = prompt_module.BRAND_PRONUNCIATION_GUIDANCE
    prompt = prompt_module.VOICE_AGENT_SYSTEM_PROMPT
    bilingual = (
        "Hello, Assalam-o-Alaikum — this is Synas Labs. "
        "You can speak in Urdu or English, whichever you prefer."
    )
    caller_facing = (
        '"Assalam-o-Alaikum, Synas Labs se baat ho rahi hai. Main aapki kis tarah madad kar sakti hoon?"',
        '"Hello, this is Synas Labs. How can I help you?"',
        f'"{bilingual}"',
    )

    assert guidance in prompt
    assert 'Company name: "Synas Labs"' in guidance
    assert 'Pronounce "Synas" as "Saaw-ay-nus".' in guidance
    assert 'Full spoken form: "Saaw-ay-nus Labs".' in guidance
    assert 'Never pronounce it as "Sinus", "Sin-us", "Sye-nas", or "Say-nas".' in guidance
    for line in caller_facing:
        assert line in prompt
        assert "Saaw-ay-nus" not in line

    monitor_source = (BACKEND_ROOT / "app" / "voice" / "session_monitor.py").read_text(encoding="utf-8")
    assert "greeting_speak_instructions(initial_greeting)" in monitor_source
