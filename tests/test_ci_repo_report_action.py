"""The reusable CI capture action and its commit-identity contract with the hook.

``.github/actions/lab-tracker-repo-report/action.yml`` runs ``lt repo report
--fail-silent`` for the pushed commit. A commit captured both by a developer's
post-commit hook and by CI must land as one note: both paths derive the
evidence identity ``<normalized-remote>@<sha>`` with the same
``repo.normalize_remote`` (the action runs ``lt repo report`` itself), use it as
the note's ``client_capture_id``, and the server keeps the first note for a
``(project, client_capture_id)`` pair, refusing a different second capture with
HTTP 409 instead of creating a parallel note.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lab_tracker_client import LabTracker
from lab_tracker_client import repo as repo_capture
from lab_tracker_client.yaml_subset import load_yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ACTION = _REPO_ROOT / ".github" / "actions" / "lab-tracker-repo-report" / "action.yml"
_TOKEN_VARS = ("LT_INPUT_ACCESS_TOKEN", "LAB_TRACKER_ACCESS_TOKEN")


def _action() -> dict:
    parsed = load_yaml(_ACTION.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def _step(name: str) -> dict:
    for step in _action()["runs"]["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"no step named {name!r}")


def _git(path: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


def _commit_repo(path: Path, remote: str) -> str:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")
    _git(path, "remote", "add", "origin", remote)
    (path / ".gitignore").write_text(".lab-tracker/\n", encoding="utf-8")
    (path / "analysis.py").write_text("print('decode')\n", encoding="utf-8")
    _git(path, "add", ".gitignore", "analysis.py")
    _git(path, "commit", "-q", "-m", "decode stimulus identity")
    return _git(path, "rev-parse", "HEAD")


def test_action_is_a_composite_that_installs_a_pinned_client_and_reports() -> None:
    action = _action()

    assert action["runs"]["using"] == "composite"
    inputs = action["inputs"]
    assert inputs["lab-tracker-ref"]["required"] is True
    for name in ("base-url", "access-token", "project-id", "include-pr-text", "remote-url"):
        assert name in inputs
    assert inputs["commit"]["default"] == "${{ github.event.pull_request.head.sha || github.sha }}"
    install = _step("Install the Lab Tracker client")["run"]
    assert '"lab-tracker @ git+https://github.com/${LT_REPOSITORY}@${LT_REF}"' in install
    report = _step("Report the commit to Lab Tracker")
    assert report["if"] == "steps.install.outputs.ready == 'true'"
    assert "set -- repo report --config" in report["run"]
    assert "--fail-silent" in report["run"]
    assert report["run"].rstrip().endswith("exit 0")


def test_the_access_token_is_never_echoed() -> None:
    action_text = _ACTION.read_text(encoding="utf-8")
    steps = _action()["runs"]["steps"]

    # The token input reaches exactly one step, and only through its env block.
    assert action_text.count("inputs.access-token") == 1
    report = _step("Report the commit to Lab Tracker")
    assert report["env"]["LT_INPUT_ACCESS_TOKEN"] == "${{ inputs.access-token }}"
    for step in steps:
        script = step["run"]
        # No expression is interpolated into a script; values arrive via env.
        assert "${{" not in script
        assert not re.search(r"^\s*set\s+-[a-z]*x|set -o xtrace", script, re.MULTILINE)
        for line in script.splitlines():
            if re.search(r"\b(echo|printf|annotate|cat|tee)\b", line):
                assert not any(var in line for var in _TOKEN_VARS), line
    assert "unset LT_INPUT_ACCESS_TOKEN" in report["run"]


def _fake_lt(tmp_path: Path) -> Path:
    fake = tmp_path / "fake-lt"
    fake.write_text(
        """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_LT_DIR/args.$2"
if [ "$2" = "report" ]; then
  env | grep -E '^(GIT_CONFIG_|LAB_TRACKER_BASE_URL=)' | sort > "$FAKE_LT_DIR/env"
  [ -n "${LAB_TRACKER_ACCESS_TOKEN:-}" ] && echo token-present >> "$FAKE_LT_DIR/env"
  case "${FAKE_MODE:-ok}" in
    ok) echo '{"action": "captured"}' ;;
    conflict)
      echo "lab-tracker: repo capture did not fully sync - 1 event(s) failed to sync" \\
        "(Note client_capture_id 'x' was already used with different field(s))." >&2 ;;
    silent) ;;
    crash) echo "Traceback: boom" >&2; exit 1 ;;
  esac
fi
exit 0
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


