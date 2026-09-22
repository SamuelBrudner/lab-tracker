"""Behavioural tests for deployments/dedicated-instance/restore-smoke.sh.

The script is run against a fake ``docker`` that models the official postgres
image's first boot: an init-only temporary server that answers on the Unix
socket (so a socket-only ``pg_isready`` succeeds) while TCP connections --
the path ``pg_restore --host postgres`` uses -- are still refused.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "deployments" / "dedicated-instance" / "restore-smoke.sh"

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX shell script")

FAKE_DOCKER = r"""#!/bin/sh
state="${FAKE_DOCKER_STATE:?}"
printf '%s\n' "$*" >> "${state}/calls.log"
bump() {
  count="$(cat "${state}/$1" 2>/dev/null || echo 0)"
  count=$((count + 1))
  echo "${count}" > "${state}/$1"
  echo "${count}"
}
case "$*" in
  *"pg_restore --list"*) exit 0 ;;
esac
case "$1" in
  network|volume|rm) exit 0 ;;
esac
if [ "$1" = run ] && [ "$2" = --detach ]; then
  exit 0
fi
case "$*" in
  *pg_isready*)
    case " $* " in
      *" -h "*|*" --host "*)
        probes="$(bump tcp_probes)"
        if [ "${probes}" -ge "${FAKE_TCP_READY_AFTER:-1}" ]; then
          : > "${state}/tcp_up"
          exit 0
        fi
        echo "postgres:5432 - no response" >&2
        exit 2
        ;;
      *)
        # Unix socket: the init-only temporary server already answers.
        exit 0
        ;;
    esac
    ;;
  *pg_restore*)
    attempts="$(bump restore_attempts)"
    if [ ! -f "${state}/tcp_up" ] \
      || [ "${attempts}" -le "${FAKE_RESTORE_CONNECTION_FAILURES:-0}" ]; then
      echo 'pg_restore: error: connection to server at "postgres" (172.18.0.2), port 5432 failed: Connection refused' >&2
      exit 1
    fi
    if [ -n "${FAKE_RESTORE_FATAL:-}" ]; then
      echo 'pg_restore: error: could not execute query: ERROR:  relation "users" already exists' >&2
      exit 1
    fi
    : > "${state}/restored"
    exit 0
    ;;
  *"AS restored_users"*)
    echo "version_num"
    exit 0
    ;;
  *"SELECT version_num FROM alembic_version;"*)
    echo "0042_head"
    exit 0
    ;;
  *"SELECT count(*) FROM users;"*)
    echo "3"
    exit 0
    ;;
  *"tar -xzf"*) exit 0 ;;
  *"find /restore"*)
    echo "/restore/app.db"
    exit 0
    ;;
esac
echo "fake docker: unexpected invocation: $*" >&2
exit 99
"""


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _prepare(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    tree = tmp_path / "tree"
    deploy_dir = tree / "deployments" / "dedicated-instance"
    deploy_dir.mkdir(parents=True)
    script = deploy_dir / "restore-smoke.sh"
    shutil.copy2(SCRIPT, script)

    backup_dir = tree / "backups" / "dedicated-instance" / "project" / "20260101T000000Z"
    backup_dir.mkdir(parents=True)
    (backup_dir / "postgres.dump").write_bytes(b"PGDMP fake dump")
    payload = tmp_path / "payload.txt"
    payload.write_text("app data", encoding="utf-8")
    with tarfile.open(backup_dir / "app-data.tar.gz", "w:gz") as archive:
        archive.add(payload, arcname="payload.txt")
    manifest_lines = [
        f"{hashlib.sha256((backup_dir / name).read_bytes()).hexdigest()}  {name}"
        for name in ("postgres.dump", "app-data.tar.gz")
    ]
    (backup_dir / "MANIFEST.sha256").write_text(
        "\n".join(manifest_lines) + "\n", encoding="utf-8"
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "docker", FAKE_DOCKER)
    _write_executable(bin_dir / "sleep", "#!/bin/sh\nexit 0\n")
    state = tmp_path / "state"
    state.mkdir()
    return script, backup_dir, bin_dir, state


def _run(tmp_path: Path, **fake_env: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    script, backup_dir, bin_dir, state = _prepare(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DOCKER_STATE": str(state),
        **fake_env,
    }
    result = subprocess.run(
        ["sh", str(script), str(backup_dir)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result, state


def _count(state: Path, name: str) -> int:
    path = state / name
    return int(path.read_text(encoding="utf-8")) if path.exists() else 0


def test_restore_waits_for_tcp_even_when_socket_probe_is_ready(tmp_path: Path) -> None:
    result, state = _run(tmp_path, FAKE_TCP_READY_AFTER="4")

    assert result.returncode == 0, result.stderr
    assert "Disposable restore succeeded" in result.stdout
    assert _count(state, "tcp_probes") >= 4
    assert (state / "restored").exists()


def test_restore_retries_transient_connection_failures(tmp_path: Path) -> None:
    result, state = _run(
        tmp_path, FAKE_TCP_READY_AFTER="1", FAKE_RESTORE_CONNECTION_FAILURES="2"
    )

    assert result.returncode == 0, result.stderr
    assert _count(state, "restore_attempts") == 3
    assert (state / "restored").exists()


def test_restore_connection_retries_are_bounded(tmp_path: Path) -> None:
    result, state = _run(
        tmp_path, FAKE_TCP_READY_AFTER="1", FAKE_RESTORE_CONNECTION_FAILURES="1000"
    )

    assert result.returncode != 0
    attempts = _count(state, "restore_attempts")
    assert 1 < attempts <= 10
    assert "Connection refused" in result.stderr
    assert "Disposable restore succeeded" not in result.stdout


def test_restore_does_not_retry_non_connection_errors(tmp_path: Path) -> None:
    result, state = _run(tmp_path, FAKE_TCP_READY_AFTER="1", FAKE_RESTORE_FATAL="1")

    assert result.returncode != 0
    assert _count(state, "restore_attempts") == 1
    assert "already exists" in result.stderr
