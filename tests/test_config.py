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


def test_local_environment_can_enable_auth(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    monkeypatch.setenv("LAB_TRACKER_AUTH_ENABLED", "true")
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


def test_non_local_environment_rejects_default_auth_secret(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "production")
    with pytest.raises(ValidationError, match="LAB_TRACKER_AUTH_SECRET_KEY must be set"):
        _settings_from_environment()


def test_non_local_environment_accepts_custom_auth_secret(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "production")
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY", "custom-secret")
    settings = _settings_from_environment()
    assert settings.auth_secret_key == "custom-secret"
    assert settings.is_auth_enabled() is True


def test_non_local_environment_rejects_disabled_auth(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "production")
    monkeypatch.setenv("LAB_TRACKER_AUTH_ENABLED", "false")
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY", "custom-secret")
    with pytest.raises(ValidationError, match="LAB_TRACKER_AUTH_ENABLED=false"):
        _settings_from_environment()
