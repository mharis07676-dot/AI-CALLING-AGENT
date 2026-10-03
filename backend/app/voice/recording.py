"""Call recording via Twilio (actual SIP media path).

Architecture note:
  Live audio is OpenAI ↔ Twilio SIP. This app never sees caller RTP.
  Sideband only observes control events / optional AI audio deltas — not a full
  duplex conversation. Twilio dual-channel recording is therefore the correct
  capture point for "what the caller heard" + caller audio with real timing.

Post-call workflow:
  Twilio WAV download → optional FFmpeg MP3 convert → RecordingStorage upload → DB ready
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

from app.config import get_settings
from app.db.session import AsyncSessionLocal
from app.models import Call
from app.services import CallService
from app.voice.storage import (
    build_recording_storage,
    recordings_root_from_settings,
    reset_recording_storage_cache,
)

logger = logging.getLogger(__name__)

_CA_SID_RE = re.compile(r"^CA[0-9a-fA-F]{32}$")
_RECORDING_SID_RE = re.compile(r"^RE[0-9a-fA-F]{32}$")


def recording_enabled() -> bool:
    return bool(get_settings().call_recording_enabled)


def recording_notice_enabled() -> bool:
    settings = get_settings()
    return bool(settings.call_recording_enabled and settings.call_recording_notice_enabled)


def recording_notice_text() -> str:
    settings = get_settings()
    return (settings.call_recording_notice_text or "").strip()


def recordings_root() -> Path:
    return recordings_root_from_settings()


def storage_key_for_call(*, tenant_id: UUID, call_id: UUID, ext: str = "mp3") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{tenant_id}/{call_id}_{stamp}.{ext.lstrip('.')}"


def absolute_path_for_key(storage_key: str) -> Path:
    path = recordings_root() / storage_key
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def resolve_recording_path(storage_key: str) -> Path | None:
    """Return a readable local path for streaming (uses storage cache when remote)."""
    storage = build_recording_storage()
    local = storage.local_path(storage_key)
    if local is not None and local.is_file():
        return local
    # Fallback for legacy local-only keys written before storage abstraction.
    path = absolute_path_for_key(storage_key)
    return path if path.is_file() else None


def _twilio_auth() -> tuple[str, str] | None:
    settings = get_settings()
    if not settings.twilio_account_sid:
        return None
    if settings.twilio_api_key_sid and settings.twilio_api_key_secret:
        return (settings.twilio_api_key_sid, settings.twilio_api_key_secret)
    if settings.twilio_auth_token:
        return (settings.twilio_account_sid, settings.twilio_auth_token)
    return None


def looks_like_twilio_call_sid(value: str | None) -> bool:
    return bool(value and _CA_SID_RE.match(value.strip()))


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def convert_wav_to_mp3(wav_bytes: bytes) -> bytes:
    """Convert Twilio WAV → MP3 via FFmpeg. Preserves WAV sample rate/channels from header.

    Runs outside the realtime audio path (post-call only).
    """
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg_not_installed")
    with tempfile.TemporaryDirectory(prefix="call-rec-") as tmp:
        wav_path = Path(tmp) / "in.wav"
        mp3_path = Path(tmp) / "out.mp3"
        wav_path.write_bytes(wav_bytes)
        # Let FFmpeg read the WAV header (typically 8 kHz dual-channel from Twilio).
        # Do not force a sample rate — wrong reinterpretation causes pitch/drift.
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(wav_path),
            "-codec:a",
            "libmp3lame",
            "-qscale:a",
            "4",
            str(mp3_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, check=False)
        if proc.returncode != 0 or not mp3_path.is_file():
            err = (proc.stderr or b"").decode("utf-8", errors="replace")[:200]
            raise RuntimeError(f"ffmpeg_failed:{err}")
        return mp3_path.read_bytes()


async def convert_wav_to_mp3_async(wav_bytes: bytes) -> bytes:
    return await asyncio.to_thread(convert_wav_to_mp3, wav_bytes)


class TwilioRecordingClient:
    """Thin Twilio REST helper for live call recordings."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.settings = get_settings()
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> TwilioRecordingClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
            self._owns_client = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    @property
    def configured(self) -> bool:
        return bool(self.settings.twilio_account_sid and _twilio_auth())

    def _auth(self) -> tuple[str, str]:
        auth = _twilio_auth()
        if auth is None:
            raise RuntimeError("twilio_auth_missing")
        return auth

    def _account_base(self) -> str:
        return (
            f"https://api.twilio.com/2010-04-01/Accounts/"
            f"{self.settings.twilio_account_sid}"
        )

    async def find_call_sid(
        self,
        *,
        from_number: str,
        started_at: datetime | None,
    ) -> str | None:
        """Resolve Twilio CallSid for a recent inbound trunk call."""
        if not self.configured or not from_number or from_number == "unknown":
            return None
        assert self._client is not None
        params: dict[str, str] = {"From": from_number, "PageSize": "20"}
        if started_at is not None:
            window = started_at.astimezone(timezone.utc) - timedelta(minutes=2)
            params["StartTime>"] = window.strftime("%Y-%m-%dT%H:%M:%SZ")
        url = f"{self._account_base()}/Calls.json"
        response = await self._client.get(url, params=params, auth=self._auth())
        if response.status_code >= 400:
            logger.warning(
                "CALL_RECORDING_CALLSID_LOOKUP_FAILED status=%s",
                response.status_code,
            )
            return None
        calls = (response.json() or {}).get("calls") or []
        if not calls:
            return None
        for row in calls:
            sid = str(row.get("sid") or "")
            if looks_like_twilio_call_sid(sid):
                return sid
        return None

    async def start_dual_recording(self, call_sid: str) -> dict[str, Any]:
        assert self._client is not None
        url = f"{self._account_base()}/Calls/{call_sid}/Recordings.json"
        channels = "dual" if self.settings.call_recording_mode == "stereo" else "mono"
        data = {
            "RecordingChannels": channels,
            "Trim": "do-not-trim",
        }
        response = await self._client.post(url, data=data, auth=self._auth())
        if response.status_code >= 400:
            body = (response.text or "")[:180].replace("\n", " ")
            return {
                "ok": False,
                "status_code": response.status_code,
                "error": "twilio_start_recording_rejected",
                "message": body,
            }
        payload = response.json() or {}
        return {
            "ok": True,
            "recording_sid": payload.get("sid"),
            "status": payload.get("status"),
            "channels": channels,
        }

    async def list_recordings(self, call_sid: str) -> list[dict[str, Any]]:
        assert self._client is not None
        url = f"{self._account_base()}/Calls/{call_sid}/Recordings.json"
        response = await self._client.get(url, auth=self._auth())
        if response.status_code >= 400:
            return []
        return list((response.json() or {}).get("recordings") or [])

    async def download_wav(self, recording_sid: str, *, dual: bool = True) -> bytes | None:
        assert self._client is not None
        url = f"{self._account_base()}/Recordings/{recording_sid}.wav"
        params = {"RequestedChannels": "2"} if dual else {"RequestedChannels": "1"}
        response = await self._client.get(url, params=params, auth=self._auth())
        if response.status_code >= 400 and dual:
            response = await self._client.get(url, auth=self._auth())
        if response.status_code >= 400:
            logger.warning(
                "CALL_RECORDING_DOWNLOAD_FAILED sid=%s status=%s",
                recording_sid,
                response.status_code,
            )
            return None
        return response.content


