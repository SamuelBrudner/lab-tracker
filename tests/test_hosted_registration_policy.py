"""Hosted deployment templates disable anonymous viewer self-registration (L52)."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_SETTING = "LAB_TRACKER_AUTH_PUBLIC_VIEWER_REGISTRATION_ENABLED"


def test_render_blueprint_disables_public_viewer_registration() -> None:
    blueprint = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")

    assert re.search(
        rf"^\s*- key: {_SETTING}\n\s+value: \"false\"$", blueprint, re.MULTILINE
    ), f"render.yaml must set {_SETTING} to \"false\""


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
