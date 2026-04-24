"""Configuration management for lab tracker."""

from __future__ import annotations

from uuid import UUID

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_AUTH_SECRET_KEY = "dev-only-change-me"


class Settings(BaseSettings):
    app_name: str = "lab-tracker"
    environment: str = "local"
    log_level: str = "INFO"
    database_url: str = "sqlite+pysqlite:///./lab_tracker.db"
    file_storage_path: str = "./file_storage"
    note_storage_path: str = "./note_storage"
    auth_secret_key: str = DEFAULT_AUTH_SECRET_KEY
    auth_token_ttl_minutes: int = 60 * 12
    bootstrap_admin_token: str = ""
    mcp_actor_user_id: UUID = UUID("00000000-0000-0000-0000-000000000000")
    mcp_actor_role: str = "viewer"
    mcp_enable_writes: bool = False
    mcp_expose_legacy_tools: bool = False

    @model_validator(mode="after")
    def _validate_auth_secret_key(self) -> Settings:
        if (
            self.environment.strip().lower() != "local"
            and self.auth_secret_key == DEFAULT_AUTH_SECRET_KEY
        ):
            raise ValueError(
                "LAB_TRACKER_AUTH_SECRET_KEY must be set when "
                "LAB_TRACKER_ENVIRONMENT is not 'local'."
            )
        return self

    model_config = SettingsConfigDict(
        env_prefix="LAB_TRACKER_",
        env_file=".env",
        case_sensitive=False,
    )


def get_settings() -> Settings:
    return Settings()
