"""Behavioural tests for the auth guard in scripts/serve-lan.sh (and serve-lan.ps1)."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SHELL_SCRIPT = REPO_ROOT / "scripts" / "serve-lan.sh"
POWERSHELL_SCRIPT = REPO_ROOT / "scripts" / "serve-lan.ps1"

# Forward to the real interpreter, except for the migration/server launch,
# which only records that the guard let the script through.
FAKE_PYTHON = f"""#!/bin/sh
case "$1 $2" in
  "-m alembic") echo "FAKE-ALEMBIC $*"; exit 0 ;;
  "-m uvicorn") echo "FAKE-UVICORN $*"; exit 0 ;;
esac
exec "{sys.executable}" "$@"
"""


def _run(tmp_path: Path, *args: str, **env_overrides: str) -> subprocess.CompletedProcess[str]:
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    shutil.copy2(SHELL_SCRIPT, tree / "scripts" / "serve-lan.sh")
    (tree / "src").symlink_to(REPO_ROOT / "src", target_is_directory=True)
    fake_python = tmp_path / "python"
    fake_python.write_text(FAKE_PYTHON, encoding="utf-8")
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("LAB_TRACKER_")
    }
    env.update(
        {
            "PYTHON_BIN": str(fake_python),
            "LAB_TRACKER_ENVIRONMENT": "local",
            "LAB_TRACKER_AUTH_ENABLED": "false",
            "LAB_TRACKER_DATABASE_URL": f"sqlite:///{tmp_path / 'lab.db'}",
        }
    )
    env.update(env_overrides)
    return subprocess.run(
        ["sh", str(tree / "scripts" / "serve-lan.sh"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell script")
@pytest.mark.parametrize(
    "host", ["0.0.0.0", "::", "10.0.0.5", "192.168.1.20", "fd00::5", "lab-box.local"]
)
def test_refuses_any_non_loopback_host_when_auth_is_disabled(
    tmp_path: Path, host: str
) -> None:
    result = _run(tmp_path, "--host", host)

    assert result.returncode == 1, result.stdout + result.stderr
    assert f"Refusing to bind Lab Tracker to {host} while authentication is disabled" in (
        result.stderr
    )
    assert "FAKE-ALEMBIC" not in result.stdout
    assert "FAKE-UVICORN" not in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell script")
def test_default_host_is_refused_when_auth_is_disabled(tmp_path: Path) -> None:
    result = _run(tmp_path)

    assert result.returncode == 1
    assert "Refusing to bind Lab Tracker to 0.0.0.0" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell script")
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.2"])
def test_loopback_host_is_allowed_when_auth_is_disabled(tmp_path: Path, host: str) -> None:
    result = _run(tmp_path, "--host", host)

    assert result.returncode == 0, result.stderr
    assert f"FAKE-UVICORN -m uvicorn lab_tracker.asgi:app --host {host}" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell script")
def test_explicit_insecure_override_allows_a_lan_host(tmp_path: Path) -> None:
    result = _run(tmp_path, "--host", "10.0.0.5", "--allow-insecure-auth-disabled")

    assert result.returncode == 0, result.stderr
    assert "FAKE-UVICORN -m uvicorn lab_tracker.asgi:app --host 10.0.0.5" in result.stdout


def test_shell_guard_shares_the_cli_loopback_rule() -> None:
    script = SHELL_SCRIPT.read_text(encoding="utf-8")

    assert "from lab_tracker.cli import _is_non_loopback_host" in script
    assert '[ "$HOST" = "0.0.0.0" ]' not in script


def test_powershell_helper_binds_only_all_interfaces_behind_the_auth_guard() -> None:
    script = POWERSHELL_SCRIPT.read_text(encoding="utf-8")

    # serve-lan.ps1 has no -Host option: it always binds 0.0.0.0 and always
    # evaluates the auth guard before starting uvicorn.
    assert "[string]$Host" not in script
    assert script.index("Refusing to bind Lab Tracker to 0.0.0.0") < script.index(
        "-m uvicorn lab_tracker.asgi:app --host 0.0.0.0"
    )
