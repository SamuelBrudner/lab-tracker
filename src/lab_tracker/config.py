"""Configuration management for lab tracker."""

from __future__ import annotations

import math
from typing import Literal
from urllib.parse import urlsplit

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from lab_tracker.artifact_resolution_admission import (
    DEFAULT_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT,
    DEFAULT_ARTIFACT_RESOLUTION_PER_ACTOR_IN_FLIGHT_LIMIT,
    MAX_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT,
)
from lab_tracker.bounded_subprocess import MAX_PROCESS_DEADLINE_SECONDS
from lab_tracker.local_resolution_budget import (
    DEFAULT_LOCAL_RECOVERY_MAX_DIRECTORIES,
    DEFAULT_LOCAL_RECOVERY_MAX_FILES,
    DEFAULT_LOCAL_RESOLUTION_MAX_READ_BYTES,
    MAX_LOCAL_RECOVERY_MAX_DIRECTORIES,
    MAX_LOCAL_RECOVERY_MAX_FILES,
    MAX_LOCAL_RESOLUTION_MAX_READ_BYTES,
)
from lab_tracker.outbound_http import MAX_OUTBOUND_HTTP_DEADLINE_SECONDS
from lab_tracker.store_health import (
    DEFAULT_STORE_HEALTH_CACHE_MAX_ENTRIES,
    DEFAULT_STORE_HEALTH_CACHE_TTL_SECONDS,
    DEFAULT_STORE_HEALTH_SINGLEFLIGHT_WAIT_SECONDS,
    MAX_STORE_HEALTH_CACHE_MAX_ENTRIES,
    MAX_STORE_HEALTH_CACHE_TTL_SECONDS,
    MAX_STORE_HEALTH_SINGLEFLIGHT_WAIT_SECONDS,
)
from lab_tracker.store_health_admission import (
    DEFAULT_STORE_HEALTH_GLOBAL_IN_FLIGHT_LIMIT,
    DEFAULT_STORE_HEALTH_PER_ACTOR_IN_FLIGHT_LIMIT,
    MAX_STORE_HEALTH_GLOBAL_IN_FLIGHT_LIMIT,
)

