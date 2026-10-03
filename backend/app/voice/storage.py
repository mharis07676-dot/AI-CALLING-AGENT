"""Recording object storage: local disk + S3-compatible (Backblaze B2 / Wasabi / S3).

Recording capture logic stays in recording.py; this module only put/get/delete bytes.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


class RecordingStorage(ABC):
    """Abstract storage for finalized call recordings (never store MP3 in Postgres)."""

    @property
    @abstractmethod
    def provider_name(self) -> str: ...

    @abstractmethod
    def put(self, key: str, data: bytes, *, content_type: str) -> None: ...

    @abstractmethod
    def get(self, key: str) -> bytes | None: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def delete(self, key: str) -> bool: ...

    def local_path(self, key: str) -> Path | None:
        """Filesystem path when the provider keeps a local file; else None."""
        return None


class LocalRecordingStorage(RecordingStorage):
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def provider_name(self) -> str:
        return "local"

    def _path(self, key: str) -> Path:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def put(self, key: str, data: bytes, *, content_type: str) -> None:
        path = self._path(key)
        path.write_bytes(data)
        logger.info(
            "CALL_RECORDING_UPLOAD_STARTED provider=local key_suffix=%s bytes=%s",
            key.split("/")[-1],
            len(data),
        )

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        if not path.is_file():
            return None
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> bool:
        path = self._path(key)
        if path.is_file():
            path.unlink(missing_ok=True)
            return True
        return False

    def local_path(self, key: str) -> Path | None:
        path = self._path(key)
        return path if path.is_file() else None


class S3CompatibleRecordingStorage(RecordingStorage):
    """Backblaze B2 / Wasabi / AWS S3 via boto3 S3 API."""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint: str,
        access_key: str,
        secret_key: str,
        region: str,
        provider: str,
        local_cache: Path | None = None,
    ) -> None:
        self.bucket = bucket
        self.endpoint = endpoint.rstrip("/")
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region or "auto"
        self._provider = provider
        self.local_cache = local_cache
        if self.local_cache is not None:
            self.local_cache.mkdir(parents=True, exist_ok=True)
        self._client: Any = None

    @property
    def provider_name(self) -> str:
        return self._provider

    def _s3(self) -> Any:
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint or None,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                region_name=self.region,
                config=Config(signature_version="s3v4"),
            )
        return self._client

    def put(self, key: str, data: bytes, *, content_type: str) -> None:
        logger.info(
            "CALL_RECORDING_UPLOAD_STARTED provider=%s key_suffix=%s bytes=%s",
            self._provider,
            key.split("/")[-1],
            len(data),
        )
        self._s3().put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        if self.local_cache is not None:
            path = self.local_cache / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def get(self, key: str) -> bytes | None:
        if self.local_cache is not None:
            cached = self.local_cache / key
            if cached.is_file():
                return cached.read_bytes()
        try:
            obj = self._s3().get_object(Bucket=self.bucket, Key=key)
            data = obj["Body"].read()
        except Exception:  # noqa: BLE001
            logger.exception("CALL_RECORDING_STORAGE_GET_FAILED key_suffix=%s", key.split("/")[-1])
            return None
        if self.local_cache is not None and data:
            path = self.local_cache / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return data

    def exists(self, key: str) -> bool:
        if self.local_cache is not None and (self.local_cache / key).is_file():
            return True
        try:
            self._s3().head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:  # noqa: BLE001
            return False

    def delete(self, key: str) -> bool:
        try:
            self._s3().delete_object(Bucket=self.bucket, Key=key)
            if self.local_cache is not None:
                (self.local_cache / key).unlink(missing_ok=True)
            return True
        except Exception:  # noqa: BLE001
            return False

    def local_path(self, key: str) -> Path | None:
        if self.local_cache is None:
            return None
        path = self.local_cache / key
        if path.is_file():
            return path
        data = self.get(key)
        if not data:
            return None
        return path if path.is_file() else None

    def signed_url(self, key: str, *, expires_seconds: int = 300) -> str | None:
        try:
            return self._s3().generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=expires_seconds,
            )
        except Exception:  # noqa: BLE001
            logger.exception("CALL_RECORDING_SIGNED_URL_FAILED")
            return None


def recordings_root_from_settings(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    root = Path(settings.call_recording_dir or "recordings")
    if not root.is_absolute():
        root = Path.cwd() / root
    root.mkdir(parents=True, exist_ok=True)
    return root


def storage_provider_name(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    raw = (settings.recording_storage_provider or settings.call_recording_storage or "local").strip().lower()
    aliases = {
        "b2": "backblaze_b2",
        "backblaze": "backblaze_b2",
        "r2": "s3_compatible",
        "s3": "s3_compatible",
        "wasabi": "s3_compatible",
        "aws_s3": "s3_compatible",
    }
    return aliases.get(raw, raw)


def build_recording_storage(settings: Settings | None = None) -> RecordingStorage:
    settings = settings or get_settings()
    provider = storage_provider_name(settings)
    root = recordings_root_from_settings(settings)

    if provider in {"local", ""}:
        return LocalRecordingStorage(root)

    if provider in {"backblaze_b2", "s3_compatible"}:
        endpoint = (settings.b2_endpoint or settings.storage_endpoint or "").strip()
        bucket = (settings.b2_bucket_name or settings.storage_bucket or "").strip()
        access = (settings.b2_key_id or settings.storage_access_key or "").strip()
        secret = (settings.b2_application_key or settings.storage_secret_key or "").strip()
        region = (settings.b2_region or settings.storage_region or "auto").strip()
        if not (endpoint and bucket and access and secret):
            logger.warning(
                "CALL_RECORDING_STORAGE_FALLBACK_LOCAL reason=remote_not_configured provider=%s",
                provider,
            )
            return LocalRecordingStorage(root)
        return S3CompatibleRecordingStorage(
            bucket=bucket,
            endpoint=endpoint,
            access_key=access,
            secret_key=secret,
            region=region,
            provider=provider,
            local_cache=root,
        )

    logger.warning("CALL_RECORDING_STORAGE_UNKNOWN provider=%s; using local", provider)
    return LocalRecordingStorage(root)


@lru_cache
def get_recording_storage() -> RecordingStorage:
    return build_recording_storage()


def reset_recording_storage_cache() -> None:
    get_recording_storage.cache_clear()