async def start_call_recording(
    *,
    tenant_id: UUID,
    call_id: UUID,
) -> dict[str, Any]:
    """Begin Twilio recording for an active call (best-effort, never raises)."""
    if not recording_enabled():
        return {"ok": True, "skipped": True, "reason": "disabled"}

    try:
        async with AsyncSessionLocal() as db:
            calls = CallService(db, tenant_id)
            call = await calls.get(call_id)
            if call is None:
                return {"ok": False, "error": "call_not_found"}
            if call.recording_status in {"recording", "processing", "ready"}:
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": "already_started",
                    "status": call.recording_status,
                }

            await calls.set_status(
                call_id,
                call.status,
                recording_status="recording",
                recording_format=(get_settings().call_recording_format or "mp3"),
            )
            await db.commit()
            from_number = call.from_number
            started_at = call.started_at or call.created_at
            provider_call_id = call.provider_call_id
            meta = dict(call.metadata_json or {})

        logger.info("CALL_RECORDING_STARTED call_id=%s", call_id)

        async with TwilioRecordingClient() as twilio:
            if not twilio.configured:
                await _set_recording_fields(
                    tenant_id=tenant_id,
                    call_id=call_id,
                    recording_status="failed",
                    failure_note="twilio_not_configured",
                )
                return {"ok": False, "error": "twilio_not_configured"}

            call_sid = None
            if looks_like_twilio_call_sid(provider_call_id):
                call_sid = provider_call_id
            elif looks_like_twilio_call_sid(meta.get("twilio_call_sid")):
                call_sid = str(meta.get("twilio_call_sid"))
            else:
                for attempt in range(1, 4):
                    call_sid = await twilio.find_call_sid(
                        from_number=from_number,
                        started_at=started_at,
                    )
                    if call_sid:
                        break
                    await asyncio.sleep(0.75 * attempt)

            if not call_sid:
                await _set_recording_fields(
                    tenant_id=tenant_id,
                    call_id=call_id,
                    recording_status="failed",
                    failure_note="twilio_call_sid_not_found",
                )
                logger.warning("CALL_RECORDING_FAILED call_id=%s reason=call_sid_not_found", call_id)
                return {"ok": False, "error": "twilio_call_sid_not_found"}

            start = await twilio.start_dual_recording(call_sid)
            async with AsyncSessionLocal() as db:
                calls = CallService(db, tenant_id)
                call = await calls.get(call_id)
                if call is None:
                    return {"ok": False, "error": "call_not_found"}
                meta = dict(call.metadata_json or {})
                meta["twilio_call_sid"] = call_sid
                if start.get("ok"):
                    meta["twilio_recording_sid"] = start.get("recording_sid")
                    await calls.set_status(
                        call_id,
                        call.status,
                        recording_status="recording",
                        recording_provider="twilio",
                        recording_provider_sid=start.get("recording_sid"),
                        metadata_json=meta,
                    )
                    logger.info(
                        "CALL_RECORDING_AGENT_AUDIO_ACTIVE call_id=%s provider=twilio",
                        call_id,
                    )
                    logger.info(
                        "CALL_RECORDING_CALLER_AUDIO_ACTIVE call_id=%s provider=twilio",
                        call_id,
                    )
                else:
                    meta["twilio_start_recording_error"] = start.get("error")
                    await calls.set_status(
                        call_id,
                        call.status,
                        recording_status="recording",
                        recording_provider="twilio",
                        metadata_json=meta,
                    )
                    logger.warning(
                        "CALL_RECORDING_START_API_REJECTED call_id=%s error=%s "
                        "(will try trunk auto-recording on finalize)",
                        call_id,
                        start.get("error"),
                    )
                await db.commit()

            return {"ok": True, "call_sid": call_sid, "start": start}
    except Exception:  # noqa: BLE001 - recording must never crash the call
        logger.exception("CALL_RECORDING_FAILED call_id=%s", call_id)
        await _set_recording_fields(
            tenant_id=tenant_id,
            call_id=call_id,
            recording_status="failed",
            failure_note="start_exception",
        )
        return {"ok": False, "error": "start_exception"}


