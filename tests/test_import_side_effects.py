"""Importing Lab Tracker entry points must not build server Settings or engines.

Client-side and operator entry points (``lt``, ``lt-mcp``, the deployment probe,
the external review-email worker) import the ``lab_tracker`` package. If any
module constructs ``Settings`` at import time, a shell or container carrying
production-style ``LAB_TRACKER_*`` variables without every server secret dies
with a pydantic traceback before the entry point's own code can run.

Each check runs in a fresh interpreter so module caching in the test process
cannot mask an import-time side effect.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ENTRY_POINT_MODULES = (
    "lab_tracker",
    "lab_tracker.cli",
    "lab_tracker.deployment_probe",
    "lab_tracker.mcp_server",
    "lab_tracker.review_email_external_worker",
    "lab_tracker_client.cli",
)

# Settings() raises for this environment: production requires a strong auth
# secret and none is provided.
_INVALID_SETTINGS_ENV = {"LAB_TRACKER_ENVIRONMENT": "production"}

_COUNT_SETTINGS_THEN_IMPORT = """
import importlib
import json
import sys

import pydantic_settings

constructed = []
_original_init = pydantic_settings.BaseSettings.__init__


def _recording_init(self, *args, **kwargs):
    module = type(self).__module__
    # Third-party settings (e.g. the MCP SDK's FastMCP settings) are not ours.
    if module.split(".")[0] in {"lab_tracker", "lab_tracker_client"}:
        constructed.append(f"{module}.{type(self).__qualname__}")
    _original_init(self, *args, **kwargs)


pydantic_settings.BaseSettings.__init__ = _recording_init
importlib.import_module(sys.argv[1])
print(json.dumps({"constructed": constructed}))
"""


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("LAB_TRACKER_")
    }
    env.update(_INVALID_SETTINGS_ENV)
    env["HOME"] = str(tmp_path)
    return env


@pytest.mark.parametrize("module", ENTRY_POINT_MODULES)
def test_importing_entry_point_does_not_construct_settings(
    tmp_path: Path, module: str
) -> None:
    # cwd=tmp_path keeps a developer's repo-root .env out of the picture.
    result = subprocess.run(
        [sys.executable, "-c", _COUNT_SETTINGS_THEN_IMPORT, module],
        capture_output=True,
        text=True,
        env=_isolated_env(tmp_path),
        cwd=tmp_path,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"constructed": []}


def test_deployment_probe_reports_invalid_settings_as_json_failure_line(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "lab_tracker.deployment_probe",
            "--expected-app-name",
            "lab-tracker",
            "--expected-environment",
            "production",
            "--expected-source-revision",
            "0123456789abcdef0123456789abcdef01234567",
        ],
        capture_output=True,
        text=True,
        env=_isolated_env(tmp_path),
        cwd=tmp_path,
        timeout=120,
    )
    assert result.returncode == 1
    assert "Traceback" not in result.stderr, result.stderr
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1, result.stdout
    payload = json.loads(lines[0])
    assert payload["status"] == "fail"
    assert isinstance(payload["error"], str) and payload["error"]
