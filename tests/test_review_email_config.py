from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from lab_tracker.config import Settings

_AUTHENTICATED_EMAIL_SETTINGS: dict[str, Any] = {
    "environment": "local",
    "auth_enabled": True,
    "auth_secret_key": "review-email-config-test-secret",
    "review_email_enabled": True,
    "base_url": "https://lab-tracker.example.test",
}
_ENV_EXAMPLE_REVIEW_EMAIL_DEFAULTS = {
    "LAB_TRACKER_REVIEW_EMAIL_ENABLED": "false",
    "LAB_TRACKER_REVIEW_EMAIL_TRANSPORT": "external",
    "LAB_TRACKER_REVIEW_EMAIL_WORKER_POLL_SECONDS": "10",
    "LAB_TRACKER_REVIEW_EMAIL_CLAIM_LEASE_SECONDS": "300",
    "LAB_TRACKER_REVIEW_EMAIL_MAX_ATTEMPTS": "8",
    "LAB_TRACKER_REVIEW_EMAIL_LINK_TTL_MINUTES": "1440",
    "LAB_TRACKER_REVIEW_EMAIL_SMTP_HOST": "",
    "LAB_TRACKER_REVIEW_EMAIL_SMTP_PORT": "587",
    "LAB_TRACKER_REVIEW_EMAIL_SMTP_USERNAME": "",
    "LAB_TRACKER_REVIEW_EMAIL_SMTP_PASSWORD": "",
    "LAB_TRACKER_REVIEW_EMAIL_SMTP_FROM_ADDRESS": "",
    "LAB_TRACKER_REVIEW_EMAIL_SMTP_TLS_MODE": "starttls",
    "LAB_TRACKER_REVIEW_EMAIL_SMTP_TIMEOUT_SECONDS": "10",
}


@pytest.fixture(autouse=True)
def _clear_lab_tracker_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in list(os.environ):
        if variable.startswith("LAB_TRACKER_"):
            monkeypatch.delenv(variable, raising=False)


def _settings(**overrides: Any) -> Settings:
    values = {**_AUTHENTICATED_EMAIL_SETTINGS, **overrides}
    return Settings(_env_file=None, **values)


def test_env_example_lists_safe_review_email_defaults() -> None:
    env_example = Path(__file__).resolve().parent.parent / ".env.example"
    configured = dict(
        line.split("=", 1)
        for line in env_example.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    )

    assert {
        variable: configured.get(variable) for variable in _ENV_EXAMPLE_REVIEW_EMAIL_DEFAULTS
    } == _ENV_EXAMPLE_REVIEW_EMAIL_DEFAULTS


def test_review_email_defaults_are_safe_and_external() -> None:
    settings = Settings(_env_file=None)

    assert settings.review_email_enabled is False
    assert settings.review_email_transport == "external"
    assert settings.review_email_worker_poll_seconds == 10.0
    assert settings.review_email_claim_lease_seconds == 300
    assert settings.review_email_max_attempts == 8
    assert settings.review_email_link_ttl_minutes == 24 * 60
    assert settings.review_email_smtp_host == ""
    assert settings.review_email_smtp_port == 587
    assert settings.review_email_smtp_username == ""
    assert settings.review_email_smtp_password == ""
    assert settings.review_email_smtp_from_address == ""
    assert settings.review_email_smtp_tls_mode == "starttls"
    assert settings.review_email_smtp_timeout_seconds == 10.0


def test_external_transport_requires_no_smtp_configuration() -> None:
    settings = _settings(
        review_email_transport="external",
        base_url="  https://lab-tracker.example.test/app/  ",
    )

    assert settings.review_email_enabled is True
    assert settings.review_email_transport == "external"
    assert settings.base_url == "https://lab-tracker.example.test"


def test_review_email_requires_authentication() -> None:
    with pytest.raises(ValidationError, match="requires authentication"):
        _settings(auth_enabled=False)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://lab-tracker.example.test",
        "https://",
        "https:///app",
        "https://user@lab-tracker.example.test",
        "https://user:password@lab-tracker.example.test",
        "https://lab-tracker.example.test?next=review",
        "https://lab-tracker.example.test#review",
        "https://lab-tracker.example.test:not-a-port",
        "https://lab-tracker.example.test:70000",
    ],
)
def test_review_email_requires_safe_absolute_https_base_url(
    base_url: str,
) -> None:
    with pytest.raises(ValidationError, match="LAB_TRACKER_BASE_URL"):
        _settings(base_url=base_url)