DEFAULT_AUTH_SECRET_KEY = "dev-only-change-me"
MAX_COMBINED_HOST_IO_IN_FLIGHT_LIMIT = 32
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
    resolver_allowed_roots: str = ""
    resolver_http_allowed_authorities: str = ""
    resolver_http_allowed_networks: str = ""
    resolver_http_deadline_seconds: float = 30.0
    resolver_subprocess_deadline_seconds: float = 30.0
    resolver_recovery: bool = False
    resolver_recovery_max_files: int = DEFAULT_LOCAL_RECOVERY_MAX_FILES
    resolver_recovery_max_directories: int = DEFAULT_LOCAL_RECOVERY_MAX_DIRECTORIES
    resolver_recovery_max_bytes: int = DEFAULT_LOCAL_RESOLUTION_MAX_READ_BYTES
    artifact_resolution_global_in_flight_limit: int = (
        DEFAULT_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT
    )
    artifact_resolution_per_actor_in_flight_limit: int = (
        DEFAULT_ARTIFACT_RESOLUTION_PER_ACTOR_IN_FLIGHT_LIMIT
    )
    store_health_global_in_flight_limit: int = DEFAULT_STORE_HEALTH_GLOBAL_IN_FLIGHT_LIMIT
    store_health_per_actor_in_flight_limit: int = DEFAULT_STORE_HEALTH_PER_ACTOR_IN_FLIGHT_LIMIT
    store_health_cache_max_entries: int = DEFAULT_STORE_HEALTH_CACHE_MAX_ENTRIES
    store_health_cache_ttl_seconds: float = DEFAULT_STORE_HEALTH_CACHE_TTL_SECONDS
    store_health_singleflight_wait_seconds: float = DEFAULT_STORE_HEALTH_SINGLEFLIGHT_WAIT_SECONDS
    rclone_allowed_remotes: str = ""
    git_allowed_remotes: str = ""
    graph_draft_provider: str = "openai"
    graph_draft_background_enabled: bool = False
    graph_draft_scheduler_enabled: bool = False
    graph_draft_worker_poll_seconds: float = 5.0
    graph_draft_scheduler_interval_seconds: float = 60.0
    public_base_url: str = ""
    canonical_base_url: str = ""
    review_email_enabled: bool = False
    review_email_transport: Literal["external", "smtp"] = "external"
    review_email_worker_poll_seconds: float = 10.0
    review_email_claim_lease_seconds: int = 300
    review_email_max_attempts: int = 8
    review_email_link_ttl_minutes: int = 24 * 60
    review_email_smtp_host: str = ""
    review_email_smtp_port: int = 587
    review_email_smtp_username: str = ""
    review_email_smtp_password: str = ""
    review_email_smtp_from_address: str = ""
    review_email_smtp_tls_mode: Literal["none", "starttls", "implicit"] = "starttls"
    review_email_smtp_timeout_seconds: float = 10.0
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] | None = None
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

    @field_validator(
        "resolver_http_deadline_seconds",
        "resolver_subprocess_deadline_seconds",
        mode="before",
    )
    @classmethod
    def _reject_boolean_resolver_deadline(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("Resolver deadline settings do not accept booleans.")
        return value

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

    @field_validator("resolver_recovery", mode="before")
    @classmethod
    def _validate_resolver_recovery(cls, value: object) -> bool:
        if type(value) is bool:
            return value
        if type(value) is str:
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off", ""}:
                return False
        raise ValueError("LAB_TRACKER_RESOLVER_RECOVERY must be a boolean value.")

    @field_validator(
        "resolver_recovery_max_files",
        "resolver_recovery_max_directories",
        "resolver_recovery_max_bytes",
        mode="before",
    )
    @classmethod
    def _validate_resolver_recovery_integer_input(cls, value: object) -> int:
        if type(value) is int:
            return value
        if (
            type(value) is str
            and value.strip()
            and value.strip().isascii()
            and value.strip().isdecimal()
        ):
            return int(value.strip())
        raise ValueError(
            "Local resolver recovery limits require positive integers and do not accept booleans."
        )

    @field_validator("resolver_recovery_max_files")
    @classmethod
    def _validate_resolver_recovery_max_files(cls, value: int) -> int:
        if not 1 <= value <= MAX_LOCAL_RECOVERY_MAX_FILES:
            raise ValueError(
                "LAB_TRACKER_RESOLVER_RECOVERY_MAX_FILES must be between 1 and "
                f"{MAX_LOCAL_RECOVERY_MAX_FILES}."
            )
        return value

    @field_validator("resolver_recovery_max_directories")
    @classmethod
    def _validate_resolver_recovery_max_directories(cls, value: int) -> int:
        if not 1 <= value <= MAX_LOCAL_RECOVERY_MAX_DIRECTORIES:
            raise ValueError(
                "LAB_TRACKER_RESOLVER_RECOVERY_MAX_DIRECTORIES must be between 1 and "
                f"{MAX_LOCAL_RECOVERY_MAX_DIRECTORIES}."
            )
        return value

    @field_validator("resolver_recovery_max_bytes")
    @classmethod
    def _validate_resolver_recovery_max_bytes(cls, value: int) -> int:
        if not 1 <= value <= MAX_LOCAL_RESOLUTION_MAX_READ_BYTES:
            raise ValueError(
                "LAB_TRACKER_RESOLVER_RECOVERY_MAX_BYTES must be between 1 and "
                f"{MAX_LOCAL_RESOLUTION_MAX_READ_BYTES}."
            )
        return value

    @field_validator(
        "store_health_global_in_flight_limit",
        "store_health_per_actor_in_flight_limit",
        "store_health_cache_max_entries",
        mode="before",
    )
    @classmethod
    def _reject_boolean_store_health_integer(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError(
                "Store-health integer settings require integers and do not accept booleans."
            )
        return value

    @field_validator(
        "store_health_cache_ttl_seconds",
        "store_health_singleflight_wait_seconds",
        mode="before",
    )
    @classmethod
    def _reject_boolean_store_health_duration(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("Store-health duration settings do not accept booleans.")
        return value

    @field_validator("store_health_cache_ttl_seconds")
    @classmethod
    def _validate_store_health_cache_ttl_seconds(cls, value: float) -> float:
        return _validate_store_health_duration_seconds(
            value,
            variable="LAB_TRACKER_STORE_HEALTH_CACHE_TTL_SECONDS",
            maximum=MAX_STORE_HEALTH_CACHE_TTL_SECONDS,
        )

    @field_validator("store_health_singleflight_wait_seconds")
    @classmethod
    def _validate_store_health_singleflight_wait_seconds(
        cls,
        value: float,
    ) -> float:
        return _validate_store_health_duration_seconds(
            value,
            variable="LAB_TRACKER_STORE_HEALTH_SINGLEFLIGHT_WAIT_SECONDS",
            maximum=MAX_STORE_HEALTH_SINGLEFLIGHT_WAIT_SECONDS,
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
            raise ValueError("LAB_TRACKER_AUTH_RATE_LIMIT_WINDOW_SECONDS must be at least 1.")
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
                "LAB_TRACKER_ARTIFACT_RESOLUTION_PER_ACTOR_IN_FLIGHT_LIMIT must be at least 1."
            )
        if (
            self.artifact_resolution_per_actor_in_flight_limit
            > self.artifact_resolution_global_in_flight_limit
        ):
            raise ValueError(
                "LAB_TRACKER_ARTIFACT_RESOLUTION_PER_ACTOR_IN_FLIGHT_LIMIT must be "
                "no greater than LAB_TRACKER_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT."
            )
        if self.store_health_global_in_flight_limit < 1:
            raise ValueError("LAB_TRACKER_STORE_HEALTH_GLOBAL_IN_FLIGHT_LIMIT must be at least 1.")
        if self.store_health_global_in_flight_limit > MAX_STORE_HEALTH_GLOBAL_IN_FLIGHT_LIMIT:
            raise ValueError(
                "LAB_TRACKER_STORE_HEALTH_GLOBAL_IN_FLIGHT_LIMIT must be no greater "
                f"than {MAX_STORE_HEALTH_GLOBAL_IN_FLIGHT_LIMIT}."
            )
        if self.store_health_per_actor_in_flight_limit < 1:
            raise ValueError(
                "LAB_TRACKER_STORE_HEALTH_PER_ACTOR_IN_FLIGHT_LIMIT must be at least 1."
            )
        if self.store_health_per_actor_in_flight_limit > self.store_health_global_in_flight_limit:
            raise ValueError(
                "LAB_TRACKER_STORE_HEALTH_PER_ACTOR_IN_FLIGHT_LIMIT must be no greater "
                "than LAB_TRACKER_STORE_HEALTH_GLOBAL_IN_FLIGHT_LIMIT."
            )
        if self.store_health_cache_max_entries < 1:
            raise ValueError("LAB_TRACKER_STORE_HEALTH_CACHE_MAX_ENTRIES must be at least 1.")
        if self.store_health_cache_max_entries > MAX_STORE_HEALTH_CACHE_MAX_ENTRIES:
            raise ValueError(
                "LAB_TRACKER_STORE_HEALTH_CACHE_MAX_ENTRIES must be no greater than "
                f"{MAX_STORE_HEALTH_CACHE_MAX_ENTRIES}."
            )
        if (
            self.artifact_resolution_global_in_flight_limit
            + self.store_health_global_in_flight_limit
            > MAX_COMBINED_HOST_IO_IN_FLIGHT_LIMIT
        ):
            raise ValueError(
                "LAB_TRACKER_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT plus "
                "LAB_TRACKER_STORE_HEALTH_GLOBAL_IN_FLIGHT_LIMIT must be no greater "
                f"than {MAX_COMBINED_HOST_IO_IN_FLIGHT_LIMIT}."
            )
        if self.graph_draft_worker_poll_seconds <= 0:
            raise ValueError("LAB_TRACKER_GRAPH_DRAFT_WORKER_POLL_SECONDS must be greater than 0.")
        if self.graph_draft_scheduler_interval_seconds <= 0:
            raise ValueError(
                "LAB_TRACKER_GRAPH_DRAFT_SCHEDULER_INTERVAL_SECONDS must be greater than 0."
            )
        if self.review_email_worker_poll_seconds <= 0:
            raise ValueError("LAB_TRACKER_REVIEW_EMAIL_WORKER_POLL_SECONDS must be greater than 0.")
        if self.review_email_claim_lease_seconds < 1:
            raise ValueError("LAB_TRACKER_REVIEW_EMAIL_CLAIM_LEASE_SECONDS must be at least 1.")
        if self.review_email_max_attempts < 1:
            raise ValueError("LAB_TRACKER_REVIEW_EMAIL_MAX_ATTEMPTS must be at least 1.")
        if self.review_email_link_ttl_minutes < 1:
            raise ValueError("LAB_TRACKER_REVIEW_EMAIL_LINK_TTL_MINUTES must be at least 1.")
        if not 0 < self.review_email_smtp_timeout_seconds <= 30:
            raise ValueError(
                "LAB_TRACKER_REVIEW_EMAIL_SMTP_TIMEOUT_SECONDS must be greater "
                "than 0 and no more than 30."
            )
        if self.review_email_enabled:
            public_base_url = self.public_base_url.strip().rstrip("/")
            self.public_base_url = public_base_url
            if not self.is_auth_enabled():
                raise ValueError("LAB_TRACKER_REVIEW_EMAIL_ENABLED requires authentication.")
            try:
                parsed_public_base_url = urlsplit(public_base_url)
                # Accessing ``port`` validates malformed and out-of-range ports.
                _ = parsed_public_base_url.port
            except ValueError:
                parsed_public_base_url = None
            if (
                parsed_public_base_url is None
                or parsed_public_base_url.scheme.lower() != "https"
                or not parsed_public_base_url.hostname
                or parsed_public_base_url.username is not None
                or parsed_public_base_url.password is not None
                or bool(parsed_public_base_url.query)
                or bool(parsed_public_base_url.fragment)
            ):
                raise ValueError(
                    "LAB_TRACKER_REVIEW_EMAIL_ENABLED requires an HTTPS "
                    "LAB_TRACKER_PUBLIC_BASE_URL with a hostname and no "
                    "credentials, query, or fragment."
                )
            if self.review_email_transport == "smtp":
                if not self.review_email_smtp_host.strip():
                    raise ValueError("LAB_TRACKER_REVIEW_EMAIL_SMTP_HOST is required for SMTP.")
                if not self.review_email_smtp_from_address.strip():
                    raise ValueError(
                        "LAB_TRACKER_REVIEW_EMAIL_SMTP_FROM_ADDRESS is required for SMTP."
                    )
                if not 1 <= self.review_email_smtp_port <= 65535:
                    raise ValueError(
                        "LAB_TRACKER_REVIEW_EMAIL_SMTP_PORT must be between 1 and 65535."
                    )
                username_configured = bool(self.review_email_smtp_username.strip())
                password_configured = bool(self.review_email_smtp_password)
                if username_configured != password_configured:
                    raise ValueError(
                        "LAB_TRACKER_REVIEW_EMAIL_SMTP_USERNAME and "
                        "LAB_TRACKER_REVIEW_EMAIL_SMTP_PASSWORD must be configured together."
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
    if not math.isfinite(value) or value <= 0 or value > maximum:
        raise ValueError(
            f"{variable} must be finite and greater than 0, and no greater than {maximum:g}."
        )
    return value


def _validate_store_health_duration_seconds(
    value: float,
    *,
    variable: str,
    maximum: float,
) -> float:
    if not math.isfinite(value) or value <= 0 or value > maximum:
        raise ValueError(
            f"{variable} must be finite and greater than 0, and no greater than {maximum:g}."
        )
    return value
