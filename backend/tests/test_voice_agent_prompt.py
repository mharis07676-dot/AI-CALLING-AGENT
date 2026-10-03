import importlib.util
from pathlib import Path
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
    assert "NEVER invent" in prompt
    assert "Never make up a sample property" in prompt


def test_voice_agent_prompt_supports_multilingual():
    prompt_module = _load_module("voice_agent_prompt", "app/ai/voice_agent_prompt.py")
    prompt = prompt_module.VOICE_AGENT_SYSTEM_PROMPT
    assert "English" in prompt
    assert "Urdu" in prompt
    assert "Roman Urdu" in prompt
    assert "Never choose or change the conversation language yourself." in prompt
    assert "Never translate Urdu" in prompt
    assert "Pakistani Urdu" in prompt
    assert "Never force English" in prompt
    assert "subscription plans" in prompt
    assert "CONVERSATION TIMING" in prompt
    assert "URDU STYLE" in prompt
    assert "Thank you for providing that information." in prompt
    assert "Main aapko pricing explain karta hoon." in prompt
    assert "Sure, I can explain our subscription plans to you." in prompt
    assert "Okay, now explain that in English." not in prompt
    assert "switch immediately" not in prompt
    assert "INITIAL GREETING LANGUAGE POLICY" in prompt
    assert "You can speak in Urdu or English, whichever you prefer." in prompt
    assert "Would you prefer Urdu or English?" not in prompt or "Do not ask" in prompt


def test_realtime_session_config_includes_voice_agent_prompt():
    prompt_module = _load_module("voice_agent_prompt", "app/ai/voice_agent_prompt.py")
    realtime_source = (BACKEND_ROOT / "app" / "voice" / "realtime.py").read_text(encoding="utf-8")

    assert "from app.ai.voice_agent_prompt import BRAND_PRONUNCIATION_GUIDANCE, VOICE_AGENT_SYSTEM_PROMPT" in realtime_source
    assert "VOICE_AGENT_SYSTEM_PROMPT" in realtime_source
    assert "type\": \"realtime\"" in realtime_source or '"type": "realtime"' in realtime_source
    assert "apply_language_control" in realtime_source
    assert "create_response\": False" in realtime_source or '"create_response": False' in realtime_source
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
        '"Assalam-o-Alaikum, Synas Labs se baat ho rahi hai. Main aapki kis tarah madad kar sakta hoon?"',
        '"Hello, this is Synas Labs. How can I help you?"',
        f'"{bilingual}"',
        '"This is Synas Labs."',
        '"Welcome to Synas Labs."',
        '"I\'m calling from Synas Labs."',
        '"Thank you for contacting Synas Labs."',
        '"At Synas Labs, we..."',
        '"Synas Labs provides..."',
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
