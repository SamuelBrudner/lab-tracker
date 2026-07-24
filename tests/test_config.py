from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from lab_tracker.app_parts import runtime as runtime_module
from lab_tracker.app_parts.runtime import build_app_runtime, configure_app_state
from lab_tracker.artifact_resolution import (
    ResolverRegistry,
    outbound_http_policy_from_config,
)
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


def test_dotenv_loads_outbound_http_policy_settings(tmp_path, monkeypatch):
    _clear_auth_env(monkeypatch)
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "LAB_TRACKER_ENVIRONMENT=local\n"
        "LAB_TRACKER_RESOLVER_HTTP_ALLOWED_AUTHORITIES=http://10.20.1.7\n"
        "LAB_TRACKER_RESOLVER_HTTP_ALLOWED_NETWORKS=10.20.0.0/16\n"
        "LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS=12.5\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=dotenv_path)
    policy = outbound_http_policy_from_config(
        allowed_authorities=settings.resolver_http_allowed_authorities,
        allowed_networks=settings.resolver_http_allowed_networks,
    )

    assert policy.authorize("http://10.20.1.7/artifact.bin").hostname == "10.20.1.7"
    assert settings.resolver_http_deadline_seconds == 12.5


def test_resolver_http_deadline_defaults_to_thirty_seconds(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.delenv("LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS", raising=False)

    assert _settings_from_environment().resolver_http_deadline_seconds == 30.0


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_resolver_http_deadline_must_be_finite_and_positive(monkeypatch, value):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS", value)

    with pytest.raises(ValidationError, match="must be finite and greater than 0"):
        _settings_from_environment()


def test_resolver_http_deadline_cannot_exceed_one_day(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS", "86400.0001")

    with pytest.raises(
        ValidationError,
        match="LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS",
    ):
        _settings_from_environment()


def test_invalid_dotenv_http_policy_fails_runtime_composition(tmp_path, monkeypatch):
    _clear_auth_env(monkeypatch)
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "LAB_TRACKER_ENVIRONMENT=local\n"
        "LAB_TRACKER_RESOLVER_HTTP_ALLOWED_AUTHORITIES=https://files.lab/path\n"
        "LAB_TRACKER_RESOLVER_HTTP_ALLOWED_NETWORKS=10.20.0.0/16\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=dotenv_path)

    with pytest.raises(ValueError, match="exact origins"):
        build_app_runtime(settings)


def test_runtime_installs_one_validated_http_policy_and_registry(monkeypatch):
    _clear_auth_env(monkeypatch)
    resolver_registry = ResolverRegistry()
    captured: dict[str, object] = {}

    def recording_registry_from_env(
        *,
        http_policy,
        http_deadline_seconds,
    ):
        captured["http_policy"] = http_policy
        captured["http_deadline_seconds"] = http_deadline_seconds
        return resolver_registry

    monkeypatch.setattr(
        runtime_module,
        "registry_from_env",
        recording_registry_from_env,
    )
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        resolver_http_allowed_authorities="http://10.20.1.7",
        resolver_http_allowed_networks="10.20.0.0/16",
        resolver_http_deadline_seconds=12.5,
    )
    runtime = build_app_runtime(settings)
    app = FastAPI()
    try:
        configure_app_state(app, runtime)

        assert app.state.outbound_http_policy is runtime.outbound_http_policy
        assert app.state.resolver_registry is resolver_registry
        assert captured == {
            "http_policy": runtime.outbound_http_policy,
            "http_deadline_seconds": 12.5,
        }
        assert (
            runtime.outbound_http_policy.authorize(
                "http://10.20.1.7/artifact.bin"
            ).hostname
            == "10.20.1.7"
        )
    finally:
        runtime.engine.dispose()


def test_compose_forwards_outbound_http_policy_settings():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    assert (
        "LAB_TRACKER_RESOLVER_HTTP_ALLOWED_AUTHORITIES: "
        "${LAB_TRACKER_RESOLVER_HTTP_ALLOWED_AUTHORITIES:-}"
    ) in compose
    assert (
        "LAB_TRACKER_RESOLVER_HTTP_ALLOWED_NETWORKS: "
        "${LAB_TRACKER_RESOLVER_HTTP_ALLOWED_NETWORKS:-}"
    ) in compose
    assert (
        "LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS: "
        "${LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS:-30}"
    ) in compose


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
