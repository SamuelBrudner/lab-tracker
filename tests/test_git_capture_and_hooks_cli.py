"""Tests for outbox-backed git snapshot capture and post-commit hook enrollment."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from lab_tracker.repository_conventions import (
    REPOSITORY_CONVENTIONS_HASH_METADATA_KEY,
    REPOSITORY_CONVENTIONS_METADATA_KEY,
)
from lab_tracker_client import LTRecord
from lab_tracker_client import cli as lt_cli
from lab_tracker_client.hooks import HOOK_BLOCK_BEGIN, HOOK_BLOCK_END
from lab_tracker_client.repo import HOOK_BEGIN_MARKER as REPO_HOOK_BLOCK_BEGIN
from lab_tracker_client.repo import HOOK_END_MARKER as REPO_HOOK_BLOCK_END


def _clear_capture_env(monkeypatch) -> None:
    for name in (
        "LAB_TRACKER_BASE_URL",
        "LAB_TRACKER_MCP_BASE_URL",
        "LAB_TRACKER_ACCESS_TOKEN",
        "LAB_TRACKER_USERNAME",
        "LAB_TRACKER_PASSWORD",
        "LAB_TRACKER_PROJECT_ID",
        "LAB_TRACKER_WATCH_CONFIG",
        "LAB_TRACKER_WATCH_OUTBOX",
        "LAB_TRACKER_REPO_CONFIG",
        "LAB_TRACKER_REPO_OUTBOX",
        "LAB_TRACKER_HPC_CONFIG",
        "LAB_TRACKER_HPC_OUTBOX",
        "LAB_TRACKER_GIT_MAX_DIFF_LINES",
        "LAB_TRACKER_GIT_CONTEXT_LINES",
    ):
        monkeypatch.delenv(name, raising=False)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(tmp_path / "lt-home"))
    _clear_capture_env(monkeypatch)
    repo = tmp_path / "consumer-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "analysis.py").write_text("print('hello')\n", encoding="utf-8")
    _git(repo, "add", "analysis.py")
    _git(repo, "commit", "-q", "-m", "Add analysis script")
    return repo


def _commit_file(repo: Path, name: str, text: str, subject: str) -> str:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", subject)
    return _git(repo, "rev-parse", "HEAD").strip()


def _merge_commit(repo: Path) -> str:
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit_file(repo, "feature.py", "print('feature')\n", "feature work")
    _git(repo, "checkout", "-q", "-")
    _commit_file(repo, "main.py", "print('main')\n", "main work")
    _git(repo, "merge", "-q", "--no-ff", "-m", "Merge feature", "feature")
    return _git(repo, "rev-parse", "HEAD").strip()


class _FakeSyncClient:
    def __init__(self) -> None:
        self.uploads: list[dict[str, object]] = []
        self.draft_requests: list[str] = []

    def build_evidence_note_index(self, *, project_id: str, cache_dir: object = None) -> dict:
        return {}

    def _upload_note_file_payload(self, **kwargs: object) -> LTRecord:
        self.uploads.append(kwargs)
        return LTRecord({"note_id": f"note-{len(self.uploads)}"})

    def create_analysis_graph_draft(self, note_id: str) -> LTRecord:
        self.draft_requests.append(note_id)
        return LTRecord({"change_set_id": "cs-1"})

    def close(self) -> None:
        pass


def test_git_snapshot_queues_deterministic_event(git_repo, capsys) -> None:
    lt_cli.main(
        ["git", "snapshot", "--repo", str(git_repo), "--project", "p-1", "--no-sync"]
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["command"] == "git-snapshot"
    assert payload["queued"] is True
    assert payload["already_queued"] is False
    event_path = Path(payload["event_path"])
    assert event_path.exists()
    outbox = git_repo / ".lab-tracker" / "outbox" / "watch"
    assert event_path.parent == outbox.resolve()

    event = json.loads(event_path.read_text(encoding="utf-8"))
    assert event["sink"] == "staged-note"
    assert event["capture_kind"] == "git_commit"
    assert "request_draft" not in event["payload"]
    assert event["context"]["project_id"] == "p-1"
    assert event["source"]["provider"] == "git"
    assert event["source"]["git_commit"] == payload["commit"]
    body = event["payload"]["body"]
    assert "# Git Commit Evidence" in body
    assert "## Commit Metadata" in body
    assert "## Diff" in body
    assert "Add analysis script" in body

    lt_cli.main(
        ["git", "snapshot", "--repo", str(git_repo), "--project", "p-1", "--no-sync"]
    )
    repeat = json.loads(capsys.readouterr().out)
    assert repeat["already_queued"] is True
    assert repeat["event_path"] == payload["event_path"]
    assert len(list(outbox.glob("*.json"))) == 1


def test_agent_context_enrollment_flows_into_commit_snapshot_and_sync_metadata(
    git_repo,
    monkeypatch,
    capsys,
) -> None:
    (git_repo / "AGENTS.md").write_text(
        "# Analysis conventions\n\nCall genotype controls `parental_controls`.\n",
        encoding="utf-8",
    )
    _git(git_repo, "add", "AGENTS.md")
    _git(git_repo, "commit", "-q", "-m", "Add repository conventions")

    with pytest.raises(SystemExit, match="--yes"):
        lt_cli.main(["agent-context", "add", "AGENTS.md", "--repo", str(git_repo)])

    lt_cli.main(
        [
            "agent-context",
            "add",
            "AGENTS.md",
            "--repo",
            str(git_repo),
            "--yes",
        ]
    )
    added = json.loads(capsys.readouterr().out)
    assert added["action"] == "added"

    lt_cli.main(
        ["git", "snapshot", "--repo", str(git_repo), "--project", "p-1", "--no-sync"]
    )
    captured = json.loads(capsys.readouterr().out)
    event = json.loads(Path(captured["event_path"]).read_text(encoding="utf-8"))
    snapshot = json.loads(event["source"][REPOSITORY_CONVENTIONS_METADATA_KEY])
    assert event["source"][REPOSITORY_CONVENTIONS_HASH_METADATA_KEY] == (
        snapshot["snapshot_hash"]
    )
    assert snapshot["documents"][0]["paths"] == ["AGENTS.md"]
    assert "parental_controls" in snapshot["documents"][0]["content"]

    fake = _FakeSyncClient()
    monkeypatch.setattr(
        lt_cli.LabTracker,
        "from_env",
        classmethod(lambda cls: fake),  # noqa: ARG005
    )
    lt_cli.main(["outbox", "sync", "--repo", str(git_repo)])
    synced = json.loads(capsys.readouterr().out)
    assert synced["errors"] == []
    assert fake.draft_requests == []
    metadata = fake.uploads[0]["metadata"]
    assert metadata[REPOSITORY_CONVENTIONS_HASH_METADATA_KEY] == snapshot["snapshot_hash"]
    assert REPOSITORY_CONVENTIONS_METADATA_KEY in metadata


def test_git_snapshot_resolves_project_from_lt_ids(git_repo, capsys) -> None:
    (git_repo / "lt_ids.json").write_text(
        json.dumps({"project_id": "ids-project"}), encoding="utf-8"
    )
    lt_cli.main(["git", "snapshot", "--repo", str(git_repo), "--no-sync"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["project_id"] == "ids-project"


def test_git_snapshot_requires_a_project(git_repo) -> None:
    with pytest.raises(Exception, match="project"):
        lt_cli.main(["git", "snapshot", "--repo", str(git_repo), "--no-sync"])


def test_git_snapshot_offline_queues_then_later_sync_drains(
    git_repo, monkeypatch, capsys
) -> None:
    # Unreachable server: the snapshot must queue durably, record the failed
    # attempt inside the event file, and exit nonzero so the hook's one-line
    # warning is reachable (the commit itself is never blocked — post-commit
    # hooks are advisory and the hook body swallows the exit).
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "http://127.0.0.1:9")
    with pytest.raises(SystemExit):
        lt_cli.main(
            [
                "git",
                "snapshot",
                "--repo",
                str(git_repo),
                "--project",
                "p-1",
                "--request-draft",
            ]
        )
    payload = json.loads(capsys.readouterr().out)
    assert payload["queued"] is True
    assert payload["sync"]["errors"]
    event = json.loads(Path(payload["event_path"]).read_text(encoding="utf-8"))
    assert event["sync"]["status"] == "failed"
    assert event["sync"]["attempts"] == 1
    assert event["payload"]["request_draft"] is True

    # An unrelated, previously-synced watch event with a note but no change
    # set sits in the same outbox: the commit drain must NOT draft it.
    unrelated = {
        "version": 1,
        "event_id": "file-aaaa",
        "capture_id": "old-file",
        "capture_kind": "file",
        "adapter": "lt-watch-files",
        "sink": "staged-note",
        "observed_at": "2026-01-01T00:00:00+00:00",
        "source": {"provider": "local-folder", "external_id": "x"},
        "context": {"project_id": "p-1"},
        "artifacts": [],
        "metrics": {},
        "log_excerpt": "",
        "payload": {},
        "sync": {"status": "synced", "attempts": 1, "note_id": "old-note"},
    }
    (Path(payload["outbox"]) / "old-file.staged-note.file-aaaa.json").write_text(
        json.dumps(unrelated), encoding="utf-8"
    )

    # Server back (fake client): the SAME queued event drains into a staged
    # note plus its own requested draft; the unrelated event is left alone.
    fake = _FakeSyncClient()
    monkeypatch.setattr(
        lt_cli.LabTracker,
        "from_env",
        classmethod(lambda cls: fake),  # noqa: ARG005
    )
    lt_cli.main(
        [
            "git",
            "snapshot",
            "--repo",
            str(git_repo),
            "--project",
            "p-1",
            "--request-draft",
        ]
    )
    drained = json.loads(capsys.readouterr().out)
    assert drained["already_queued"] is True
    actions = {item["capture_id"]: item["action"] for item in drained["sync"]["results"]}
    assert actions["old-file"] == "skipped"
    git_result = next(
        item for item in drained["sync"]["results"] if item["capture_id"] != "old-file"
    )
    assert git_result["action"] == "imported"
    assert git_result["note_id"] == "note-1"
    assert git_result["change_set_id"] == "cs-1"
    assert fake.draft_requests == ["note-1"]

    upload = fake.uploads[0]
    assert b"# Git Commit Evidence" in upload["payload"]
    metadata = upload["metadata"]
    assert metadata["evidence_source_provider"] == "git"
    assert metadata["git_commit"] == drained["commit"]
    assert metadata["git_repository_name"] == "consumer-repo"

    event = json.loads(Path(drained["event_path"]).read_text(encoding="utf-8"))
    assert event["sync"]["status"] == "synced"
    assert event["sync"]["note_id"] == "note-1"


def test_git_snapshot_sync_failure_prints_actionable_stderr(
    git_repo, monkeypatch, capsys
) -> None:
    # Unreachable server: the commit is queued but not synced. The user must get
    # an actionable diagnostic on stderr (cause + queue location + drain command),
    # not the old opaque "did not fully sync; retries later" one-liner (GH #77).
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "http://127.0.0.1:9")
    with pytest.raises(SystemExit):
        lt_cli.main(["git", "snapshot", "--repo", str(git_repo), "--project", "p-1"])

    err = capsys.readouterr().err
    assert "did not fully sync" in err
    assert "failed to sync" in err  # names the cause
    assert "lt outbox sync" in err  # names the exact recovery command
    assert err.index("lt outbox sync") < err.index("lt outbox status")
    assert "timeout as ambiguous" in err
    assert "idempotent" in err
    assert "1 event" in err  # names the backlog size
    assert ".lab-tracker/outbox/watch" in err.replace("\\", "/")  # names where


def test_outbox_status_reports_queued_events_without_watch_json(git_repo, capsys) -> None:
    from lab_tracker_client import watch as watch_capture

    # Commit-snapshot capture queues an event but writes no watch.json.
    lt_cli.main(["git", "snapshot", "--repo", str(git_repo), "--project", "p-1", "--no-sync"])
    capsys.readouterr()

    # Precondition (GH #78): the watch loader hard-fails without watch.json, so
    # `lt watch status`/`sync` cannot drain commit-snapshot events...
    with pytest.raises(Exception, match="Watch config not found"):
        watch_capture.load_config(config_path=git_repo / ".lab-tracker" / "watch.json")

    # ...but `lt outbox status` reads the queue straight from the outbox layout.
    lt_cli.main(["outbox", "status", "--repo", str(git_repo)])
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "outbox-status"
    assert payload["total"] == 1
    assert payload["events"][0]["capture_kind"] == "git_commit"
    assert payload["events"][0]["sync_status"] == "pending"


def test_outbox_sync_drains_queue_without_watch_json(git_repo, monkeypatch, capsys) -> None:
    # Queue an event offline (no watch.json), leaving a backlog to replay.
    lt_cli.main(["git", "snapshot", "--repo", str(git_repo), "--project", "p-1", "--no-sync"])
    capsys.readouterr()

    fake = _FakeSyncClient()
    monkeypatch.setattr(
        lt_cli.LabTracker,
        "from_env",
        classmethod(lambda cls: fake),  # noqa: ARG005
    )

    lt_cli.main(["outbox", "sync", "--repo", str(git_repo)])
    payload = json.loads(capsys.readouterr().out)

    assert payload["command"] == "outbox-sync"
    assert payload["processed"] == 1
    assert not payload["errors"]
    assert payload["results"][0]["action"] == "imported"
    assert payload["results"][0]["note_id"] == "note-1"
    assert fake.draft_requests == []

    # The queue is now drained: a follow-up status shows the event synced.
    lt_cli.main(["outbox", "status", "--repo", str(git_repo)])
    status = json.loads(capsys.readouterr().out)
    assert status["events"][0]["sync_status"] == "synced"


def test_git_snapshot_reports_ignored_context_on_requeue(git_repo, capsys) -> None:
    lt_cli.main(["git", "snapshot", "--repo", str(git_repo), "--project", "p-1", "--no-sync"])
    capsys.readouterr()
    lt_cli.main(["git", "snapshot", "--repo", str(git_repo), "--project", "p-2", "--no-sync"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["already_queued"] is True
    assert payload["project_id"] == "p-1"
    assert payload["context_ignored"] is True


def test_git_snapshot_survives_malformed_watch_config(git_repo, capsys) -> None:
    config_dir = git_repo / ".lab-tracker"
    config_dir.mkdir()
    custom_outbox = git_repo / "custom-outbox"
    (config_dir / "watch.json").write_text(
        json.dumps(
            {
                "version": "bogus",
                "outbox": str(custom_outbox).replace("\\", "/"),
                "watches": [],
            }
        ),
        encoding="utf-8",
    )

    # config_error makes the exit nonzero (surfaced via the hook warning),
    # but the commit is still queued — into the SALVAGED configured outbox,
    # where the later, repaired sync will actually look.
    with pytest.raises(SystemExit):
        lt_cli.main(
            ["git", "snapshot", "--repo", str(git_repo), "--project", "p-1", "--no-sync"]
        )
    payload = json.loads(capsys.readouterr().out)
    assert payload["queued"] is True
    assert payload["config_error"]
    event_path = Path(payload["event_path"])
    assert event_path.exists()
    assert event_path.parent == custom_outbox.resolve()


def test_git_snapshot_strips_remote_credentials(git_repo, capsys) -> None:
    _git(git_repo, "remote", "add", "origin", "https://user:sekret@example.com/lab/repo.git")
    lt_cli.main(["git", "snapshot", "--repo", str(git_repo), "--project", "p-1", "--no-sync"])
    payload = json.loads(capsys.readouterr().out)

    raw = Path(payload["event_path"]).read_text(encoding="utf-8")
    assert "sekret" not in raw
    assert "https://example.com/lab/repo.git" in raw


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        # GitHub's bare-token form: the token is the whole userinfo.
        ("https://ghp_SEKRET123@github.com/lab/repo.git", "https://github.com/lab/repo.git"),
        ("https://oauth2:SEKRET123@gitlab.example.com/lab/repo.git",
         "https://gitlab.example.com/lab/repo.git"),
        ("https://github.com/lab/repo.git?access_token=SEKRET123",
         "https://github.com/lab/repo.git"),
        ("ssh://git:SEKRET123@example.com/lab/repo.git", "ssh://git@example.com/lab/repo.git"),
        ("git@github.com:lab/repo.git", "git@github.com:lab/repo.git"),
    ],
)
def test_git_snapshot_never_records_remote_credentials(
    git_repo, capsys, remote: str, expected: str
) -> None:
    from lab_tracker_client.watch import read_event

    _git(git_repo, "remote", "add", "origin", remote)
    lt_cli.main(["git", "snapshot", "--repo", str(git_repo), "--project", "p-1", "--no-sync"])
    payload = json.loads(capsys.readouterr().out)

    event_path = Path(payload["event_path"])
    assert "SEKRET123" not in event_path.read_text(encoding="utf-8")
    event = read_event(event_path)
    assert event["source"]["git_remote_origin_url"] == expected
    assert event["source"]["uri"] == expected
    assert f"- remote_origin: {expected}\n" in event["payload"]["body"]


def test_git_snapshot_fail_silent_swallows_errors(git_repo, capsys) -> None:
    # No project resolvable: fails loudly without the flag, silently with it.
    lt_cli.main(["git", "snapshot", "--repo", str(git_repo), "--no-sync", "--fail-silent"])
    assert capsys.readouterr().out == ""


def test_hooks_install_requires_consent(git_repo) -> None:
    with pytest.raises(SystemExit, match="--yes"):
        lt_cli.main(["hooks", "install", "--repo", str(git_repo)])
    assert not (git_repo / ".git" / "hooks" / "post-commit").exists()


def test_hooks_install_writes_posix_hook(git_repo, capsys) -> None:
    lt_cli.main(["hooks", "install", "--repo", str(git_repo), "--project", "p-9", "--yes"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "created"
    assert payload["project_id"] == "p-9"
    assert payload["created_config"].endswith("repo.json")

    hook_path = Path(payload["hook_path"])
    raw = hook_path.read_bytes()
    assert b"\r" not in raw
    content = raw.decode("utf-8")
    assert content.startswith("#!/usr/bin/env sh\n")
    # The single installer writes the `lt repo` block; GRAPH DRAFT is legacy.
    assert REPO_HOOK_BLOCK_BEGIN in content
    assert REPO_HOOK_BLOCK_END in content
    assert HOOK_BLOCK_BEGIN not in content
    assert "repo report >/dev/null" in content
    assert "git snapshot" not in content
    assert "--request-draft" not in content
    assert "LAB_TRACKER_REPO_HOOK_ENABLED" in content
    assert "LAB_TRACKER_GIT_CAPTURE_ENABLED" not in content
    # Suppress stdout only, not stderr, so lt's skip notice and its
    # cause+remediation line survive (GH #77); the fallback warning points at
    # the drain commands, in order.
    report_line = next(line for line in content.splitlines() if "repo report" in line)
    assert "2>&1" not in report_line
    assert "lt outbox sync" in content
    assert content.index("lt outbox sync") < content.index("lt outbox status")
    assert "--fail-silent" not in content
    # The project id lives in repo.json, pinned into the hook by path.
    assert "LAB_TRACKER_PROJECT_ID" not in content
    assert "repo.json" in content
    config = json.loads((git_repo / ".lab-tracker" / "repo.json").read_text(encoding="utf-8"))
    assert config["project_id"] == "p-9"
    if sys.platform != "win32":
        assert hook_path.stat().st_mode & 0o111

    lt_cli.main(["hooks", "install", "--repo", str(git_repo), "--project", "p-9", "--yes"])
    repeat = json.loads(capsys.readouterr().out)
    assert repeat["action"] == "updated"
    assert "created_config" not in repeat
    assert hook_path.read_bytes() == raw


def test_hooks_install_dry_run_writes_nothing(git_repo, capsys) -> None:
    # The old opt-out flag remains accepted (but hidden) so existing setup
    # automation does not fail now that capture-only is unconditional.
    lt_cli.main(
        [
            "hooks",
            "install",
            "--repo",
            str(git_repo),
            "--project",
            "p-1",
            "--no-request-draft",
            "--dry-run",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["action"] == "created"
    assert payload["diff"]
    assert REPO_HOOK_BLOCK_BEGIN in payload["diff"]
    assert "--request-draft" not in payload["diff"]
    # No repo.json yet: the preview names the config a real install creates.
    assert payload["would_create_config"].endswith("repo.json")
    assert "created_config" not in payload
    assert not (git_repo / ".lab-tracker" / "repo.json").exists()
    assert not (git_repo / ".git" / "hooks" / "post-commit").exists()


def test_hooks_install_normalizes_absolute_beads_hooks_path(git_repo, capsys) -> None:
    hooks_dir = git_repo / ".beads" / "hooks"
    hooks_dir.mkdir(parents=True)
    _git(git_repo, "config", "core.hooksPath", str(hooks_dir))

    lt_cli.main(["hooks", "install", "--repo", str(git_repo), "--project", "p-1", "--yes"])
    payload = json.loads(capsys.readouterr().out)

    assert Path(payload["hook_path"]).resolve() == (hooks_dir / "post-commit").resolve()
    assert payload["core_hooks_path"]["action"] == "normalized"
    assert payload["core_hooks_path"]["desired"] == ".beads/hooks"
    assert _git(git_repo, "config", "--get", "core.hooksPath").strip() == ".beads/hooks"
    assert (hooks_dir / "post-commit").exists()


def test_hooks_install_refuses_unmanaged_hook_without_force(git_repo, capsys) -> None:
    hook_path = git_repo / ".git" / "hooks" / "post-commit"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text("#!/bin/sh\necho custom-hook\n", encoding="utf-8")

    with pytest.raises(Exception, match="--force"):
        lt_cli.main(["hooks", "install", "--repo", str(git_repo), "--project", "p-1", "--yes"])
    assert "custom-hook" in hook_path.read_text(encoding="utf-8")
    assert REPO_HOOK_BLOCK_BEGIN not in hook_path.read_text(encoding="utf-8")
    # A refused install creates nothing, not even the config.
    assert not (git_repo / ".lab-tracker" / "repo.json").exists()

    lt_cli.main(
        ["hooks", "install", "--repo", str(git_repo), "--project", "p-1", "--yes", "--force"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "prepended"
    content = hook_path.read_text(encoding="utf-8")
    assert "echo custom-hook" in content
    assert REPO_HOOK_BLOCK_BEGIN in content
    # Ahead of the foreign body, so a trailing exit there cannot disable capture.
    assert content.index(REPO_HOOK_BLOCK_END) < content.index("echo custom-hook")


def test_hooks_install_upgrades_legacy_ps1_block_in_place(git_repo, capsys) -> None:
    hook_path = git_repo / ".git" / "hooks" / "post-commit"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    legacy = (
        "#!/usr/bin/env sh\n"
        f"{HOOK_BLOCK_BEGIN}\n"
        'LAB_TRACKER_ROOT="${LAB_TRACKER_ROOT:-C:/old/checkout}"\n'
        'LAB_TRACKER_BASE_URL="${LAB_TRACKER_BASE_URL:-http://192.168.1.5:8000}"\n'
        'LAB_TRACKER_PROJECT_ID="${LAB_TRACKER_PROJECT_ID:-legacy-project}"\n'
        '"$LAB_TRACKER_PYTHON" "$LAB_TRACKER_ROOT/scripts/create-analysis-graph-draft.py"\n'
        f"{HOOK_BLOCK_END}\n"
    )
    hook_path.write_text(legacy, encoding="utf-8", newline="\n")

    lt_cli.main(["hooks", "install", "--repo", str(git_repo), "--yes"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "migrated"
    # The legacy block was this repo's only project/URL binding; dropping it
    # would silently kill capture, so the baked defaults carry forward: the
    # project into the new repo.json, the base URL into the new block.
    assert payload["carried_project_id"] == "legacy-project"
    assert payload["carried_base_url"] == "http://192.168.1.5:8000"
    assert payload["created_config"].endswith("repo.json")
    config = json.loads((git_repo / ".lab-tracker" / "repo.json").read_text(encoding="utf-8"))
    assert config["project_id"] == "legacy-project"

    content = hook_path.read_text(encoding="utf-8")
    assert "create-analysis-graph-draft.py" not in content
    assert "git snapshot" not in content
    assert "repo report" in content
    assert "--request-draft" not in content
    assert "LAB_TRACKER_BASE_URL='http://192.168.1.5:8000'" in content
    assert HOOK_BLOCK_BEGIN not in content
    assert HOOK_BLOCK_END not in content
    assert content.count(REPO_HOOK_BLOCK_BEGIN) == 1
    assert content.count(REPO_HOOK_BLOCK_END) == 1

    lt_cli.main(["hooks", "status", "--repo", str(git_repo)])
    status = json.loads(capsys.readouterr().out)
    assert status["managed_block_present"] is True
    assert status["legacy_block_present"] is False
    assert status["baked_project_id"] == "legacy-project"
    assert status["baked_base_url"] == "http://192.168.1.5:8000"


def test_legacy_powershell_installer_is_capture_only() -> None:
    script = (
        Path(__file__).parents[1] / "scripts" / "install-git-graph-draft-hook.ps1"
    ).read_text(encoding="utf-8")

    assert "-m lab_tracker_client git snapshot" in script
    assert "create-analysis-graph-draft.py" not in script
    assert "--request-draft" not in script
    assert "could not create a proposal" not in script


def test_hooks_refuse_unpaired_markers(git_repo) -> None:
    hook_path = git_repo / ".git" / "hooks" / "post-commit"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(
        f"#!/bin/sh\n{HOOK_BLOCK_BEGIN}\nleftover user content\n", encoding="utf-8"
    )

    with pytest.raises(Exception, match="unpaired"):
        lt_cli.main(["hooks", "install", "--repo", str(git_repo), "--yes", "--force"])
    with pytest.raises(Exception, match="unpaired"):
        lt_cli.main(["hooks", "uninstall", "--repo", str(git_repo), "--yes"])
    assert "leftover user content" in hook_path.read_text(encoding="utf-8")


def test_hooks_uninstall_and_status(git_repo, capsys) -> None:
    lt_cli.main(["hooks", "install", "--repo", str(git_repo), "--project", "p-1", "--yes"])
    capsys.readouterr()

    lt_cli.main(["hooks", "status", "--repo", str(git_repo)])
    status = json.loads(capsys.readouterr().out)
    assert status["hook_present"] is True
    assert status["managed_block_present"] is True
    assert status["legacy_block_present"] is False
    assert status["markers_unpaired"] is False
    assert status["lt_path"]
    assert status["lt_path_exists"] is True
    assert status["baked_project_id"] == "p-1"
    assert status["config"].endswith("repo.json")

    with pytest.raises(SystemExit, match="--yes"):
        lt_cli.main(["hooks", "uninstall", "--repo", str(git_repo)])

    lt_cli.main(["hooks", "uninstall", "--repo", str(git_repo), "--yes"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "removed-hook-file"
    assert payload["removed_blocks"] == ["repo"]
    assert not Path(payload["hook_path"]).exists()

    lt_cli.main(["hooks", "status", "--repo", str(git_repo)])
    status = json.loads(capsys.readouterr().out)
    assert status["hook_present"] is False
    assert status["managed_block_present"] is False


def test_hooks_uninstall_preserves_unmanaged_content(git_repo, capsys) -> None:
    hook_path = git_repo / ".git" / "hooks" / "post-commit"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text("#!/bin/sh\necho custom-hook\n", encoding="utf-8")
    lt_cli.main(
        ["hooks", "install", "--repo", str(git_repo), "--project", "p-1", "--yes", "--force"]
    )
    capsys.readouterr()

    lt_cli.main(["hooks", "uninstall", "--repo", str(git_repo), "--yes"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "stripped-block"
    content = hook_path.read_text(encoding="utf-8")
    assert "echo custom-hook" in content
    assert REPO_HOOK_BLOCK_BEGIN not in content


def test_hooks_uninstall_strips_repo_and_legacy_blocks(git_repo, capsys) -> None:
    hook_path = git_repo / ".git" / "hooks" / "post-commit"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(
        "#!/usr/bin/env sh\n"
        f"{REPO_HOOK_BLOCK_BEGIN}\n: repo capture\n{REPO_HOOK_BLOCK_END}\n"
        f"{HOOK_BLOCK_BEGIN}\n: legacy snapshot\n{HOOK_BLOCK_END}\n",
        encoding="utf-8",
    )

    lt_cli.main(["hooks", "uninstall", "--repo", str(git_repo), "--yes"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["removed_blocks"] == ["repo", "graph-draft"]
    assert payload["action"] == "removed-hook-file"
    assert not hook_path.exists()


def test_hook_status_reports_lt_path_from_repo_block(git_repo, capsys) -> None:
    lt_cli.main(
        [
            "hooks",
            "install",
            "--repo",
            str(git_repo),
            "--project",
            "p-9",
            "--lt-path",
            "/opt/my tools/lt",
            "--yes",
        ]
    )
    capsys.readouterr()

    lt_cli.main(["hooks", "status", "--repo", str(git_repo)])
    status = json.loads(capsys.readouterr().out)

    assert status["lt_path"] == "/opt/my tools/lt"
    assert status["lt_path_exists"] is False
    assert status["managed_block_present"] is True
    assert status["baked_project_id"] == "p-9"
    assert status["baked_base_url"] is None


# --- cross-adapter coexistence (lt-81s6.17) ----------------------------------


def test_git_snapshot_external_id_uses_shared_git_identity(git_repo, capsys) -> None:
    """Snapshot evidence must carry <normalized-remote>@<commit>, not a bare SHA,
    so hook-, CLI-, and CI-captured evidence for one commit share one identity."""

    from lab_tracker_client.repo import normalize_remote

    _git(git_repo, "remote", "add", "origin", "https://example.com/org/repo.git")
    lt_cli.main(
        ["git", "snapshot", "--repo", str(git_repo), "--project", "p-1", "--no-sync"]
    )
    payload = json.loads(capsys.readouterr().out)
    event = json.loads(Path(payload["event_path"]).read_text(encoding="utf-8"))

    expected = f"{normalize_remote('https://example.com/org/repo.git')}@{payload['commit']}"
    assert event["source"]["external_id"] == expected
    assert expected == f"example.com/org/repo@{payload['commit']}"


def test_git_snapshot_external_id_falls_back_to_local_without_remote(
    git_repo, capsys
) -> None:
    lt_cli.main(
        ["git", "snapshot", "--repo", str(git_repo), "--project", "p-1", "--no-sync"]
    )
    payload = json.loads(capsys.readouterr().out)
    event = json.loads(Path(payload["event_path"]).read_text(encoding="utf-8"))

    assert event["source"]["external_id"] == f"local@{payload['commit']}"


def test_hooks_install_updates_an_existing_repo_block_in_place(git_repo, capsys) -> None:
    """The `lt repo` block is the installer's own block now: re-runs update it."""

    lt_cli.main(
        [
            "hooks",
            "install",
            "--repo",
            str(git_repo),
            "--project",
            "p-1",
            "--lt-path",
            "/old/lt",
            "--yes",
        ]
    )
    capsys.readouterr()

    lt_cli.main(["hooks", "install", "--repo", str(git_repo), "--lt-path", "/new/lt", "--yes"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["action"] == "updated"
    assert payload["lt_path"] == "/new/lt"
    assert payload["project_id"] == "p-1"
    content = (git_repo / ".git" / "hooks" / "post-commit").read_text(encoding="utf-8")
    assert "LT='/new/lt'" in content
    assert "/old/lt" not in content
    assert content.count(REPO_HOOK_BLOCK_BEGIN) == 1


def test_hooks_install_requires_a_project_when_no_repo_json(git_repo) -> None:
    from lab_tracker_client.client import LTValidationError
    from lab_tracker_client.hooks import install_hook

    with pytest.raises(LTValidationError, match="lt repo init"):
        install_hook(repo=git_repo, lt_path="/opt/lt")

    assert not (git_repo / ".git" / "hooks" / "post-commit").exists()
    assert not (git_repo / ".lab-tracker" / "repo.json").exists()


def test_hooks_install_refuses_conflicting_project_id(git_repo, monkeypatch, capsys) -> None:
    from lab_tracker_client.client import LTValidationError
    from lab_tracker_client.hooks import install_hook

    monkeypatch.chdir(git_repo)
    lt_cli.main(["repo", "init", "--project", "p-1"])
    capsys.readouterr()

    with pytest.raises(LTValidationError, match="conflicts"):
        install_hook(repo=git_repo, project_id="p-2", lt_path="/opt/lt")

    assert not (git_repo / ".git" / "hooks" / "post-commit").exists()
    # The same project (or none) is fine: repo.json is the source of truth.
    payload = install_hook(repo=git_repo, project_id="p-1", lt_path="/opt/lt")
    assert payload["action"] == "created"
    assert payload["project_id"] == "p-1"
    assert "created_config" not in payload


@pytest.mark.parametrize(
    "unsafe",
    ["p}rm", 'p"x', "p$(touch pwned)", "p`id`", "p\\x", "it's"],
)
@pytest.mark.parametrize("field", ["base_url", "lt_path"])
def test_hooks_install_single_quotes_baked_values_so_they_cannot_escape(
    git_repo, field: str, unsafe: str
) -> None:
    from lab_tracker_client.hooks import install_hook

    kwargs = {"project_id": "p-1", "base_url": "http://lab:8000", "lt_path": "/opt/lt"}
    kwargs[field] = unsafe

    install_hook(repo=git_repo, **kwargs)

    content = (git_repo / ".git" / "hooks" / "post-commit").read_text(encoding="utf-8")
    baked = unsafe.replace("\\", "/") if field == "lt_path" else unsafe  # lt paths fold to "/"
    quoted = "'" + baked.replace("'", "'\\''") + "'"
    prefix = "LT=" if field == "lt_path" else "LAB_TRACKER_BASE_URL="
    # The whole value sits inside single quotes to the end of the line, so sh
    # can neither expand nor execute any character of it.
    assert f"{prefix}{quoted}\n" in content


@pytest.mark.parametrize("field", ["base_url", "lt_path"])
def test_hooks_install_refuses_newlines_in_baked_values(git_repo, field: str) -> None:
    from lab_tracker_client.client import LTValidationError
    from lab_tracker_client.hooks import install_hook

    kwargs = {"project_id": "p-1", "base_url": "http://lab:8000", "lt_path": "/opt/lt"}
    kwargs[field] = "p\nx"

    with pytest.raises(LTValidationError, match="newlines"):
        install_hook(repo=git_repo, **kwargs)

    assert not (git_repo / ".git" / "hooks" / "post-commit").exists()
    assert not (git_repo / ".lab-tracker" / "repo.json").exists()


@pytest.mark.parametrize(
    ("line", "flag"),
    [
        ('LAB_TRACKER_PROJECT_ID="${LAB_TRACKER_PROJECT_ID:-p$(id)}"', "--project"),
        ('LAB_TRACKER_BASE_URL="${LAB_TRACKER_BASE_URL:-http://lab`id`}"', "--base-url"),
    ],
)
def test_hooks_install_names_an_unsafe_value_carried_from_a_legacy_block(
    git_repo, line: str, flag: str
) -> None:
    from lab_tracker_client.client import LTValidationError
    from lab_tracker_client.hooks import install_hook

    hook_path = git_repo / ".git" / "hooks" / "post-commit"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    legacy = f"#!/usr/bin/env sh\n{HOOK_BLOCK_BEGIN}\n{line}\n{HOOK_BLOCK_END}\n"
    hook_path.write_text(legacy, encoding="utf-8", newline="\n")

    # The user never passed this value: say where it came from and how to override it.
    with pytest.raises(LTValidationError, match="carried forward") as excinfo:
        install_hook(repo=git_repo, lt_path="/opt/lt")

    assert flag in str(excinfo.value)
    assert hook_path.read_text(encoding="utf-8") == legacy


def test_hooks_install_keeps_spaces_in_baked_values(git_repo) -> None:
    from lab_tracker_client.hooks import install_hook

    install_hook(repo=git_repo, project_id="p-1", lt_path="/opt/my tools/lt")

    content = (git_repo / ".git" / "hooks" / "post-commit").read_text(encoding="utf-8")
    assert "LT='/opt/my tools/lt'" in content


# --- commit filter + deprecation on the legacy snapshot path -------------------


def test_git_snapshot_skips_merge_commits_by_default_and_counts_them(git_repo, capsys) -> None:
    merge = _merge_commit(git_repo)

    # No --no-sync: a skipped commit queues nothing, so there is nothing to drain.
    lt_cli.main(["git", "snapshot", "--repo", str(git_repo), "--project", "p-1"])
    out, err = capsys.readouterr()

    payload = json.loads(out)
    assert payload["skipped"] is True
    assert payload["queued"] is False
    assert payload["skip_reason"] == "merge_commit"
    assert payload["commit"] == merge
    assert payload["skipped_total"] == 1
    assert payload["deprecated"] is True
    assert "sync" not in payload
    assert "skipped commit" in err
    assert "merge_commit" in err
    outbox = git_repo / ".lab-tracker" / "outbox" / "watch"
    assert (outbox / ".skipped-commits.jsonl").exists()
    assert list(outbox.glob("*.json")) == []

    lt_cli.main(["outbox", "status", "--repo", str(git_repo)])
    status = json.loads(capsys.readouterr().out)
    assert status["skipped_commits"] == 1
    assert status["total"] == 0
    assert status["adapters"][0]["adapter"] == "watch"
    assert status["adapters"][0]["skipped_commits"] == 1


def test_git_snapshot_honours_repo_json_commit_filter(git_repo, capsys) -> None:
    (git_repo / ".lab-tracker").mkdir()
    (git_repo / ".lab-tracker" / "repo.json").write_text(
        json.dumps(
            {
                "version": 1,
                "project_id": "p-1",
                "outbox": ".lab-tracker/outbox/repo",
                "commit_filter": {"skip_wip": True},
            }
        ),
        encoding="utf-8",
    )
    _commit_file(git_repo, "analysis.py", "print('wip')\n", "wip: tinkering")

    lt_cli.main(["git", "snapshot", "--repo", str(git_repo), "--project", "p-1", "--no-sync"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["skipped"] is True
    assert payload["skip_reason"] == "wip_subject"
    # The skip is logged in the outbox that would have queued the event.
    assert (git_repo / ".lab-tracker" / "outbox" / "watch" / ".skipped-commits.jsonl").exists()


def test_git_snapshot_force_capture_overrides_filter(git_repo, capsys) -> None:
    merge = _merge_commit(git_repo)

    lt_cli.main(
        [
            "git",
            "snapshot",
            "--repo",
            str(git_repo),
            "--project",
            "p-1",
            "--no-sync",
            "--force-capture",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["queued"] is True
    assert "skipped" not in payload
    assert payload["commit"] == merge
    assert Path(payload["event_path"]).exists()


def test_git_snapshot_prints_deprecation_notice(git_repo, capsys) -> None:
    lt_cli.main(["git", "snapshot", "--repo", str(git_repo), "--project", "p-1", "--no-sync"])
    out, err = capsys.readouterr()

    assert json.loads(out)["deprecated"] is True
    assert "deprecated" in err
    assert "lt hooks install --yes" in err


# --- lt outbox covers every adapter -------------------------------------------


def _queue_watch_and_repo_events(git_repo: Path, monkeypatch, capsys) -> None:
    lt_cli.main(["git", "snapshot", "--repo", str(git_repo), "--project", "p-1", "--no-sync"])
    capsys.readouterr()
    monkeypatch.chdir(git_repo)
    lt_cli.main(["repo", "init", "--project", "p-1"])
    capsys.readouterr()
    lt_cli.main(["repo", "report", "--no-sync"])
    capsys.readouterr()


def test_outbox_status_aggregates_every_adapter(git_repo, monkeypatch, capsys) -> None:
    _queue_watch_and_repo_events(git_repo, monkeypatch, capsys)

    lt_cli.main(["outbox", "status", "--repo", str(git_repo)])
    payload = json.loads(capsys.readouterr().out)

    assert payload["command"] == "outbox-status"
    assert [item["adapter"] for item in payload["adapters"]] == ["watch", "repo", "hpc"]
    assert payload["total"] == 2
    assert payload["pending"] == 2
    assert payload["skipped_commits"] == 0
    assert payload["config_errors"] == []
    assert len(payload["events"]) == 2
    assert payload["events"][0]["capture_kind"] == "git_commit"
    assert payload["events"][1]["event_type"] == "commit"
    watch, repo, hpc = payload["adapters"]
    assert watch["total"] == 1
    assert repo["total"] == 1
    assert repo["config"].endswith("repo.json")
    assert hpc["total"] == 0
    assert hpc["config"] is None
    assert not (git_repo / ".lab-tracker" / "outbox" / "hpc").exists()


def test_outbox_sync_drains_watch_and_repo_outboxes_and_leaves_absent_hpc_alone(
    git_repo, monkeypatch, capsys
) -> None:
    _queue_watch_and_repo_events(git_repo, monkeypatch, capsys)
    fake = _FakeSyncClient()
    monkeypatch.setattr(
        lt_cli.LabTracker,
        "from_env",
        classmethod(lambda cls: fake),  # noqa: ARG005
    )

    lt_cli.main(["outbox", "sync", "--repo", str(git_repo)])
    payload = json.loads(capsys.readouterr().out)

    assert payload["command"] == "outbox-sync"
    assert payload["processed"] == 2
    assert payload["errors"] == []
    assert [item["adapter"] for item in payload["adapters"]] == ["watch", "repo", "hpc"]
    assert payload["adapters"][2]["skipped"] == "absent"
    assert not (git_repo / ".lab-tracker" / "outbox" / "hpc").exists()
    assert {item["action"] for item in payload["results"]} == {"imported"}
    assert len(fake.uploads) == 2
    assert fake.draft_requests == []

    lt_cli.main(["outbox", "status", "--repo", str(git_repo)])
    status = json.loads(capsys.readouterr().out)
    assert status["synced"] == 2
    assert status["pending"] == 0
