from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    openai_webhook_secret: str = ""

    sip_provider_api_key: str = ""
    sip_provider_base_url: str = ""
    sip_trunk_id: str = ""
    openai_sip_project_id: str = ""

    cors_origins: str = "http://localhost:3000"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
