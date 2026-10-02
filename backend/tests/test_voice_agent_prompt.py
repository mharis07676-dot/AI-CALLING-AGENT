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


def test_realtime_session_config_includes_voice_agent_prompt():
    prompt_module = _load_module("voice_agent_prompt", "app/ai/voice_agent_prompt.py")
    realtime_source = (BACKEND_ROOT / "app" / "voice" / "realtime.py").read_text(encoding="utf-8")

    assert "from app.ai.voice_agent_prompt import VOICE_AGENT_SYSTEM_PROMPT" in realtime_source
    assert "VOICE_AGENT_SYSTEM_PROMPT" in realtime_source
    assert "type\": \"realtime\"" in realtime_source or '"type": "realtime"' in realtime_source

    prompt = prompt_module.VOICE_AGENT_SYSTEM_PROMPT
    tenant_id = uuid4()
    call_id = uuid4()
    # Mirror realtime.py instructions composition without importing DB-backed modules.
    instructions = prompt + f"\n\nTenant: {tenant_id}\nCall: {call_id}\nLanguage hint: roman_urdu"
    assert prompt in instructions
    assert "NEVER invent" in instructions
    assert "Roman Urdu" in instructions
    assert str(tenant_id) in instructions
    assert str(call_id) in instructions
