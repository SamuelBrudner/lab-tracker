from __future__ import annotations

import pytest
from pydantic import ValidationError

from lab_tracker.config import DEFAULT_AUTH_SECRET_KEY, Settings


def _clear_auth_env(monkeypatch) -> None:
    monkeypatch.delenv("LAB_TRACKER_AUTH_SECRET_KEY", raising=False)
    monkeypatch.delenv("LAB_TRACKER_ENVIRONMENT", raising=False)


def test_local_environment_allows_default_auth_secret(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    settings = Settings()
    assert settings.auth_secret_key == DEFAULT_AUTH_SECRET_KEY


def test_non_local_environment_rejects_default_auth_secret(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "production")
    with pytest.raises(ValidationError, match="LAB_TRACKER_AUTH_SECRET_KEY must be set"):
        Settings()


def test_non_local_environment_accepts_custom_auth_secret(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "production")
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY", "custom-secret")
    settings = Settings()
    assert settings.auth_secret_key == "custom-secret"
