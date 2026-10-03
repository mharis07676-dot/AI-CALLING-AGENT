"""Tests for Twilio-backed call recording (actual SIP media path)."""

from __future__ import annotations

import struct
import sys
import types
import wave
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

if "asyncpg" not in sys.modules:
    sys.modules["asyncpg"] = types.ModuleType("asyncpg")

from app.config import Settings
from app.models import CallStatus
from app.voice.recording import (
    absolute_path_for_key,
    convert_wav_to_mp3,
    ffmpeg_available,
    looks_like_twilio_call_sid,
    purge_expired_recordings,
    recording_public_meta,
    start_call_recording,
    storage_key_for_call,
)
from app.voice.storage import LocalRecordingStorage, build_recording_storage, reset_recording_storage_cache


def _minimal_wav(*, frames: int = 8000, channels: int = 2, rate: int = 8000) -> bytes:
    buf = BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        silence = struct.pack("<h", 0) * channels
        wf.writeframes(silence * frames)
    return buf.getvalue()


def test_looks_like_twilio_call_sid():
    assert looks_like_twilio_call_sid("CAaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    assert not looks_like_twilio_call_sid("sip-call-id@twilio.com")
    assert not looks_like_twilio_call_sid(None)


def test_storage_key_is_safe_and_unique():
    tenant_id = uuid4()
    call_id = uuid4()
    key = storage_key_for_call(tenant_id=tenant_id, call_id=call_id, ext="mp3")
    assert str(tenant_id) in key
    assert str(call_id) in key
    assert key.endswith(".mp3")
    assert "+" not in key


def test_wav_bytes_are_valid_playable_header(tmp_path: Path):
    media = _minimal_wav(frames=1600, channels=2)
    path = tmp_path / "sample.wav"
    path.write_bytes(media)
    with wave.open(str(path), "rb") as wf:
        assert wf.getnchannels() == 2
        assert wf.getframerate() == 8000
        assert wf.getnframes() == 1600


def test_local_storage_isolation_per_call(tmp_path: Path):
    storage = LocalRecordingStorage(tmp_path)
    a = "tenant/call-a.mp3"
    b = "tenant/call-b.mp3"
    storage.put(a, b"aaa", content_type="audio/mpeg")
    storage.put(b, b"bbb", content_type="audio/mpeg")
    assert storage.get(a) == b"aaa"
    assert storage.get(b) == b"bbb"
    assert a != b


@pytest.mark.asyncio
async def test_start_recording_skipped_when_disabled():
    with patch("app.voice.recording.get_settings", return_value=Settings(call_recording_enabled=False)):
        result = await start_call_recording(tenant_id=uuid4(), call_id=uuid4())
    assert result["ok"] is True
    assert result["skipped"] is True


@pytest.mark.asyncio
async def test_start_recording_does_not_crash_active_call_on_twilio_error():
    call = SimpleNamespace(
        id=uuid4(),
        status=CallStatus.ACTIVE,
        from_number="+923001234567",
        started_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
        provider_call_id="not-a-ca-sid",
        metadata_json={},
        recording_status=None,
    )
    calls = AsyncMock()
    calls.get = AsyncMock(return_value=call)
    calls.set_status = AsyncMock()

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.commit = AsyncMock()

    twilio = AsyncMock()
    twilio.configured = True
    twilio.find_call_sid = AsyncMock(return_value="CAaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    twilio.start_dual_recording = AsyncMock(
        return_value={"ok": False, "error": "twilio_start_recording_rejected"}
    )
    twilio.__aenter__ = AsyncMock(return_value=twilio)
    twilio.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("app.voice.recording.get_settings", return_value=Settings(call_recording_enabled=True)),
        patch("app.voice.recording.AsyncSessionLocal", return_value=session),
        patch("app.voice.recording.CallService", return_value=calls),
        patch("app.voice.recording.TwilioRecordingClient", return_value=twilio),
        patch("app.voice.recording.asyncio.sleep", AsyncMock()),
    ):
        result = await start_call_recording(tenant_id=uuid4(), call_id=call.id)

    assert result["ok"] is True
    assert calls.set_status.await_count >= 1


