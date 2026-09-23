"""Drift guard: docs/configuration.md stays in step with the real env surface.

Three checks, assert-only (the doc has rich per-var prose, so it is not
generated):

1. Every ``lab_tracker.config.Settings`` field must have a documented
   ``LAB_TRACKER_*`` bullet — adding or renaming a server setting without
   documenting it fails here.
2. Every documented bullet must name a variable the code actually consumes —
   either a ``Settings`` field or a direct environment read (the MCP, Dolt
   mirror, and deploy surfaces read ``LAB_TRACKER_*`` without going through
   ``Settings``) — so a stale bullet after a rename/removal fails here.
3. Every ``LAB_TRACKER_*`` variable ``.env.example`` sets must be documented
   and consumed, so the operator template cannot drift from either.

The canonical ``LAB_TRACKER_BASE_URL`` is shared by server and clients.
Client-only variables are documented alongside the MCP service-client section.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from lab_tracker.config import Settings

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOC_PATH = _REPO_ROOT / "docs" / "configuration.md"
_ENV_EXAMPLE_PATH = _REPO_ROOT / ".env.example"

# A bullet head names one variable or several joined by " / ", e.g.
# "- `LAB_TRACKER_MCP_API_KEY` / `LAB_TRACKER_MCP_TOKEN`: ...".
_BULLET_PATTERN = re.compile(
    r"(?m)^\s*-\s+(`LAB_TRACKER_[A-Z0-9_]+`(?:\s*/\s*`LAB_TRACKER_[A-Z0-9_]+`)*)"
)
_VAR_PATTERN = re.compile(r"\bLAB_TRACKER_[A-Z0-9_]+\b")

_SCAN_ROOTS = ("src", "scripts", "deploy")
# Files that consume variables. .env.example only sets them, so it is checked
# separately (every variable it sets must be documented and consumed).
_SCAN_FILES = (
    "docker-compose.yml",
    "Dockerfile",
    "render.yaml",
)
_SCAN_SUFFIXES = {".py", ".sh", ".yml", ".yaml", ""}


def _settings_env_vars() -> set[str]:
    return {f"LAB_TRACKER_{name}".upper() for name in Settings.model_fields}


def _documented_env_vars() -> set[str]:
    return {
        name
        for head in _BULLET_PATTERN.findall(_DOC_PATH.read_text())
        for name in _VAR_PATTERN.findall(head)
    }


def _consumed_env_vars() -> set[str]:
    consumed = set(_settings_env_vars())
    paths: list[Path] = [_REPO_ROOT / name for name in _SCAN_FILES]
    for root in _SCAN_ROOTS:
        base = _REPO_ROOT / root
        if base.exists():
            paths.extend(base.rglob("*"))
    for path in paths:
        if not path.is_file() or path.suffix not in _SCAN_SUFFIXES:
            continue
        parts = set(path.parts)
        if "__pycache__" in parts or "node_modules" in parts:
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        consumed.update(_VAR_PATTERN.findall(text))
    return consumed


def test_env_prefix_is_stable() -> None:
    assert Settings.model_config.get("env_prefix") == "LAB_TRACKER_"


def test_every_settings_field_is_documented() -> None:
    missing = _settings_env_vars() - _documented_env_vars()
    assert not missing, (
        "Settings fields without a docs/configuration.md bullet "
        f"(document them): {sorted(missing)}"
    )


def test_every_scan_file_is_actually_scanned() -> None:
    unscanned = [
        name
        for name in _SCAN_FILES
        if not (_REPO_ROOT / name).is_file()
        or (_REPO_ROOT / name).suffix not in _SCAN_SUFFIXES
    ]
    assert not unscanned, f"_SCAN_FILES entries the scan silently skips: {unscanned}"


def test_slash_joined_bullets_document_every_variable_they_name() -> None:
    # e.g. "- `LAB_TRACKER_MCP_API_KEY` / `LAB_TRACKER_MCP_TOKEN`: ..."
    assert {
        "LAB_TRACKER_MCP_TOKEN",
        "LAB_TRACKER_MCP_PASSWORD",
        "LAB_TRACKER_MCP_PORT",
        "LAB_TRACKER_MCP_PATH",
    } <= _documented_env_vars()


def _env_example_vars() -> set[str]:
    return set(_VAR_PATTERN.findall(_ENV_EXAMPLE_PATH.read_text(encoding="utf-8")))


def test_every_env_example_variable_is_documented() -> None:
    undocumented = _env_example_vars() - _documented_env_vars()
    assert not undocumented, (
        ".env.example variables without a docs/configuration.md bullet "
        f"(document them): {sorted(undocumented)}"
    )


def test_every_env_example_variable_is_consumed_by_the_code() -> None:
    stale = _env_example_vars() - _consumed_env_vars()
    assert not stale, (
        ".env.example sets variables nothing consumes "
        f"(stale after a rename/removal?): {sorted(stale)}"
    )


def test_every_documented_variable_is_consumed_by_the_code() -> None:
    stale = _documented_env_vars() - _consumed_env_vars()
    assert not stale, (
        "docs/configuration.md documents variables nothing consumes "
        f"(stale after a rename/removal?): {sorted(stale)}"
    )


_SETUP_DOC_PATH = _REPO_ROOT / "docs" / "setup.md"


def _non_docker_first_admin_commands() -> list[str]:
    text = _SETUP_DOC_PATH.read_text(encoding="utf-8")
    section = text.split("### Non-Docker\n", 1)[1].split("\n### ", 1)[0]
    block = section.split("```bash\n", 1)[1].split("```", 1)[0]
    lines = block.splitlines()
    assert lines[-1] == "lab-tracker serve"
    return lines[:-1]


def _run_non_docker_first_admin_block(home: Path) -> str:
    script = "\n".join(
        [
            "set -eu -o pipefail",
            *_non_docker_first_admin_commands(),
            'exec "$PYTHON" -c "from lab_tracker.config import Settings; '
            "settings = Settings(_env_file=None); "
            "assert settings.is_auth_enabled(); "
            "assert settings.bootstrap_admin_token; "
            'print(settings.auth_secret_key)"',
        ]
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("LAB_TRACKER_")
    }
    environment["PATH"] = os.pathsep.join(
        [str(Path(sys.executable).parent), environment.get("PATH", "")]
    )
    environment["PYTHON"] = sys.executable
    environment["HOME"] = str(home)

    result = subprocess.run(
        ["bash", "-c", script],
        env=environment,
        cwd=_REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_setup_non_docker_first_admin_environment_is_accepted(tmp_path: Path) -> None:
    """The documented first-admin block builds valid auth-enabled settings.

    The block is run twice: the secret is generated once into a private file and
    reused on restart, because a new secret signs every user out.
    """
    first = _run_non_docker_first_admin_block(tmp_path)
    second = _run_non_docker_first_admin_block(tmp_path)

    assert len(first) >= 32
    assert second == first
    secret_files = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert len(secret_files) == 1, secret_files
    assert secret_files[0].stat().st_mode & 0o077 == 0


_AUTH_ENABLE_INSTRUCTION_DOCS = (
    _DOC_PATH,
    _REPO_ROOT / "docs" / "lab-tracker-mcp-skills.md",
)
_AUTH_ENABLE_INSTRUCTION = re.compile(r"\bSet\s+`LAB_TRACKER_AUTH_ENABLED=true`")


def test_auth_enable_instructions_also_require_a_strong_secret() -> None:
    """Enabling auth with the placeholder secret is rejected even in ``local``.

    Every prose instruction to turn auth on must also tell the reader to set
    ``LAB_TRACKER_AUTH_SECRET_KEY`` in the same paragraph, or following it
    fails at startup with a validation error.
    """
    offenders: list[str] = []
    for path in _AUTH_ENABLE_INSTRUCTION_DOCS:
        for paragraph in re.split(r"\n\s*\n", path.read_text(encoding="utf-8")):
            if _AUTH_ENABLE_INSTRUCTION.search(paragraph) and (
                "LAB_TRACKER_AUTH_SECRET_KEY" not in paragraph
            ):
                offenders.append(f"{path.relative_to(_REPO_ROOT)}: {paragraph!r}")
    assert not offenders, offenders


def test_auth_secret_bullet_describes_when_the_placeholder_is_rejected() -> None:
    text = _DOC_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"(?m)^- `LAB_TRACKER_AUTH_SECRET_KEY`.*(?:\n  .*)*", text
    )
    assert match is not None
    bullet = " ".join(match.group(0).split())
    assert "allowed only in `local`" not in bullet
    assert "rejected whenever authentication is enabled" in bullet
