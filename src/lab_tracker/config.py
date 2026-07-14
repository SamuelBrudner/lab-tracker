"""Configuration management for lab tracker."""

from __future__ import annotations

from typing import Literal

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
    backup_path: str = "~/.lab-tracker/backups"
    backup_keep: int = 10
    file_storage_path: str = "./file_storage"
    note_storage_path: str = "./note_storage"
    auth_secret_key: str = DEFAULT_AUTH_SECRET_KEY
    auth_token_ttl_minutes: int = 60 * 12
    auth_invite_ttl_hours: int = 7 * 24
    auth_rate_limit_attempts: int = 10
    auth_rate_limit_window_seconds: int = 60
    auth_public_viewer_registration_enabled: bool = False
    usage_events: bool | None = None
    bootstrap_admin_token: str = ""
    bootstrap_admin_token_disclosure: Literal["local", "first_run", "never"] = "local"
    auth_enabled: bool | None = None
    max_upload_bytes: int = 100 * 1024 * 1024
    graph_draft_provider: str = "openai"
    graph_draft_background_enabled: bool = False
    graph_draft_scheduler_enabled: bool = False
    graph_draft_worker_poll_seconds: float = 5.0
    graph_draft_scheduler_interval_seconds: float = 60.0
    graph_draft_agentic_tool_loop_enabled: bool = False
    graph_draft_agentic_max_tool_calls: int = 8
    graph_draft_agentic_base_provider: str = "openai"
    graph_draft_agentic_sensitivity_policy: Literal["redact", "omit", "allow"] = "redact"
    # Evidence-quote grounding for batch note dispositions (lab-tracker-hymd.4):
    # "warn" stamps attestation_verified on persisted ledger entries; "enforce"
    # additionally rejects drafts whose quotes are not grounded in the note's
    # delivered text. Flip to enforce only after observing real-traffic
    # false-negative (paraphrase/whitespace) rates.
    graph_draft_evidence_grounding: Literal["warn", "enforce"] = "warn"
    graph_draft_external_harness_enabled: bool = False
    graph_draft_external_harness: str = "codex"
    graph_draft_external_harness_command: str = ""
    graph_draft_external_harness_sandbox_profile: str = "disabled"
    graph_draft_external_harness_egress_profile: str = "disabled"
    graph_draft_external_harness_timeout_seconds: float = 120.0
    graph_draft_external_harness_max_stdout_bytes: int = 64 * 1024
    graph_draft_external_harness_max_spawns_per_tick: int = 1
    graph_draft_github_read_enabled: bool = False
    graph_draft_github_token: str = ""
    graph_draft_github_max_file_bytes: int = 256 * 1024
    graph_draft_github_timeout_seconds: float = 20.0
    review_link_ttl_hours: int = 72
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
        if self.graph_draft_worker_poll_seconds <= 0:
            raise ValueError(
                "LAB_TRACKER_GRAPH_DRAFT_WORKER_POLL_SECONDS must be greater than 0."
            )
        if self.graph_draft_scheduler_interval_seconds <= 0:
            raise ValueError(
                "LAB_TRACKER_GRAPH_DRAFT_SCHEDULER_INTERVAL_SECONDS must be greater than 0."
            )
        if self.graph_draft_agentic_max_tool_calls < 1:
            raise ValueError(
                "LAB_TRACKER_GRAPH_DRAFT_AGENTIC_MAX_TOOL_CALLS must be at least 1."
            )
        self.graph_draft_agentic_base_provider = (
            self.graph_draft_agentic_base_provider.strip().lower()
        )
        self.graph_draft_external_harness = (
            self.graph_draft_external_harness.strip().lower() or "codex"
        )
        if self.graph_draft_external_harness not in {"claude_code", "codex", "gemini"}:
            raise ValueError(
                "LAB_TRACKER_GRAPH_DRAFT_EXTERNAL_HARNESS must be one of "
                "claude_code, codex, or gemini."
            )
        self.graph_draft_external_harness_sandbox_profile = (
            self.graph_draft_external_harness_sandbox_profile.strip().lower()
            or "disabled"
        )
        if self.graph_draft_external_harness_sandbox_profile not in {
            "disabled",
            "operator_managed",
        }:
            raise ValueError(
                "LAB_TRACKER_GRAPH_DRAFT_EXTERNAL_HARNESS_SANDBOX_PROFILE must be "
                "disabled or operator_managed."
            )
        self.graph_draft_external_harness_egress_profile = (
            self.graph_draft_external_harness_egress_profile.strip().lower()
            or "disabled"
        )
        if self.graph_draft_external_harness_egress_profile not in {
            "disabled",
            "vendor_api_only",
        }:
            raise ValueError(
                "LAB_TRACKER_GRAPH_DRAFT_EXTERNAL_HARNESS_EGRESS_PROFILE must be "
                "disabled or vendor_api_only."
            )
        if self.graph_draft_external_harness_timeout_seconds <= 0:
            raise ValueError(
                "LAB_TRACKER_GRAPH_DRAFT_EXTERNAL_HARNESS_TIMEOUT_SECONDS must be "
                "greater than 0."
            )
        if self.graph_draft_external_harness_max_stdout_bytes < 1:
            raise ValueError(
                "LAB_TRACKER_GRAPH_DRAFT_EXTERNAL_HARNESS_MAX_STDOUT_BYTES must be "
                "at least 1."
            )
        if self.graph_draft_external_harness_max_spawns_per_tick < 1:
            raise ValueError(
                "LAB_TRACKER_GRAPH_DRAFT_EXTERNAL_HARNESS_MAX_SPAWNS_PER_TICK must be "
                "at least 1."
            )
        self.graph_draft_github_token = self.graph_draft_github_token.strip()
        if self.graph_draft_github_max_file_bytes < 1:
            raise ValueError(
                "LAB_TRACKER_GRAPH_DRAFT_GITHUB_MAX_FILE_BYTES must be at least 1."
            )
        if self.graph_draft_github_timeout_seconds <= 0:
            raise ValueError(
                "LAB_TRACKER_GRAPH_DRAFT_GITHUB_TIMEOUT_SECONDS must be greater than 0."
            )
        if self.review_link_ttl_hours < 1:
            raise ValueError("LAB_TRACKER_REVIEW_LINK_TTL_HOURS must be at least 1.")
        return self

    model_config = SettingsConfigDict(
        env_prefix="LAB_TRACKER_",
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )


def get_settings() -> Settings:
    return Settings()