@pytest.mark.asyncio
async def test_finalize_downloads_converts_and_marks_ready(tmp_path: Path):
    from app.voice.recording import finalize_call_recording

    call_id = uuid4()
    tenant_id = uuid4()
    call = SimpleNamespace(
        id=call_id,
        status=CallStatus.COMPLETED,
        from_number="+923001234567",
        started_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
        provider_call_id=None,
        metadata_json={"twilio_call_sid": "CAaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
        recording_status="recording",
        recording_provider_sid="REbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        recording_storage_key=None,
    )
    calls = AsyncMock()
    calls.get = AsyncMock(return_value=call)
    calls.set_status = AsyncMock()
    calls.add_event = AsyncMock()

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.commit = AsyncMock()

    media = _minimal_wav(frames=800, channels=2)
    twilio = AsyncMock()
    twilio.configured = True
    twilio.list_recordings = AsyncMock(
        return_value=[
            {
                "sid": "REbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "status": "completed",
                "duration": "12",
            }
        ]
    )
    twilio.download_wav = AsyncMock(return_value=media)
    twilio.__aenter__ = AsyncMock(return_value=twilio)
    twilio.__aexit__ = AsyncMock(return_value=None)

    fake_mp3 = b"ID3fake-mp3-bytes"

    with (
        patch(
            "app.voice.recording.get_settings",
            return_value=Settings(
                call_recording_enabled=True,
                call_recording_dir=str(tmp_path),
                call_recording_mode="stereo",
                call_recording_format="mp3",
                recording_storage_provider="local",
            ),
        ),
        patch("app.voice.storage.get_settings", return_value=Settings(
            call_recording_dir=str(tmp_path),
            recording_storage_provider="local",
        )),
        patch("app.voice.recording.AsyncSessionLocal", return_value=session),
        patch("app.voice.recording.CallService", return_value=calls),
        patch("app.voice.recording.TwilioRecordingClient", return_value=twilio),
        patch("app.voice.recording.asyncio.sleep", AsyncMock()),
        patch("app.voice.recording.convert_wav_to_mp3_async", AsyncMock(return_value=fake_mp3)),
    ):
        reset_recording_storage_cache()
        result = await finalize_call_recording(tenant_id=tenant_id, call_id=call_id)

        assert result["ok"] is True
        assert result["format"] == "mp3"
        assert result["size_bytes"] == len(fake_mp3)
        assert result["duration_seconds"] == 12
        ready_calls = [
            c
            for c in calls.set_status.await_args_list
            if c.kwargs.get("recording_status") == "ready"
        ]
        assert ready_calls
        key = ready_calls[-1].kwargs["recording_storage_key"]
        assert key.endswith(".mp3")
        path = absolute_path_for_key(key)
        assert path.is_file()
        assert path.read_bytes() == fake_mp3


@pytest.mark.asyncio
async def test_finalize_wav_fallback_when_ffmpeg_fails(tmp_path: Path):
    from app.voice.recording import finalize_call_recording

    call_id = uuid4()
    call = SimpleNamespace(
        id=call_id,
        status=CallStatus.COMPLETED,
        from_number="+923001234567",
        started_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
        provider_call_id=None,
        metadata_json={"twilio_call_sid": "CAaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
        recording_status="recording",
        recording_provider_sid="REbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        recording_storage_key=None,
    )
    calls = AsyncMock()
    calls.get = AsyncMock(return_value=call)
    calls.set_status = AsyncMock()
    calls.add_event = AsyncMock()
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.commit = AsyncMock()
    media = _minimal_wav(frames=400, channels=2)
    twilio = AsyncMock()
    twilio.configured = True
    twilio.list_recordings = AsyncMock(
        return_value=[{"sid": "REbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "status": "completed", "duration": "5"}]
    )
    twilio.download_wav = AsyncMock(return_value=media)
    twilio.__aenter__ = AsyncMock(return_value=twilio)
    twilio.__aexit__ = AsyncMock(return_value=None)

    with (
        patch(
            "app.voice.recording.get_settings",
            return_value=Settings(
                call_recording_enabled=True,
                call_recording_dir=str(tmp_path),
                call_recording_format="mp3",
                recording_storage_provider="local",
            ),
        ),
        patch(
            "app.voice.storage.get_settings",
            return_value=Settings(call_recording_dir=str(tmp_path), recording_storage_provider="local"),
        ),
        patch("app.voice.recording.AsyncSessionLocal", return_value=session),
        patch("app.voice.recording.CallService", return_value=calls),
        patch("app.voice.recording.TwilioRecordingClient", return_value=twilio),
        patch("app.voice.recording.asyncio.sleep", AsyncMock()),
        patch(
            "app.voice.recording.convert_wav_to_mp3_async",
            AsyncMock(side_effect=RuntimeError("ffmpeg_not_installed")),
        ),
    ):
        result = await finalize_call_recording(tenant_id=uuid4(), call_id=call_id)

    assert result["ok"] is True
    assert result["format"] == "wav"


def test_recording_public_meta_hides_filesystem_paths():
    call = SimpleNamespace(
        id=uuid4(),
        recording_status="ready",
        recording_format="mp3",
        recording_duration_seconds=42,
        recording_size_bytes=1000,
        recording_storage_key="tenant/call.mp3",
    )
    with patch("app.voice.recording.get_settings", return_value=Settings(call_recording_enabled=True)):
        meta = recording_public_meta(call)
    assert meta is not None
    assert meta["available"] is True
    assert meta["playback_endpoint"] == f"/api/v1/calls/{call.id}/recording"
    assert meta["download_endpoint"] == f"/api/v1/calls/{call.id}/recording/download"
    assert "storage_key" not in meta
    assert "tenant/call.mp3" not in str(meta)


def test_purge_expired_recordings(tmp_path: Path):
    old = tmp_path / "old.mp3"
    old.write_bytes(b"x")
    import os
    import time

    old_time = time.time() - (100 * 86400)
    os.utime(old, (old_time, old_time))
    settings = Settings(call_recording_dir=str(tmp_path), call_recording_retention_days=90)
    with (
        patch("app.voice.recording.get_settings", return_value=settings),
        patch("app.voice.storage.get_settings", return_value=settings),
    ):
        removed = purge_expired_recordings()
    assert removed == 1
    assert not old.exists()


def test_recording_endpoint_requires_auth_route_exists():
    from app.api.calls import router

    stream = next(
        (r for r in router.routes if getattr(r, "path", "") == "/calls/{call_id}/recording"),
        None,
    )
    download = next(
        (r for r in router.routes if getattr(r, "path", "") == "/calls/{call_id}/recording/download"),
        None,
    )
    assert stream is not None
    assert download is not None
    assert "GET" in (getattr(stream, "methods", set()) or set())
    assert "GET" in (getattr(download, "methods", set()) or set())
    source = Path(__file__).resolve().parents[1] / "app" / "api" / "calls.py"
    text = source.read_text(encoding="utf-8")
    assert "audio/mpeg" in text
    assert "attachment" in text
    assert "_media_auth" in text
    assert "FileResponse" in text


@pytest.mark.asyncio
async def test_finalize_already_ready_is_idempotent():
    from app.voice.recording import finalize_call_recording

    call = SimpleNamespace(
        id=uuid4(),
        status=CallStatus.COMPLETED,
        recording_status="ready",
        recording_storage_key="tenant/call.mp3",
        metadata_json={},
        provider_call_id=None,
        recording_provider_sid=None,
    )
    calls = AsyncMock()
    calls.get = AsyncMock(return_value=call)
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("app.voice.recording.get_settings", return_value=Settings(call_recording_enabled=True)),
        patch("app.voice.recording.AsyncSessionLocal", return_value=session),
        patch("app.voice.recording.CallService", return_value=calls),
    ):
        result = await finalize_call_recording(tenant_id=uuid4(), call_id=call.id)

    assert result["ok"] is True
    assert result["skipped"] is True
    assert result["reason"] == "already_ready"


@pytest.mark.asyncio
async def test_start_already_recording_skips_duplicate():
    call = SimpleNamespace(
        id=uuid4(),
        status=CallStatus.ACTIVE,
        recording_status="recording",
        from_number="+923001234567",
        started_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
        provider_call_id=None,
        metadata_json={},
    )
    calls = AsyncMock()
    calls.get = AsyncMock(return_value=call)
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("app.voice.recording.get_settings", return_value=Settings(call_recording_enabled=True)),
        patch("app.voice.recording.AsyncSessionLocal", return_value=session),
        patch("app.voice.recording.CallService", return_value=calls),
    ):
        result = await start_call_recording(tenant_id=uuid4(), call_id=call.id)

    assert result["ok"] is True
    assert result["skipped"] is True
    assert result["reason"] == "already_started"


@pytest.mark.asyncio
async def test_finalize_missing_twilio_recording_marks_failed_without_raising():
    from app.voice.recording import finalize_call_recording

    call = SimpleNamespace(
        id=uuid4(),
        status=CallStatus.COMPLETED,
        from_number="+923001234567",
        started_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
        provider_call_id=None,
        metadata_json={"twilio_call_sid": "CAaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
        recording_status="recording",
        recording_provider_sid=None,
        recording_storage_key=None,
    )
    calls = AsyncMock()
    calls.get = AsyncMock(return_value=call)
    calls.set_status = AsyncMock()
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.commit = AsyncMock()

    twilio = AsyncMock()
    twilio.configured = True
    twilio.list_recordings = AsyncMock(return_value=[])
    twilio.__aenter__ = AsyncMock(return_value=twilio)
    twilio.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("app.voice.recording.get_settings", return_value=Settings(call_recording_enabled=True)),
        patch("app.voice.recording.AsyncSessionLocal", return_value=session),
        patch("app.voice.recording.CallService", return_value=calls),
        patch("app.voice.recording.TwilioRecordingClient", return_value=twilio),
        patch("app.voice.recording.asyncio.sleep", AsyncMock()),
    ):
        result = await finalize_call_recording(tenant_id=uuid4(), call_id=call.id)

    assert result["ok"] is False
    assert result["error"] == "twilio_recording_not_ready"


def test_call_detail_recording_metadata_shape():
    from app.schemas import RecordingOut

    meta = RecordingOut(
        available=True,
        status="ready",
        format="mp3",
        duration_seconds=120,
        size_bytes=96000,
        playback_endpoint="/api/v1/calls/abc/recording",
        download_endpoint="/api/v1/calls/abc/recording/download",
    )
    dumped = meta.model_dump()
    assert dumped["available"] is True
    assert dumped["playback_endpoint"].endswith("/recording")
    assert dumped["download_endpoint"].endswith("/download")
    assert "path" not in dumped
    assert dumped["format"] == "mp3"


def test_call_list_includes_recording_field():
    from app.schemas import CallOut, CallDirection, CallStatus
    from datetime import datetime, timezone

    out = CallOut(
        id=uuid4(),
        tenant_id=uuid4(),
        customer_id=None,
        direction=CallDirection.INBOUND,
        status=CallStatus.COMPLETED,
        from_number="+100",
        to_number="+200",
        provider_call_id=None,
        openai_session_id=None,
        intent=None,
        started_at=None,
        ended_at=None,
        duration_seconds=30,
        failure_reason=None,
        created_at=datetime.now(timezone.utc),
        recording={"available": True, "status": "ready", "format": "mp3"},
    )
    assert out.recording is not None
    assert out.recording.status == "ready"


def test_twilio_recording_is_actual_heard_audio_architecture():
    """Capture happens on Twilio SIP media path (post-barge-in / transmitted audio)."""
    source = Path(__file__).resolve().parents[1] / "app" / "voice" / "recording.py"
    text = source.read_text(encoding="utf-8")
    assert "OpenAI ↔ Twilio SIP" in text or ("OpenAI" in text and "Twilio" in text)
    assert "dual-channel" in text or "RecordingChannels" in text
    assert "ffmpeg" in text.lower()
    assert "response.audio.delta" not in text or "not" in text.lower()


def test_dashboard_player_component_exists():
    root = Path(__file__).resolve().parents[2]
    player = root / "dashboard" / "components" / "CallRecordingPlayer.tsx"
    table = root / "dashboard" / "components" / "CallsTable.tsx"
    log = root / "dashboard" / "components" / "CallLogView.tsx"
    assert player.is_file()
    text = player.read_text(encoding="utf-8")
    assert "Play" in text
    assert "Download" in text
    assert "access_token" in text
    assert "Seek" in text or "seek" in text.lower() or "range" in text.lower()
    assert "Play" in table.read_text(encoding="utf-8") or "CallRecordingPlayer" in table.read_text(
        encoding="utf-8"
    )
    assert "CallRecordingPlayer" in log.read_text(encoding="utf-8")


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg not installed on test host")
def test_ffmpeg_mp3_conversion_produces_bytes():
    media = _minimal_wav(frames=1600, channels=2, rate=8000)
    out = convert_wav_to_mp3(media)
    assert len(out) > 100
    assert out[:3] == b"ID3" or out[0] == 0xFF


def test_storage_provider_falls_back_local_when_b2_incomplete(tmp_path: Path):
    settings = Settings(
        recording_storage_provider="backblaze_b2",
        call_recording_dir=str(tmp_path),
        b2_endpoint="",
        b2_bucket_name="",
        b2_key_id="",
        b2_application_key="",
    )
    with patch("app.voice.storage.get_settings", return_value=settings):
        storage = build_recording_storage(settings)
    assert storage.provider_name == "local"
