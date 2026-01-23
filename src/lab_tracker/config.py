"""Configuration management for lab tracker."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "lab-tracker"
    environment: str = "local"
    log_level: str = "INFO"
    database_url: str = "sqlite+pysqlite:///./lab_tracker.db"

    model_config = SettingsConfigDict(
        env_prefix="LAB_TRACKER_",
        env_file=".env",
        case_sensitive=False,
    )


def get_settings() -> Settings:
    return Settings()
