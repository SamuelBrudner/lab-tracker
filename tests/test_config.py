from __future__ import annotations

import pytest
from pydantic import ValidationError

from lab_tracker.config import DEFAULT_AUTH_SECRET_KEY, Settings


def _settings_from_environment() -> Settings:
    return Settings(_env_file=None)


def _clear_auth_env(monkeypatch) -> None:
    monkeypatch.delenv("LAB_TRACKER_AUTH_SECRET_KEY", raising=False)
    monkeypatch.delenv("LAB_TRACKER_ENVIRONMENT", raising=False)
    monkeypatch.delenv("LAB_TRACKER_AUTH_ENABLED", raising=False)


def test_local_environment_allows_default_auth_secret(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    settings = _settings_from_environment()
    assert settings.auth_secret_key == DEFAULT_AUTH_SECRET_KEY
    assert settings.is_auth_enabled() is False
    assert settings.is_usage_events_enabled() is False


def test_local_environment_rejects_default_auth_secret_when_auth_enabled(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    monkeypatch.setenv("LAB_TRACKER_AUTH_ENABLED", "true")
    with pytest.raises(ValidationError, match="strong non-placeholder"):
        _settings_from_environment()


def test_local_environment_can_enable_auth_with_custom_secret(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    monkeypatch.setenv("LAB_TRACKER_AUTH_ENABLED", "true")
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY", "custom-local-secret")
    settings = _settings_from_environment()
    assert settings.is_auth_enabled() is True


def test_dotenv_ignores_non_lab_tracker_keys(tmp_path, monkeypatch):
    _clear_auth_env(monkeypatch)
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "POSTGRES_DB=lab_tracker\n"
        "LAB_TRACKER_ENVIRONMENT=local\n"
        "LAB_TRACKER_OPENAI_MODEL=gpt-test\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=dotenv_path)

    assert settings.environment == "local"
    assert settings.openai_model == "gpt-test"


def test_default_openai_model_is_standard_account_model(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    settings = _settings_from_environment()
    assert settings.openai_model == "gpt-4o-mini"


def test_managed_postgres_urls_use_installed_psycopg_driver(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    monkeypatch.setenv(
        "LAB_TRACKER_DATABASE_URL",
        "postgres://lab_tracker:secret@db.example.org/lab_tracker",
    )
    settings = _settings_from_environment()
    assert settings.database_url == (
        "postgresql+psycopg://lab_tracker:secret@db.example.org/lab_tracker"
    )

    monkeypatch.setenv(
        "LAB_TRACKER_DATABASE_URL",
        "postgresql://lab_tracker:secret@db.example.org/lab_tracker",
    )
    settings = _settings_from_environment()
    assert settings.database_url == (
        "postgresql+psycopg://lab_tracker:secret@db.example.org/lab_tracker"
    )


def test_non_local_environment_rejects_default_auth_secret(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "production")
    with pytest.raises(ValidationError, match="strong non-placeholder"):
        _settings_from_environment()


def test_non_local_environment_rejects_placeholder_auth_secret(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "production")
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY", "replace-with-a-strong-secret")
    with pytest.raises(ValidationError, match="strong non-placeholder"):
        _settings_from_environment()


def test_non_local_environment_rejects_example_random_secret_placeholder(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "LAB_TRACKER_AUTH_SECRET_KEY",
        "replace-with-a-strong-random-secret",
    )
    with pytest.raises(ValidationError, match="strong non-placeholder"):
        _settings_from_environment()


def test_non_local_environment_accepts_custom_auth_secret(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "production")
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY", "custom-secret")
    settings = _settings_from_environment()
    assert settings.auth_secret_key == "custom-secret"
    assert settings.is_auth_enabled() is True
    assert settings.is_usage_events_enabled() is True


def test_usage_events_flag_overrides_environment(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    monkeypatch.setenv("LAB_TRACKER_USAGE_EVENTS", "true")
    settings = _settings_from_environment()
    assert settings.is_usage_events_enabled() is True

    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "production")
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY", "custom-secret")
    monkeypatch.setenv("LAB_TRACKER_USAGE_EVENTS", "false")
    settings = _settings_from_environment()
    assert settings.is_usage_events_enabled() is False


def test_non_local_environment_rejects_disabled_auth(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "production")
    monkeypatch.setenv("LAB_TRACKER_AUTH_ENABLED", "false")
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY", "custom-secret")
    with pytest.raises(ValidationError, match="LAB_TRACKER_AUTH_ENABLED=false"):
        _settings_from_environment()


def test_agentic_max_tool_calls_must_be_positive(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    monkeypatch.setenv("LAB_TRACKER_GRAPH_DRAFT_AGENTIC_MAX_TOOL_CALLS", "0")
    with pytest.raises(ValidationError, match="GRAPH_DRAFT_AGENTIC_MAX_TOOL_CALLS"):
        _settings_from_environment()


def test_external_harness_config_validates_selector_profiles_and_bounds(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    monkeypatch.setenv("LAB_TRACKER_GRAPH_DRAFT_EXTERNAL_HARNESS", "claude_code")
    monkeypatch.setenv(
        "LAB_TRACKER_GRAPH_DRAFT_EXTERNAL_HARNESS_SANDBOX_PROFILE",
        "operator_managed",
    )
    monkeypatch.setenv(
        "LAB_TRACKER_GRAPH_DRAFT_EXTERNAL_HARNESS_EGRESS_PROFILE",
        "vendor_api_only",
    )
    settings = _settings_from_environment()
    assert settings.graph_draft_external_harness == "claude_code"
    assert settings.graph_draft_external_harness_sandbox_profile == "operator_managed"
    assert settings.graph_draft_external_harness_egress_profile == "vendor_api_only"

    monkeypatch.setenv("LAB_TRACKER_GRAPH_DRAFT_EXTERNAL_HARNESS", "robot")
    with pytest.raises(ValidationError, match="GRAPH_DRAFT_EXTERNAL_HARNESS"):
        _settings_from_environment()

    monkeypatch.setenv("LAB_TRACKER_GRAPH_DRAFT_EXTERNAL_HARNESS", "codex")
    monkeypatch.setenv("LAB_TRACKER_GRAPH_DRAFT_EXTERNAL_HARNESS_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValidationError, match="EXTERNAL_HARNESS_TIMEOUT_SECONDS"):
        _settings_from_environment()

    monkeypatch.setenv("LAB_TRACKER_GRAPH_DRAFT_EXTERNAL_HARNESS_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv(
        "LAB_TRACKER_GRAPH_DRAFT_EXTERNAL_HARNESS_MAX_SPAWNS_PER_TICK",
        "0",
    )
    with pytest.raises(ValidationError, match="EXTERNAL_HARNESS_MAX_SPAWNS_PER_TICK"):
        _settings_from_environment()
