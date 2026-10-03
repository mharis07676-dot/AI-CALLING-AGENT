from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def to_async_postgres_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


def to_sync_postgres_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Synas Labs AI Agents"
    app_env: str = "development"
    debug: bool = True
    secret_key: str = "change-me-in-production"
    api_prefix: str = "/api/v1"
    max_concurrent_calls: int = 10

    database_url: str = "postgresql+asyncpg://synas:synas@localhost:5432/synas_agents"
    database_url_sync: str = "postgresql://synas:synas@localhost:5432/synas_agents"
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: str = "change-me-jwt-secret"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    openai_api_key: str = ""
    openai_realtime_model: str = "gpt-realtime"
    # OpenAI Realtime built-in voices. marin/cedar recommended for natural speech.
    openai_realtime_voice: str = "marin"
    openai_webhook_secret: str = ""
    openai_sip_project_id: str = ""

    # Call recording (Twilio captures actual SIP media; this app does not see RTP)
    call_recording_enabled: bool = False
    call_recording_format: str = "mp3"  # mp3 | wav
    call_recording_mode: str = "stereo"  # stereo (dual) | mono
    call_recording_storage: str = "local"  # legacy alias for RECORDING_STORAGE_PROVIDER
    call_recording_dir: str = "recordings"
    call_recording_retention_days: int = 90
    call_recording_notice_enabled: bool = False
    call_recording_notice_text: str = (
        "This call may be recorded for quality and training."
    )
    # Storage provider: local | backblaze_b2 | s3_compatible
    recording_storage_provider: str = "local"
    # Backblaze B2 (S3-compatible API) — preferred production storage
    b2_endpoint: str = ""
    b2_bucket_name: str = ""
    b2_key_id: str = ""
    b2_application_key: str = ""
    b2_region: str = "auto"
    # Generic S3-compatible aliases (Wasabi / R2 / S3 later)
    storage_endpoint: str = ""
    storage_bucket: str = ""
    storage_access_key: str = ""
    storage_secret_key: str = ""
    storage_region: str = ""

    # Legacy SIP adapter fields (kept for compatibility)
    sip_provider_api_key: str = ""
    sip_provider_base_url: str = ""
    sip_trunk_id: str = ""
    sip_provider_sip_url: str = ""
    sip_username: str = ""
    sip_password: str = ""
    sip_caller_number: str = ""

    # Twilio (Railway)
    twilio_account_sid: str = ""
    twilio_api_key_sid: str = ""
    twilio_api_key_secret: str = ""
    twilio_phone_number: str = ""
    twilio_auth_token: str = ""

    # Human handoff / live agent transfer (Twilio Dial on active CallSid)
    human_handoff_enabled: bool = False
    human_handoff_number: str = ""
    human_handoff_timeout_seconds: int = 25
    # Public HTTPS base of this API (Railway URL). Required for Twilio TwiML fetch.
    public_base_url: str = ""

    cors_origins: str = "http://localhost:3000"

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_async_database_url(cls, value: str) -> str:
        return to_async_postgres_url(value)

    @field_validator("database_url_sync", mode="before")
    @classmethod
    def normalize_sync_database_url(cls, value: str) -> str:
        return to_sync_postgres_url(value)

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod", "staging"}

    @property
    def voice_enabled(self) -> bool:
        """True when any voice/SIP/Twilio wiring is configured."""
        return bool(
            self.openai_sip_project_id
            or self.sip_trunk_id
            or self.twilio_account_sid
            or self.twilio_api_key_sid
            or self.sip_provider_base_url
        )

    @property
    def twilio_hangup_configured(self) -> bool:
        has_account = bool(self.twilio_account_sid)
        has_api_key = bool(self.twilio_api_key_sid and self.twilio_api_key_secret)
        has_auth_token = bool(self.twilio_auth_token)
        return has_account and (has_api_key or has_auth_token)

    @property
    def sip_adapter_configured(self) -> bool:
        return bool(self.sip_provider_api_key and self.sip_provider_base_url)

    @property
    def telephony_hangup_configured(self) -> bool:
        return self.twilio_hangup_configured or self.sip_adapter_configured

    @property
    def human_handoff_configured(self) -> bool:
        return bool(
            self.human_handoff_enabled
            and (self.human_handoff_number or "").strip()
            and self.twilio_hangup_configured
            and (self.public_base_url or "").strip()
        )


def _is_placeholder(value: str) -> bool:
    normalized = (value or "").strip().lower()
    return normalized in {"", "change-me-in-production", "change-me-jwt-secret"}


def validate_required_settings(settings: Settings) -> None:
    """Fail fast on missing production/voice env vars. Never log secret values."""
    missing: list[str] = []

    if settings.is_production:
        checks = [
            ("DATABASE_URL", settings.database_url),
            ("REDIS_URL", settings.redis_url),
            ("JWT_SECRET", settings.jwt_secret),
            ("SECRET_KEY", settings.secret_key),
        ]
        for name, value in checks:
            if _is_placeholder(value):
                missing.append(name)

    if settings.is_production or settings.voice_enabled:
        voice_checks = [
            ("OPENAI_API_KEY", settings.openai_api_key),
            ("OPENAI_REALTIME_MODEL", settings.openai_realtime_model),
            ("OPENAI_SIP_PROJECT_ID", settings.openai_sip_project_id),
            ("OPENAI_WEBHOOK_SECRET", settings.openai_webhook_secret),
            ("SIP_TRUNK_ID", settings.sip_trunk_id),
        ]
        for name, value in voice_checks:
            if _is_placeholder(value):
                missing.append(name)

        # Twilio API key pair (preferred) or legacy SIP provider key
        has_twilio_keys = bool(settings.twilio_api_key_sid and settings.twilio_api_key_secret)
        has_sip_provider_key = bool(settings.sip_provider_api_key)
        if not has_twilio_keys and not has_sip_provider_key:
            missing.extend(["TWILIO_API_KEY_SID", "TWILIO_API_KEY_SECRET"])

        # Account SID is required for Twilio REST hangup, but missing it must not
        # block inbound OpenAI SIP accept/startup when API keys + trunk are set.
        # Hangup will return a clear provider error until TWILIO_ACCOUNT_SID is added.

    # Deduplicate while preserving order
    ordered_missing = list(dict.fromkeys(missing))
    if ordered_missing:
        names = ", ".join(ordered_missing)
        raise RuntimeError(f"Missing required environment variable: {names}")


@lru_cache
def get_settings() -> Settings:
    return Settings()
