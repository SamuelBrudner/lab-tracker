"""Regression tests for the repository-root ``docker-compose.yml``.

``docker compose config`` resolves the file without a Docker daemon, so these
tests exercise real Compose interpolation and profile selection whenever the
Compose CLI is installed.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT_COMPOSE = REPO_ROOT / "docker-compose.yml"
DEDICATED_COMPOSE = REPO_ROOT / "deployments" / "dedicated-instance" / "docker-compose.yml"

_MCP_TOKEN_VARIABLES = ("LT_MCP_INBOUND_TOKEN", "LT_MCP_READONLY_TOKEN")


def _docker() -> str:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose is required to resolve docker-compose.yml")
    return docker


def _compose_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("LAB_TRACKER_", "LT_MCP_", "COMPOSE_", "POSTGRES_"))
        and key != "FORWARDED_ALLOW_IPS"
    }
    environment.update(extra or {})
    return environment


def _compose_config(
    tmp_path: Path,
    *args: str,
    env_file: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if env_file is None:
        env_file = tmp_path / "empty.env"
        env_file.write_text("", encoding="utf-8")
    return subprocess.run(
        [
            _docker(),
            "compose",
            "--env-file",
            str(env_file),
            "-f",
            str(ROOT_COMPOSE),
            *args,
        ],
        cwd=REPO_ROOT,
        env=_compose_environment(extra_env),
        check=False,
        capture_output=True,
        text=True,
    )


def _resolved_services(
    tmp_path: Path,
    *args: str,
    env_file: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    result = _compose_config(
        tmp_path,
        *args,
        "config",
        "--format",
        "json",
        env_file=env_file,
        extra_env=extra_env,
    )
    assert result.returncode == 0, result.stderr
    services: dict[str, Any] = json.loads(result.stdout)["services"]
    return services


def test_default_services_resolve_without_mcp_tokens(tmp_path: Path) -> None:
    for arguments in (("config", "--services"), ("config", "--services", "app")):
        result = _compose_config(tmp_path, *arguments)

        assert result.returncode == 0, result.stderr
        for variable in _MCP_TOKEN_VARIABLES:
            assert variable not in result.stderr
    default = _compose_config(tmp_path, "config", "--services")
    assert set(default.stdout.split()) == {"app", "postgres"}


def test_mcp_service_is_opt_in_through_its_profile(tmp_path: Path) -> None:
    services = _resolved_services(tmp_path, "--profile", "mcp")

    assert "mcp" in services
    assert services["mcp"]["profiles"] == ["mcp"]


def test_mcp_binds_container_interface_even_with_env_example_as_dotenv(
    tmp_path: Path,
) -> None:
    services = _resolved_services(
        tmp_path, "--profile", "mcp", env_file=REPO_ROOT / ".env.example"
    )

    mcp = services["mcp"]
    assert mcp["environment"]["LAB_TRACKER_MCP_HOST"] == "0.0.0.0"
    assert mcp["environment"]["LAB_TRACKER_MCP_TRANSPORT"] == "streamable-http"
    published = mcp["ports"][0]
    assert published["host_ip"] == "127.0.0.1"
    assert str(published["target"]) == mcp["environment"]["LAB_TRACKER_MCP_PORT"]


def _mcp_entrypoint(tmp_path: Path) -> list[str]:
    mcp = _resolved_services(tmp_path, "--profile", "mcp")["mcp"]
    assert mcp.get("command") in (None, [])
    # `docker compose config` re-escapes a literal `$` as `$$` so its output can
    # be fed back to Compose; the container receives a single `$`.
    return [part.replace("$$", "$") for part in mcp["entrypoint"]]


def _run_entrypoint(
    entrypoint: list[str], environment: dict[str, str], stub_bin: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        entrypoint,
        env={"PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}", **environment},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.fixture
def python_stub(tmp_path: Path) -> Path:
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    stub = stub_bin / "python"
    stub.write_text('#!/bin/sh\necho "stub-python $*"\n', encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return stub_bin


def test_mcp_entrypoint_fails_loud_without_either_token(
    tmp_path: Path, python_stub: Path
) -> None:
    entrypoint = _mcp_entrypoint(tmp_path)
    inbound = "inbound-" + "a" * 32

    missing_both = _run_entrypoint(entrypoint, {}, python_stub)
    missing_readonly = _run_entrypoint(
        entrypoint, {"LAB_TRACKER_MCP_INBOUND_TOKEN": inbound}, python_stub
    )
    empty_inbound = _run_entrypoint(
        entrypoint,
        {"LAB_TRACKER_MCP_INBOUND_TOKEN": "", "LAB_TRACKER_MCP_API_KEY": "lpat_x"},
        python_stub,
    )

    assert missing_both.returncode != 0
    assert "LT_MCP_INBOUND_TOKEN" in missing_both.stderr
    assert missing_readonly.returncode != 0
    assert "LT_MCP_READONLY_TOKEN" in missing_readonly.stderr
    assert empty_inbound.returncode != 0
    assert "LT_MCP_INBOUND_TOKEN" in empty_inbound.stderr
    for result in (missing_both, missing_readonly, empty_inbound):
        assert "stub-python" not in result.stdout


def test_mcp_entrypoint_execs_the_frozen_image_python(
    tmp_path: Path, python_stub: Path
) -> None:
    entrypoint = _mcp_entrypoint(tmp_path)

    result = _run_entrypoint(
        entrypoint,
        {
            "LAB_TRACKER_MCP_INBOUND_TOKEN": "inbound-" + "a" * 32,
            "LAB_TRACKER_MCP_API_KEY": "lpat_readonly",
        },
        python_stub,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "stub-python -m lab_tracker.mcp_server"
    assert "uv" not in " ".join(entrypoint).split()


def test_mcp_healthcheck_probes_the_listener_not_the_authenticated_app(
    tmp_path: Path,
) -> None:
    healthcheck = _resolved_services(tmp_path, "--profile", "mcp")["mcp"]["healthcheck"]
    test = healthcheck["test"]
    assert "/health" not in " ".join(test)
    assert test[0] == "CMD"

    command = [shutil.which("python3") or "python3", *test[2:]]
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        healthy = subprocess.run(
            command,
            env={**os.environ, "LAB_TRACKER_MCP_PORT": str(port)},
            check=False,
            capture_output=True,
            timeout=30,
        )
    unhealthy = subprocess.run(
        command,
        env={**os.environ, "LAB_TRACKER_MCP_PORT": str(port)},
        check=False,
        capture_output=True,
        timeout=30,
    )

    assert test[1] == "python"
    assert healthy.returncode == 0, healthy.stderr
    assert unhealthy.returncode != 0


def test_long_running_services_run_under_an_init_process(tmp_path: Path) -> None:
    services = _resolved_services(tmp_path, "--profile", "mcp")

    assert services["app"]["init"] is True
    assert services["mcp"]["init"] is True
    assert "\n    init: true\n" in DEDICATED_COMPOSE.read_text(encoding="utf-8")


def test_app_forwards_uvicorn_trusted_proxy_setting(tmp_path: Path) -> None:
    default = _resolved_services(tmp_path)["app"]["environment"]
    configured = _resolved_services(
        tmp_path, extra_env={"FORWARDED_ALLOW_IPS": "172.18.0.1"}
    )["app"]["environment"]

    assert default["FORWARDED_ALLOW_IPS"] == "127.0.0.1"
    assert configured["FORWARDED_ALLOW_IPS"] == "172.18.0.1"


def test_default_database_url_follows_postgres_credential_overrides(
    tmp_path: Path,
) -> None:
    # An operator who changes only POSTGRES_* must not leave the app and the
    # review-email control plane dialling the postgres service with the old
    # built-in credentials.
    profile = ("--profile", "review-email-external")
    default = _resolved_services(tmp_path, *profile)
    overridden = _resolved_services(
        tmp_path,
        *profile,
        extra_env={
            "POSTGRES_USER": "lab_owner",
            "POSTGRES_PASSWORD": "rotated-secret",
            "POSTGRES_DB": "lab_records",
        },
    )
    explicit = _resolved_services(
        tmp_path,
        *profile,
        extra_env={
            "POSTGRES_PASSWORD": "rotated-secret",
            "LAB_TRACKER_DATABASE_URL": "postgresql+psycopg://other:pw@db.example:5432/x",
        },
    )

    for service in ("app", "review-email-control"):
        assert default[service]["environment"]["LAB_TRACKER_DATABASE_URL"] == (
            "postgresql+psycopg://lab_tracker:lab_tracker@postgres:5432/lab_tracker"
        )
        assert overridden[service]["environment"]["LAB_TRACKER_DATABASE_URL"] == (
            "postgresql+psycopg://lab_owner:rotated-secret@postgres:5432/lab_records"
        )
        assert explicit[service]["environment"]["LAB_TRACKER_DATABASE_URL"] == (
            "postgresql+psycopg://other:pw@db.example:5432/x"
        )
    assert overridden["postgres"]["environment"]["POSTGRES_PASSWORD"] == "rotated-secret"


def test_explicitly_targeted_mcp_resolves_without_naming_the_profile(
    tmp_path: Path,
) -> None:
    # Some guides still say `docker compose up mcp`; naming a profiled service
    # activates its profile, so that form keeps working.
    result = _compose_config(tmp_path, "config", "--format", "json", "mcp")
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]

    assert set(services) == {"app", "mcp", "postgres"}


def test_operations_doc_explains_proxy_trust_and_init() -> None:
    operations = (REPO_ROOT / "docs" / "self-hosted-operations.md").read_text(
        encoding="utf-8"
    )

    assert "FORWARDED_ALLOW_IPS=" in operations
    assert "--profile mcp" in operations
    assert "init: true" in operations
    assert "tini" in operations
    assert "Render's Docker runtime has no" not in operations
    assert "COMPOSE_PROJECT_NAME" in operations


def _operations_doc_proxy_override() -> str:
    operations = (REPO_ROOT / "docs" / "self-hosted-operations.md").read_text(
        encoding="utf-8"
    )
    section = operations.split("## Reverse Proxy and Client Addresses\n", 1)[1]
    section = section.split("\n## ", 1)[0]
    return section.split("```yaml\n", 1)[1].split("```", 1)[0]


def test_operations_doc_proxy_override_publishes_the_app_on_loopback_only(
    tmp_path: Path,
) -> None:
    override = tmp_path / "docker-compose.override.yml"
    override.write_text(_operations_doc_proxy_override(), encoding="utf-8")

    services = _resolved_services(tmp_path, "-f", str(override))

    ports = services["app"]["ports"]
    assert ports, "the app must stay published for the proxy"
    assert all(port["host_ip"] == "127.0.0.1" for port in ports), ports


def test_owned_docs_start_the_mcp_service_through_its_profile() -> None:
    for relative in ("docs/deployment-options.md", "docs/lab-tracker-mcp-skills.md"):
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "docker compose --profile mcp up mcp" in text, relative
        assert "docker compose up mcp" not in text, relative
