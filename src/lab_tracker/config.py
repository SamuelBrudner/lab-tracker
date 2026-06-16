"""Configuration management for lab tracker."""

from __future__ import annotations

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_AUTH_SECRET_KEY = "dev-only-change-me"
INSECURE_AUTH_SECRET_KEYS = {
    DEFAULT_AUTH_SECRET_KEY,
    "replace-with-a-strong-secret",
    "replace-with-a-strong-random-secret",
    "change-me",
    "changeme",
}


class Settings(BaseSettings):
    app_name: str = "lab-tracker"
    environment: str = "local"
    log_level: str = "INFO"
    database_url: str = "sqlite+pysqlite:///./lab_tracker.db"
    file_storage_path: str = "./file_storage"
    note_storage_path: str = "./note_storage"
    auth_secret_key: str = DEFAULT_AUTH_SECRET_KEY
    auth_token_ttl_minutes: int = 60 * 12
    auth_invite_ttl_hours: int = 7 * 24
    bootstrap_admin_token: str = ""
    auth_enabled: bool | None = None
    graph_draft_provider: str = "openai"
    public_base_url: str = ""
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_transcription_model: str = "gpt-4o-mini-transcribe"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_timeout_seconds: float = 60.0
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-3-5-sonnet-latest"
    anthropic_base_url: str = "https://api.anthropic.com/v1"
    anthropic_timeout_seconds: float = 60.0
    google_api_key: str = ""
    google_model: str = "gemini-2.5-flash"
    google_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    google_timeout_seconds: float = 60.0

    def is_auth_enabled(self) -> bool:
        if self.auth_enabled is not None:
            return self.auth_enabled
        return self.environment.strip().lower() != "local"

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if cleaned.startswith("postgres://"):
            return f"postgresql+psycopg://{cleaned.removeprefix('postgres://')}"
        if cleaned.startswith("postgresql://"):
            return f"postgresql+psycopg://{cleaned.removeprefix('postgresql://')}"
        return cleaned

    @model_validator(mode="after")
    def _validate_auth_secret_key(self) -> Settings:
        is_local = self.environment.strip().lower() == "local"
        auth_secret_key = self.auth_secret_key.strip()
        self.auth_secret_key = auth_secret_key
        if not is_local and not self.is_auth_enabled():
            raise ValueError(
                "LAB_TRACKER_AUTH_ENABLED=false is only allowed when "
                "LAB_TRACKER_ENVIRONMENT is 'local'."
            )
        if self.is_auth_enabled() and auth_secret_key in INSECURE_AUTH_SECRET_KEYS:
            raise ValueError(
                "LAB_TRACKER_AUTH_SECRET_KEY must be set to a strong "
                "non-placeholder value when authentication is enabled."
            )
        return self

    model_config = SettingsConfigDict(
        env_prefix="LAB_TRACKER_",
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )


def get_settings() -> Settings:
    return Settings()