def _run_report_step(tmp_path: Path, repo: Path, **overrides: str):
    script = tmp_path / "report-step.sh"
    script.write_text(_step("Report the commit to Lab Tracker")["run"], encoding="utf-8")
    calls = tmp_path / "calls"
    calls.mkdir(exist_ok=True)
    for stale in calls.iterdir():
        stale.unlink()
    (tmp_path / "runner").mkdir(exist_ok=True)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "RUNNER_TEMP": str(tmp_path / "runner"),
        "FAKE_LT_DIR": str(calls),
        "LT": str(_fake_lt(tmp_path)),
        "LT_PYTHON": sys.executable,
        "PYTHONPATH": str(_REPO_ROOT / "src"),
        "LT_INPUT_BASE_URL": "https://lab.example.org",
        "LT_INPUT_ACCESS_TOKEN": "lpat_topsecret_value",
        "LT_INPUT_PROJECT_ID": "project-1",
        "LT_QUESTION_ID": "",
        "LT_EXPECTED_COMMIT": _git(repo, "rev-parse", "HEAD"),
        "LT_REMOTE_URL": "",
        "LT_INCLUDE_PR_TEXT": "false",
        "LT_PR_TEXT_MAX_CHARS": "2000",
        "LT_PR_TITLE": "",
        "LT_PR_BODY": "",
        **overrides,
    }
    completed = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return completed, calls


def test_report_step_runs_lt_repo_report_with_pr_text_and_remote_override(tmp_path) -> None:
    repo = tmp_path / "checkout"
    sha = _commit_repo(repo, "https://github.com/Lab/Analysis")

    completed, calls = _run_report_step(
        tmp_path,
        repo,
        LT_REMOTE_URL="git@gh-work:Lab/Analysis.git",
        LT_INCLUDE_PR_TEXT="true",
        LT_PR_TEXT_MAX_CHARS="40",
        LT_PR_TITLE="Widen the decoding window",
        LT_PR_BODY="Adds 50 ms on each side. password=hunter2 " + "x" * 200,
        LT_QUESTION_ID="question-9",
    )

    assert completed.returncode == 0, completed.stderr
    assert f"::notice title=Lab Tracker::commit {sha} captured as a staged note." in (
        completed.stdout
    )
    assert "topsecret" not in completed.stdout + completed.stderr
    args = (calls / "args.report").read_text().splitlines()
    assert args[:4] == [
        "repo",
        "report",
        "--config",
        str(tmp_path / "runner" / "lab-tracker-repo-report" / "repo.json"),
    ]
    assert "--fail-silent" in args
    assert args[args.index("--question") + 1] == "question-9"
    summary = args[args.index("--summary") + 1 :]
    summary_text = "\n".join(summary)
    assert summary_text.startswith("Widen the decoding window")
    assert len(summary_text) <= 40
    assert "hunter2" not in summary_text
    init = (calls / "args.init").read_text().splitlines()
    assert init[init.index("--project") + 1] == "project-1"
    env = (calls / "env").read_text().splitlines()
    assert "GIT_CONFIG_COUNT=1" in env
    assert "GIT_CONFIG_KEY_0=remote.origin.url" in env
    assert "GIT_CONFIG_VALUE_0=git@gh-work:Lab/Analysis.git" in env
    assert "LAB_TRACKER_BASE_URL=https://lab.example.org" in env
    assert "token-present" in env


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"LT_EXPECTED_COMMIT": "0" * 40}, "::warning title=Lab Tracker::HEAD is"),
        ({"LT_INPUT_BASE_URL": ""}, "::warning title=Lab Tracker::no base-url"),
        ({"LT_INPUT_PROJECT_ID": ""}, "::warning title=Lab Tracker::no project-id"),
    ],
)
def test_report_step_skips_without_reporting(tmp_path, overrides, expected) -> None:
    repo = tmp_path / "checkout"
    _commit_repo(repo, "https://github.com/Lab/Analysis")

    completed, calls = _run_report_step(tmp_path, repo, **overrides)

    assert completed.returncode == 0
    assert expected in completed.stdout
    assert not (calls / "args.report").exists()


def test_report_step_explains_a_commit_the_hook_already_captured(tmp_path) -> None:
    repo = tmp_path / "checkout"
    _commit_repo(repo, "https://github.com/Lab/Analysis")
    (repo / "lt_ids.json").write_text(json.dumps({"project_id": "bound"}), encoding="utf-8")

    completed, calls = _run_report_step(
        tmp_path, repo, FAKE_MODE="conflict", LT_INPUT_PROJECT_ID=""
    )

    assert completed.returncode == 0
    assert "is already captured under its <remote>@<sha> identity" in completed.stdout
    init = (calls / "args.init").read_text().splitlines()
    assert init[init.index("--project") + 1] == "bound"

    completed, _calls = _run_report_step(tmp_path, repo, FAKE_MODE="silent")
    assert "recorded nothing" in completed.stdout

    # GitHub runs the step under bash -e: a crashing client must not fail the job.
    completed, _calls = _run_report_step(tmp_path, repo, FAKE_MODE="crash")
    assert completed.returncode == 0
    assert "::warning title=Lab Tracker::Traceback: boom" in completed.stdout