def test_smtp_transport_accepts_required_fields_without_login() -> None:
    settings = _settings(
        review_email_transport="smtp",
        review_email_smtp_host="smtp.example.test",
        review_email_smtp_from_address="notifications@example.test",
        review_email_smtp_port=465,
        review_email_smtp_tls_mode="implicit",
        review_email_smtp_timeout_seconds=30,
    )

    assert settings.review_email_smtp_host == "smtp.example.test"
    assert settings.review_email_smtp_port == 465
    assert settings.review_email_smtp_tls_mode == "implicit"
    assert settings.review_email_smtp_timeout_seconds == 30


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        (
            {"review_email_smtp_from_address": "notifications@example.test"},
            "SMTP_HOST is required",
        ),
        (
            {"review_email_smtp_host": "smtp.example.test"},
            "SMTP_FROM_ADDRESS is required",
        ),
        (
            {
                "review_email_smtp_host": "smtp.example.test",
                "review_email_smtp_from_address": "notifications@example.test",
                "review_email_smtp_port": 0,
            },
            "SMTP_PORT must be between 1 and 65535",
        ),
        (
            {
                "review_email_smtp_host": "smtp.example.test",
                "review_email_smtp_from_address": "notifications@example.test",
                "review_email_smtp_port": 65536,
            },
            "SMTP_PORT must be between 1 and 65535",
        ),
    ],
)
def test_smtp_transport_requires_endpoint_and_sender(
    overrides: dict[str, Any],
    error: str,
) -> None:
    with pytest.raises(ValidationError, match=error):
        _settings(review_email_transport="smtp", **overrides)


@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("smtp-user", ""),
        ("", "smtp-password"),
    ],
)
def test_smtp_credentials_must_be_configured_as_a_pair(
    username: str,
    password: str,
) -> None:
    with pytest.raises(ValidationError, match="must be configured together"):
        _settings(
            review_email_transport="smtp",
            review_email_smtp_host="smtp.example.test",
            review_email_smtp_from_address="notifications@example.test",
            review_email_smtp_username=username,
            review_email_smtp_password=password,
        )


def test_smtp_transport_accepts_paired_credentials() -> None:
    settings = _settings(
        review_email_transport="smtp",
        review_email_smtp_host="smtp.example.test",
        review_email_smtp_from_address="notifications@example.test",
        review_email_smtp_username="smtp-user",
        review_email_smtp_password="smtp-password",
    )

    assert settings.review_email_smtp_username == "smtp-user"
    assert settings.review_email_smtp_password == "smtp-password"


@pytest.mark.parametrize("timeout_seconds", [0, -0.1, 30.01])
def test_smtp_timeout_is_bounded(timeout_seconds: float) -> None:
    with pytest.raises(ValidationError, match="SMTP_TIMEOUT_SECONDS"):
        _settings(review_email_smtp_timeout_seconds=timeout_seconds)


@pytest.mark.parametrize("timeout_seconds", [0.01, 30])
def test_smtp_timeout_accepts_boundary_values(timeout_seconds: float) -> None:
    settings = _settings(review_email_smtp_timeout_seconds=timeout_seconds)

    assert settings.review_email_smtp_timeout_seconds == timeout_seconds


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("review_email_worker_poll_seconds", 0, "WORKER_POLL_SECONDS"),
        ("review_email_claim_lease_seconds", 0, "CLAIM_LEASE_SECONDS"),
        ("review_email_max_attempts", 0, "MAX_ATTEMPTS"),
        ("review_email_link_ttl_minutes", 0, "LINK_TTL_MINUTES"),
    ],
)
def test_review_email_runtime_bounds_reject_nonpositive_values(
    field: str,
    value: int,
    error: str,
) -> None:
    with pytest.raises(ValidationError, match=error):
        _settings(**{field: value})


def test_review_email_lease_and_link_ttl_accept_minimum_values() -> None:
    settings = _settings(
        review_email_worker_poll_seconds=0.01,
        review_email_claim_lease_seconds=1,
        review_email_max_attempts=1,
        review_email_link_ttl_minutes=1,
    )

    assert settings.review_email_worker_poll_seconds == 0.01
    assert settings.review_email_claim_lease_seconds == 1
    assert settings.review_email_max_attempts == 1
    assert settings.review_email_link_ttl_minutes == 1
