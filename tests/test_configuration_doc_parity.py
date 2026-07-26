"""Drift guard: docs/configuration.md stays in step with the real env surface.

Two directions, assert-only (the doc has rich per-var prose, so it is not
generated):

1. Every ``lab_tracker.config.Settings`` field must have a documented
   ``LAB_TRACKER_*`` bullet — adding or renaming a server setting without
   documenting it fails here.
2. Every documented bullet must name a variable the code actually consumes —
   either a ``Settings`` field or a direct environment read (the MCP, Dolt
   mirror, and deploy surfaces read ``LAB_TRACKER_*`` without going through
   ``Settings``) — so a stale bullet after a rename/removal fails here.

Consumer-side client variables (``LAB_TRACKER_BASE_URL`` etc.) are documented
in the client docs and appear in configuration.md only inside fenced code
blocks; the start-of-line bullet regex deliberately excludes them.
"""

from __future__ import annotations

import re
from pathlib import Path

from lab_tracker.config import Settings

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOC_PATH = _REPO_ROOT / "docs" / "configuration.md"

_BULLET_PATTERN = re.compile(r"(?m)^\s*-\s+`(LAB_TRACKER_[A-Z0-9_]+)`")
_VAR_PATTERN = re.compile(r"\bLAB_TRACKER_[A-Z0-9_]+\b")

_SCAN_ROOTS = ("src", "scripts", "deploy")
_SCAN_FILES = (
    "docker-compose.yml",
    "Dockerfile",
    ".env.example",
    "render.yaml",
)
_SCAN_SUFFIXES = {".py", ".sh", ".yml", ".yaml", ""}


def _settings_env_vars() -> set[str]:
    return {f"LAB_TRACKER_{name}".upper() for name in Settings.model_fields}


def _documented_env_vars() -> set[str]:
    return set(_BULLET_PATTERN.findall(_DOC_PATH.read_text()))


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


def test_every_documented_variable_is_consumed_by_the_code() -> None:
    stale = _documented_env_vars() - _consumed_env_vars()
    assert not stale, (
        "docs/configuration.md documents variables nothing consumes "
        f"(stale after a rename/removal?): {sorted(stale)}"
    )
