"""Remote-URL credential hygiene shared by every client capture adapter.

Git remotes can embed secrets (``https://oauth2:<token>@...``,
``https://<token>@...``, ``ssh://user:<password>@...``, ``?access_token=``).
Anything a capture adapter records or renders must go through the one shared
sanitiser so those secrets never reach an outbox event, a staged note body, or
note metadata uploaded to Lab Tracker.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from lab_tracker_client import figure as figure_module
from lab_tracker_client.gitinfo import sanitize_remote_url
from lab_tracker_client.repo import (
    capture_commit,
    event_metadata,
    event_source_external_id,
    init_config,
    normalize_remote,
    render_event_note,
)

SECRET = "SEKRET_TOKEN_123"


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        # Credential-free forms are returned unchanged.
        ("https://github.com/lab/repo.git", "https://github.com/lab/repo.git"),
        ("http://example.com:8080/lab/repo", "http://example.com:8080/lab/repo"),
        ("git@github.com:lab/repo.git", "git@github.com:lab/repo.git"),
        ("github.com:lab/repo.git", "github.com:lab/repo.git"),
        ("ssh://git@github.com/lab/repo.git", "ssh://git@github.com/lab/repo.git"),
        ("ssh://git@github.com:2222/lab/repo.git", "ssh://git@github.com:2222/lab/repo.git"),
        ("git://example.com/lab/repo.git", "git://example.com/lab/repo.git"),
        ("file:///srv/git/repo.git", "file:///srv/git/repo.git"),
        ("/srv/git/repo.git", "/srv/git/repo.git"),
        ("../sibling/repo", "../sibling/repo"),
        ("", ""),
        ("  https://github.com/lab/repo.git\n", "https://github.com/lab/repo.git"),
        # http(s) user:password userinfo.
        (
            f"https://oauth2:{SECRET}@gitlab.example.com/lab/repo.git",
            "https://gitlab.example.com/lab/repo.git",
        ),
        (f"https://user:{SECRET}@example.com/lab/repo.git", "https://example.com/lab/repo.git"),
        # http(s) bare-token userinfo (GitHub PAT / ${GITHUB_TOKEN} form).
        (f"https://{SECRET}@github.com/lab/repo.git", "https://github.com/lab/repo.git"),
        (f"HTTPS://{SECRET}@github.com/lab/repo.git", "HTTPS://github.com/lab/repo.git"),
        (
            f"http://x-access-token:{SECRET}@example.com:8080/lab/repo",
            "http://example.com:8080/lab/repo",
        ),
        # A raw '@' inside the password still resolves to the real host.
        (f"https://user:p@ss{SECRET}@example.com/lab/repo.git", "https://example.com/lab/repo.git"),
        # Query strings and fragments can carry tokens.
        (
            f"https://github.com/lab/repo.git?access_token={SECRET}",
            "https://github.com/lab/repo.git",
        ),
        (f"https://github.com/lab/repo.git#{SECRET}", "https://github.com/lab/repo.git"),
        (
            f"https://sam:{SECRET}@github.com/lab/repo.git?private_token={SECRET}#frag",
            "https://github.com/lab/repo.git",
        ),
        # ssh:// keeps the login name (addressing) but never a password.
        (f"ssh://git:{SECRET}@example.com/lab/repo.git", "ssh://git@example.com/lab/repo.git"),
        (
            f"git+ssh://deploy:{SECRET}@example.com:22/lab/repo",
            "git+ssh://deploy@example.com:22/lab/repo",
        ),
        (f"ssh://:{SECRET}@example.com/lab/repo", "ssh://example.com/lab/repo"),
        # Non-ssh transports never need userinfo.
        (f"git://{SECRET}@example.com/lab/repo.git", "git://example.com/lab/repo.git"),
        # scp-like addressing is kept verbatim (it has no password syntax).
        ("deploy@example.com:lab/repo@v2.git", "deploy@example.com:lab/repo@v2.git"),
    ],
)
def test_sanitize_remote_url_forms(remote: str, expected: str) -> None:
    sanitized = sanitize_remote_url(remote)

    assert sanitized == expected
    assert SECRET not in sanitized
    # Idempotent: sanitising twice changes nothing.
    assert sanitize_remote_url(sanitized) == sanitized


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/lab/repo.git",
        "git@github.com:lab/repo.git",
        "ssh://git@github.com/lab/repo.git",
        f"https://oauth2:{SECRET}@gitlab.example.com/lab/repo.git",
        f"https://{SECRET}@github.com/lab/repo.git",
        f"ssh://git:{SECRET}@example.com/lab/repo.git",
    ],
)
def test_sanitising_keeps_normalized_remote_identities_stable(remote: str) -> None:
    # Deterministic evidence identities (<normalized-remote>@<commit>) already
    # dropped userinfo, so sanitising first must not move existing identities.
    assert normalize_remote(sanitize_remote_url(remote)) == normalize_remote(remote)


def test_figure_remote_identity_uses_shared_sanitiser() -> None:
    remote = f"https://{SECRET}@github.com/lab/repo.git?access_token={SECRET}#frag"

    assert figure_module._credential_free_repo_remote(remote) == "github.com/lab/repo"


# --- lt repo capture (H10) ---------------------------------------------------


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_git_repo(path: Path, remote: str) -> str:
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")
    _git(path, "remote", "add", "origin", remote)
    (path / ".gitignore").write_text(".lab-tracker/\n", encoding="utf-8")
    (path / "analysis.py").write_text("print('hi')\n", encoding="utf-8")
    _git(path, "add", ".gitignore", "analysis.py")
    _git(path, "commit", "-q", "-m", "initial analysis")
    return _git(path, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    ("remote", "expected_remote"),
    [
        (
            f"https://oauth2:{SECRET}@gitlab.example.com/lab/repo.git",
            "https://gitlab.example.com/lab/repo.git",
        ),
        (f"https://{SECRET}@github.com/lab/repo.git", "https://github.com/lab/repo.git"),
        (
            f"https://github.com/lab/repo.git?access_token={SECRET}",
            "https://github.com/lab/repo.git",
        ),
    ],
)
@pytest.mark.parametrize("event_type", ["commit", "report"])
def test_repo_capture_never_records_remote_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remote: str,
    expected_remote: str,
    event_type: str,
) -> None:
    for name in ("LAB_TRACKER_REPO_CONFIG", "LAB_TRACKER_REPO_OUTBOX", "LAB_TRACKER_REPO_RUN_ID"):
        monkeypatch.delenv(name, raising=False)
    commit = _init_git_repo(tmp_path, remote)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")

    event, path, _action = capture_commit(config, event_type=event_type)
    note = render_event_note(event)
    metadata = event_metadata(
        event,
        source_uri=path.as_uri(),
        source_external_id=event_source_external_id(event),
        content_hash="0" * 64,
    )

    assert event["source"]["repo_remote_url"] == expected_remote
    assert SECRET not in path.read_text(encoding="utf-8")
    assert SECRET not in note
    assert SECRET not in json.dumps(metadata)
    assert metadata["repo_remote_url"] == expected_remote
    assert event_source_external_id(event) == f"{normalize_remote(expected_remote)}@{commit}"


def test_repo_render_sanitises_remote_of_already_queued_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Events queued by an older client carry the raw remote; draining them must
    # not upload the secret either.
    for name in ("LAB_TRACKER_REPO_CONFIG", "LAB_TRACKER_REPO_OUTBOX", "LAB_TRACKER_REPO_RUN_ID"):
        monkeypatch.delenv(name, raising=False)
    _init_git_repo(tmp_path, "https://github.com/lab/repo.git")
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    event, path, _action = capture_commit(config, event_type="report")
    event["source"]["repo_remote_url"] = f"https://{SECRET}@github.com/lab/repo.git"

    note = render_event_note(event)
    metadata = event_metadata(
        event,
        source_uri=path.as_uri(),
        source_external_id=event_source_external_id(event),
        content_hash="0" * 64,
    )

    assert SECRET not in note
    assert "- Remote: `https://github.com/lab/repo.git`" in note
    assert metadata["repo_remote_url"] == "https://github.com/lab/repo.git"