async def finalize_call_recording(
    *,
    tenant_id: UUID,
    call_id: UUID,
) -> dict[str, Any]:
    """Download Twilio WAV, convert to MP3 if configured, upload, mark ready."""
    if not recording_enabled():
        return {"ok": True, "skipped": True, "reason": "disabled"}

    try:
        async with AsyncSessionLocal() as db:
            calls = CallService(db, tenant_id)
            call = await calls.get(call_id)
            if call is None:
                return {"ok": False, "error": "call_not_found"}
            if call.recording_status == "ready" and call.recording_storage_key:
                return {"ok": True, "skipped": True, "reason": "already_ready"}
            meta = dict(call.metadata_json or {})
            call_sid = meta.get("twilio_call_sid") or (
                call.provider_call_id if looks_like_twilio_call_sid(call.provider_call_id) else None
            )
            recording_sid = call.recording_provider_sid or meta.get("twilio_recording_sid")
            await calls.set_status(call_id, call.status, recording_status="processing")
            await db.commit()

        logger.info("CALL_RECORDING_FINALIZING call_id=%s", call_id)

        async with TwilioRecordingClient() as twilio:
            if not twilio.configured:
                await _set_recording_fields(
                    tenant_id=tenant_id,
                    call_id=call_id,
                    recording_status="failed",
                    failure_note="twilio_not_configured",
                )
                return {"ok": False, "error": "twilio_not_configured"}

            if not call_sid:
                async with AsyncSessionLocal() as db:
                    call = await CallService(db, tenant_id).get(call_id)
                    from_number = call.from_number if call else ""
                    started_at = call.started_at if call else None
                call_sid = await twilio.find_call_sid(
                    from_number=from_number,
                    started_at=started_at,
                )

            if not call_sid:
                await _set_recording_fields(
                    tenant_id=tenant_id,
                    call_id=call_id,
                    recording_status="failed",
                    failure_note="twilio_call_sid_not_found",
                )
                return {"ok": False, "error": "twilio_call_sid_not_found"}

            chosen: dict[str, Any] | None = None
            for attempt in range(1, 8):
                recordings = await twilio.list_recordings(str(call_sid))
                completed = [
                    r
                    for r in recordings
                    if str(r.get("status") or "").lower() == "completed"
                    and _RECORDING_SID_RE.match(str(r.get("sid") or ""))
                ]
                if recording_sid:
                    for row in completed:
                        if row.get("sid") == recording_sid:
                            chosen = row
                            break
                if chosen is None and completed:
                    # Prefer longest segment so AI + human portions stay on one Call record
                    # when Twilio produces multiple recordings for the same CallSid.
                    chosen = max(
                        completed,
                        key=lambda row: int(row.get("duration") or 0),
                    )
                if chosen is not None:
                    break
                await asyncio.sleep(min(1.5 * attempt, 6.0))

            if chosen is None:
                await _set_recording_fields(
                    tenant_id=tenant_id,
                    call_id=call_id,
                    recording_status="failed",
                    failure_note="twilio_recording_not_ready",
                )
                logger.warning("CALL_RECORDING_FAILED call_id=%s reason=not_ready", call_id)
                return {"ok": False, "error": "twilio_recording_not_ready"}

            sid = str(chosen.get("sid"))
            # Preserve every segment SID on the same Call — never silently overwrite history.
            all_sids = [
                str(r.get("sid"))
                for r in (await twilio.list_recordings(str(call_sid)))
                if _RECORDING_SID_RE.match(str(r.get("sid") or ""))
            ]
            dual = get_settings().call_recording_mode == "stereo"
            media = await twilio.download_wav(sid, dual=dual)
            if not media or len(media) < 44:
                await _set_recording_fields(
                    tenant_id=tenant_id,
                    call_id=call_id,
                    recording_status="failed",
                    failure_note="twilio_download_empty",
                )
                return {"ok": False, "error": "twilio_download_empty"}

            wanted_fmt = (get_settings().call_recording_format or "mp3").lower().strip()
            final_bytes = media
            final_fmt = "wav"
            content_type = "audio/wav"
            if wanted_fmt == "mp3":
                try:
                    final_bytes = await convert_wav_to_mp3_async(media)
                    final_fmt = "mp3"
                    content_type = "audio/mpeg"
                    logger.info(
                        "CALL_RECORDING_CONVERTED call_id=%s format=mp3 bytes=%s",
                        call_id,
                        len(final_bytes),
                    )
                except Exception as exc:  # noqa: BLE001
                    # Keep WAV playable rather than failing the whole recording.
                    logger.warning(
                        "CALL_RECORDING_MP3_FALLBACK_WAV call_id=%s error=%s",
                        call_id,
                        type(exc).__name__,
                    )
                    final_bytes = media
                    final_fmt = "wav"
                    content_type = "audio/wav"

            key = storage_key_for_call(tenant_id=tenant_id, call_id=call_id, ext=final_fmt)
            storage = build_recording_storage()
            await asyncio.to_thread(
                storage.put,
                key,
                final_bytes,
                content_type=content_type,
            )

            duration = None
            raw_duration = chosen.get("duration")
            try:
                if raw_duration is not None:
                    duration = int(raw_duration)
            except (TypeError, ValueError):
                duration = None

            async with AsyncSessionLocal() as db:
                calls = CallService(db, tenant_id)
                call = await calls.get(call_id)
                if call is None:
                    return {"ok": False, "error": "call_not_found"}
                meta = dict(call.metadata_json or {})
                previous_sid = call.recording_provider_sid or meta.get("twilio_recording_sid")
                meta["twilio_call_sid"] = call_sid
                meta["twilio_recording_sid"] = sid
                meta["recording_storage_provider"] = storage.provider_name
                segment_sids = list(dict.fromkeys([*(meta.get("twilio_recording_sids") or []), *all_sids]))
                if previous_sid and previous_sid not in segment_sids:
                    segment_sids.insert(0, str(previous_sid))
                meta["twilio_recording_sids"] = segment_sids
                if previous_sid and previous_sid != sid:
                    meta["twilio_recording_sid_previous"] = previous_sid
                meta.pop("recording_error", None)
                await calls.set_status(
                    call_id,
                    call.status,
                    recording_status="ready",
                    recording_format=final_fmt,
                    recording_duration_seconds=duration,
                    recording_size_bytes=len(final_bytes),
                    recording_storage_key=key,
                    recording_provider="twilio",
                    recording_provider_sid=sid,
                    metadata_json=meta,
                )
                await calls.add_event(
                    call_id,
                    "call.recording_ready",
                    {
                        "format": final_fmt,
                        "duration_seconds": duration,
                        "size_bytes": len(final_bytes),
                        "mode": get_settings().call_recording_mode,
                        "storage": storage.provider_name,
                    },
                )
                await db.commit()

            logger.info(
                "CALL_RECORDING_READY call_id=%s duration_seconds=%s size_bytes=%s format=%s",
                call_id,
                duration,
                len(final_bytes),
                final_fmt,
            )
            return {
                "ok": True,
                "storage_key": key,
                "duration_seconds": duration,
                "size_bytes": len(final_bytes),
                "format": final_fmt,
            }
    except Exception:  # noqa: BLE001
        logger.exception("CALL_RECORDING_FAILED call_id=%s", call_id)
        await _set_recording_fields(
            tenant_id=tenant_id,
            call_id=call_id,
            recording_status="failed",
            failure_note="finalize_exception",
        )
        return {"ok": False, "error": "finalize_exception"}


