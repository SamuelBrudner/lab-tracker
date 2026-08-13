from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
OVERLAY = REPO_ROOT / "deployments" / "shared-provider" / "docker-compose.yml"


def test_pinned_overlay_removes_builds_and_requires_one_release_image() -> None:
    compose = OVERLAY.read_text(encoding="utf-8")

    assert compose.count("image: ${LAB_TRACKER_RELEASE_IMAGE:?") == 2
    assert compose.count("build: !reset null") == 2
    assert "LAB_TRACKER_PROVIDER_ENV_FILE" in compose
    assert "LAB_TRACKER_RUNTIME_ENV_FILE" in compose
    assert compose.count("required: true") == 2
    assert 'socket.create_connection(("127.0.0.1", 8000)' in compose


def test_pinned_overlay_documentation_has_fail_closed_preflight() -> None:
    readme = (OVERLAY.parent / "README.md").read_text(encoding="utf-8")

    assert "COMPOSE_FILE=" in readme
    assert "docker compose config --format json" in readme
    assert "Both `build` values must be `null`" in readme
    assert "fails before replacing a container" in readme


def test_compose_merge_has_pinned_images_and_no_builds(tmp_path: Path) -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose is required for the merge regression test")

    provider_env = tmp_path / "provider.env"
    provider_env.write_text("LAB_TRACKER_OPENAI_API_KEY=test-only\n", encoding="utf-8")
    runtime_env = tmp_path / "runtime.env"
    runtime_env.write_text(
        "LAB_TRACKER_GRAPH_DRAFT_PROVIDER=openai\n", encoding="utf-8"
    )
    release_image = "lab-tracker-primary:sha-0123456789abcdef"
    environment = os.environ.copy()
    environment.update(
        {
            "LAB_TRACKER_PROVIDER_ENV_FILE": str(provider_env),
            "LAB_TRACKER_RUNTIME_ENV_FILE": str(runtime_env),
            "LAB_TRACKER_RELEASE_IMAGE": release_image,
            "LT_MCP_READONLY_TOKEN": "unused",
            "LT_MCP_INBOUND_TOKEN": "inbound-" + "a" * 32,
        }
    )
    result = subprocess.run(
        [
            docker,
            "compose",
            "-f",
            str(REPO_ROOT / "docker-compose.yml"),
            "-f",
            str(OVERLAY),
            "config",
            "--format",
            "json",
        ],
        cwd=REPO_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    services = json.loads(result.stdout)["services"]

    for service_name in ("app", "mcp"):
        assert services[service_name]["image"] == release_image
        assert "build" not in services[service_name]
