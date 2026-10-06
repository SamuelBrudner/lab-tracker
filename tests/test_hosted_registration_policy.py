"""Hosted deployment templates disable anonymous viewer self-registration (L52)
and never hand the first-admin token to unauthenticated callers."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from lab_tracker.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent
_SETTING = "LAB_TRACKER_AUTH_PUBLIC_VIEWER_REGISTRATION_ENABLED"
_DISCLOSURE = "LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN_DISCLOSURE"


def _blueprint_literal(blueprint: str, key: str) -> str | None:
    match = re.search(rf"^\s*- key: {key}\n\s+value: (\S+)$", blueprint, re.MULTILINE)
    return match.group(1).strip('"') if match else None


def test_render_blueprint_disables_public_viewer_registration() -> None:
    blueprint = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")

    assert re.search(rf"^\s*- key: {_SETTING}\n\s+value: \"false\"$", blueprint, re.MULTILINE), (
        f'render.yaml must set {_SETTING} to "false"'
    )


def test_shared_provider_runtime_env_disables_public_viewer_registration() -> None:
    runtime_env = REPO_ROOT / "deployments" / "shared-provider" / "runtime.env.example"
    configured = dict(
        line.split("=", 1)
        for line in runtime_env.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    )

    assert configured.get(_SETTING) == "false"


def test_dedicated_instance_disables_public_viewer_registration() -> None:
    compose = (REPO_ROOT / "deployments" / "dedicated-instance" / "docker-compose.yml").read_text(
        encoding="utf-8"
    )

    assert f'{_SETTING}: "false"' in compose


def test_root_env_example_disables_public_viewer_registration() -> None:
    """The root compose reads .env; its template must not re-enable signup."""

    env_example = REPO_ROOT / ".env.example"
    configured = dict(
        line.split("=", 1)
        for line in env_example.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    )

    assert configured.get(_SETTING) == "false"


def test_render_blueprint_never_discloses_the_first_admin_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The service URL is public as soon as the deploy finishes; first_run would
    # return the token to whoever opens it before the operator does.
    # Settings reads the environment case-insensitively.
    for key in [key for key in os.environ if key.upper().startswith("LAB_TRACKER_")]:
        monkeypatch.delenv(key)
    blueprint = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")

    assert _blueprint_literal(blueprint, _DISCLOSURE) == "never", (
        f"render.yaml must set {_DISCLOSURE} to never"
    )
    settings = Settings(
        _env_file=None,
        environment=_blueprint_literal(blueprint, "LAB_TRACKER_ENVIRONMENT"),
        auth_secret_key="strong-production-secret",
        bootstrap_admin_token_disclosure=_blueprint_literal(blueprint, _DISCLOSURE),
    )
    assert settings.effective_bootstrap_admin_token_disclosure() == "never"


def test_dedicated_instance_never_discloses_the_first_admin_token() -> None:
    compose = (REPO_ROOT / "deployments" / "dedicated-instance" / "docker-compose.yml").read_text(
        encoding="utf-8"
    )

    assert f"{_DISCLOSURE}: never" in compose