async def _set_recording_fields(
    *,
    tenant_id: UUID,
    call_id: UUID,
    recording_status: str,
    failure_note: str | None = None,
) -> None:
    try:
        async with AsyncSessionLocal() as db:
            calls = CallService(db, tenant_id)
            call = await calls.get(call_id)
            if call is None:
                return
            meta = dict(call.metadata_json or {})
            if failure_note:
                meta["recording_error"] = failure_note
            await calls.set_status(
                call_id,
                call.status,
                recording_status=recording_status,
                metadata_json=meta,
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("CALL_RECORDING_STATUS_UPDATE_FAILED call_id=%s", call_id)


def schedule_start_recording(*, tenant_id: UUID, call_id: UUID) -> None:
    if not recording_enabled():
        return
    asyncio.create_task(
        start_call_recording(tenant_id=tenant_id, call_id=call_id),
        name=f"call-recording-start-{call_id}",
    )


def schedule_finalize_recording(*, tenant_id: UUID, call_id: UUID) -> None:
    if not recording_enabled():
        return
    asyncio.create_task(
        finalize_call_recording(tenant_id=tenant_id, call_id=call_id),
        name=f"call-recording-finalize-{call_id}",
    )


def purge_expired_recordings(*, retention_days: int | None = None) -> int:
    """Delete local recording files older than retention. Safe to schedule later."""
    settings = get_settings()
    days = retention_days if retention_days is not None else settings.call_recording_retention_days
    if days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    removed = 0
    root = recordings_root()
    for path in root.rglob("*"):
        if path.suffix.lower() not in {".wav", ".mp3"}:
            continue
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed


def recording_public_meta(call: Call) -> dict[str, Any] | None:
    """Safe recording metadata for API responses (no filesystem paths / storage keys)."""
    status = call.recording_status
    if not status:
        if recording_enabled():
            return {
                "available": False,
                "status": "pending",
                "format": None,
                "duration_seconds": None,
                "playback_endpoint": None,
                "download_endpoint": None,
            }
        return None
    ready = status == "ready" and bool(call.recording_storage_key)
    return {
        "available": ready,
        "status": status,
        "format": call.recording_format,
        "duration_seconds": call.recording_duration_seconds,
        "size_bytes": call.recording_size_bytes,
        "playback_endpoint": f"/api/v1/calls/{call.id}/recording" if ready else None,
        "download_endpoint": f"/api/v1/calls/{call.id}/recording/download" if ready else None,
    }


# Re-export for tests that clear storage between cases.
__all__ = [
    "TwilioRecordingClient",
    "absolute_path_for_key",
    "convert_wav_to_mp3",
    "convert_wav_to_mp3_async",
    "ffmpeg_available",
    "finalize_call_recording",
    "looks_like_twilio_call_sid",
    "purge_expired_recordings",
    "recording_enabled",
    "recording_notice_enabled",
    "recording_notice_text",
    "recording_public_meta",
    "recordings_root",
    "reset_recording_storage_cache",
    "resolve_recording_path",
    "schedule_finalize_recording",
    "schedule_start_recording",
    "start_call_recording",
    "storage_key_for_call",
]