@pytest.mark.parametrize(
    ("ref", "python", "expected"),
    [
        ("", sys.executable, "lab-tracker-ref is empty"),
        ("v9.9.9", "false", "could not install lab-tracker@v9.9.9"),
    ],
)
def test_install_step_failures_skip_the_capture_without_failing(
    tmp_path, ref, python, expected
) -> None:
    script = tmp_path / "install-step.sh"
    script.write_text(_step("Install the Lab Tracker client")["run"], encoding="utf-8")
    output = tmp_path / "github-output"
    completed = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        env={
            "PATH": os.environ["PATH"],
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_OUTPUT": str(output),
            "LT_REPOSITORY": "SamuelBrudner/lab-tracker",
            "LT_REF": ref,
            "LT_BOOTSTRAP_PYTHON": python,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert completed.returncode == 0
    assert expected in completed.stdout
    assert output.read_text().strip().splitlines()[-1] == "ready=false"


def test_ci_checkout_and_local_clone_share_the_commit_identity(tmp_path, monkeypatch) -> None:
    for name in ("LAB_TRACKER_REPO_CONFIG", "LAB_TRACKER_REPO_OUTBOX", "LAB_TRACKER_REPO_RUN_ID"):
        monkeypatch.delenv(name, raising=False)
    local = tmp_path / "laptop"
    sha = _commit_repo(local, "git@github.com:Lab/Analysis.git")
    ci = tmp_path / "runner-checkout"
    subprocess.run(["git", "clone", "-q", str(local), str(ci)], check=True)
    # actions/checkout: an https origin without credentials or a .git suffix.
    _git(ci, "remote", "set-url", "origin", "https://github.com/Lab/Analysis")

    identities = set()
    for checkout in (local, ci):
        config = repo_capture.init_config(
            project_id="p", config_path=checkout / ".lab-tracker" / "repo.json"
        )
        event = repo_capture.make_event(config, event_type="commit", cwd=checkout)
        identities.add(repo_capture.event_source_external_id(event))

    assert identities == {f"github.com/lab/analysis@{sha}"}


def test_remote_url_override_reaches_lts_git_calls_only(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "checkout"
    _commit_repo(repo, "https://github.com/Lab/Analysis")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "remote.origin.url")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "git@gh-work:Lab/Analysis.git")

    context = repo_capture.git_context(repo)

    assert context["repo_remote_url"] == "git@gh-work:Lab/Analysis.git"
    monkeypatch.delenv("GIT_CONFIG_COUNT")
    assert "https://github.com/Lab/Analysis" in (repo / ".git" / "config").read_text()


def test_server_keeps_one_note_per_commit_identity(
    tmp_path, monkeypatch, client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    for name in ("LAB_TRACKER_REPO_CONFIG", "LAB_TRACKER_REPO_OUTBOX", "LAB_TRACKER_REPO_RUN_ID"):
        monkeypatch.delenv(name, raising=False)
    project = client.post("/projects", json={"name": "CI dedupe"}, headers=admin_auth_headers)
    assert project.status_code in (200, 201), project.text
    project_id = project.json()["data"]["project_id"]
    local = tmp_path / "laptop"
    _commit_repo(local, "git@github.com:Lab/Analysis.git")
    ci = tmp_path / "runner-checkout"
    subprocess.run(["git", "clone", "-q", str(local), str(ci)], check=True)
    _git(ci, "remote", "set-url", "origin", "https://github.com/Lab/Analysis")
    token = admin_auth_headers["Authorization"].split(" ", 1)[1]
    results = []
    for checkout, host in ((local, "laptop"), (ci, "github-runner")):
        monkeypatch.setenv("LAB_TRACKER_CAPTURE_HOST", host)
        config = repo_capture.init_config(
            project_id=project_id, config_path=checkout / ".lab-tracker" / "repo.json"
        )
        repo_capture.capture_commit(config, cwd=checkout, tags=[host])
        with LabTracker(
            base_url="http://testserver", access_token=token, transport=client._transport
        ) as lt:
            results.append(repo_capture.sync_outbox_path(lt, config.outbox_path()))

    hook, ci_sync = results
    assert hook["errors"] == []
    assert [item["action"] for item in hook["results"]] == ["imported"]
    [refused] = ci_sync["errors"]
    assert "already used with different field" in refused["error"]
    notes = client.get(
        "/notes", params={"project_id": project_id}, headers=admin_auth_headers
    ).json()["data"]
    assert len([note for note in notes if note["metadata"].get("repo_git_commit")]) == 1
