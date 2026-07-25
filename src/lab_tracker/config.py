"""Configuration management for lab tracker."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from lab_tracker.artifact_resolution_admission import (
    DEFAULT_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT,
    DEFAULT_ARTIFACT_RESOLUTION_PER_ACTOR_IN_FLIGHT_LIMIT,
    MAX_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT,
)
from lab_tracker.bounded_subprocess import MAX_PROCESS_DEADLINE_SECONDS
from lab_tracker.outbound_http import MAX_OUTBOUND_HTTP_DEADLINE_SECONDS

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
    backup_path: str = "~/.lab-tracker/backups"
    backup_keep: int = 10
    file_storage_path: str = "./file_storage"
    note_storage_path: str = "./note_storage"
    auth_secret_key: str = DEFAULT_AUTH_SECRET_KEY
    auth_token_ttl_minutes: int = 60 * 12
    auth_invite_ttl_hours: int = 7 * 24
    auth_rate_limit_attempts: int = 10
    auth_rate_limit_window_seconds: int = 60
    auth_public_viewer_registration_enabled: bool = True
    usage_events: bool | None = None
    bootstrap_admin_token: str = ""
    bootstrap_admin_token_disclosure: Literal["local", "first_run", "never"] = "local"
    auth_enabled: bool | None = None
    max_upload_bytes: int = 100 * 1024 * 1024
    resolver_http_allowed_authorities: str = ""
    resolver_http_allowed_networks: str = ""
    resolver_http_deadline_seconds: float = 30.0
    resolver_subprocess_deadline_seconds: float = 30.0
    artifact_resolution_global_in_flight_limit: int = (
        DEFAULT_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT
    )
    artifact_resolution_per_actor_in_flight_limit: int = (
        DEFAULT_ARTIFACT_RESOLUTION_PER_ACTOR_IN_FLIGHT_LIMIT
    )
    git_allowed_remotes: str = ""
    graph_draft_provider: str = "openai"
    graph_draft_background_enabled: bool = False
    graph_draft_scheduler_enabled: bool = False
    graph_draft_worker_poll_seconds: float = 5.0
    graph_draft_scheduler_interval_seconds: float = 60.0
    public_base_url: str = ""
    canonical_base_url: str = ""
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_reasoning_effort: Literal[
        "none", "low", "medium", "high", "xhigh", "max"
    ] | None = None
    openai_reasoning_mode: Literal["standard", "pro"] | None = None
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

    def is_usage_events_enabled(self) -> bool:
        if self.usage_events is not None:
            return self.usage_events
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

    @field_validator("resolver_http_deadline_seconds")
    @classmethod
    def _validate_resolver_http_deadline_seconds(cls, value: float) -> float:
        return _validate_resolver_deadline_seconds(
            value,
            variable="LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS",
            maximum=MAX_OUTBOUND_HTTP_DEADLINE_SECONDS,
        )

    @field_validator("resolver_subprocess_deadline_seconds")
    @classmethod
    def _validate_resolver_subprocess_deadline_seconds(cls, value: float) -> float:
        return _validate_resolver_deadline_seconds(
            value,
            variable="LAB_TRACKER_RESOLVER_SUBPROCESS_DEADLINE_SECONDS",
            maximum=MAX_PROCESS_DEADLINE_SECONDS,
        )

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
        if self.max_upload_bytes < 1:
            raise ValueError("LAB_TRACKER_MAX_UPLOAD_BYTES must be at least 1.")
        if self.backup_keep < 1:
            raise ValueError("LAB_TRACKER_BACKUP_KEEP must be at least 1.")
        if self.auth_rate_limit_attempts < 1:
            raise ValueError("LAB_TRACKER_AUTH_RATE_LIMIT_ATTEMPTS must be at least 1.")
        if self.auth_rate_limit_window_seconds < 1:
            raise ValueError(
                "LAB_TRACKER_AUTH_RATE_LIMIT_WINDOW_SECONDS must be at least 1."
            )
        if self.artifact_resolution_global_in_flight_limit < 1:
            raise ValueError(
                "LAB_TRACKER_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT must be at least 1."
            )
        if (
            self.artifact_resolution_global_in_flight_limit
            > MAX_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT
        ):
            raise ValueError(
                "LAB_TRACKER_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT must be no "
                f"greater than {MAX_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT}."
            )
        if self.artifact_resolution_per_actor_in_flight_limit < 1:
            raise ValueError(
                "LAB_TRACKER_ARTIFACT_RESOLUTION_PER_ACTOR_IN_FLIGHT_LIMIT must be "
                "at least 1."
            )
        if (
            self.artifact_resolution_per_actor_in_flight_limit
            > self.artifact_resolution_global_in_flight_limit
        ):
            raise ValueError(
                "LAB_TRACKER_ARTIFACT_RESOLUTION_PER_ACTOR_IN_FLIGHT_LIMIT must be "
                "no greater than LAB_TRACKER_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT."
            )
        if self.graph_draft_worker_poll_seconds <= 0:
            raise ValueError(
                "LAB_TRACKER_GRAPH_DRAFT_WORKER_POLL_SECONDS must be greater than 0."
            )
        if self.graph_draft_scheduler_interval_seconds <= 0:
            raise ValueError(
                "LAB_TRACKER_GRAPH_DRAFT_SCHEDULER_INTERVAL_SECONDS must be greater than 0."
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


def _validate_resolver_deadline_seconds(
    value: float,
    *,
    variable: str,
    maximum: float,
) -> float:
    if (
        not math.isfinite(value)
        or value <= 0
        or value > maximum
    ):
        raise ValueError(
            f"{variable} must be finite and greater than 0, and no greater than "
            f"{maximum:g}."
        )
    return value
