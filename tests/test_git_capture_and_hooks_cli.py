"""Tests for outbox-backed git snapshot capture and post-commit hook enrollment."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from lab_tracker_client import LTRecord
from lab_tracker_client import cli as lt_cli
from lab_tracker_client.hooks import HOOK_BLOCK_BEGIN, HOOK_BLOCK_END


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


class _FakeSyncClient:
    def __init__(self) -> None:
        self.uploads: list[dict[str, object]] = []
        self.draft_requests: list[str] = []

    def build_evidence_note_index(self, *, project_id: str) -> dict:
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

    hook_path = Path(payload["hook_path"])
    raw = hook_path.read_bytes()
    assert b"\r" not in raw
    content = raw.decode("utf-8")
    assert content.startswith("#!/usr/bin/env sh\n")
    assert HOOK_BLOCK_BEGIN in content
    assert HOOK_BLOCK_END in content
    assert "git snapshot --request-draft >/dev/null" in content
    assert "--fail-silent" not in content
    assert 'LAB_TRACKER_PROJECT_ID="${LAB_TRACKER_PROJECT_ID:-p-9}"' in content
    assert "\\" not in content.split('LAB_TRACKER_LT:-', 1)[1].split("}", 1)[0]
    if sys.platform != "win32":
        assert hook_path.stat().st_mode & 0o111

    lt_cli.main(["hooks", "install", "--repo", str(git_repo), "--project", "p-9", "--yes"])
    repeat = json.loads(capsys.readouterr().out)
    assert repeat["action"] == "updated"
    assert hook_path.read_bytes() == raw


def test_hooks_install_dry_run_writes_nothing(git_repo, capsys) -> None:
    lt_cli.main(["hooks", "install", "--repo", str(git_repo), "--dry-run"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["diff"]
    assert not (git_repo / ".git" / "hooks" / "post-commit").exists()


def test_hooks_install_refuses_unmanaged_hook_without_force(git_repo, capsys) -> None:
    hook_path = git_repo / ".git" / "hooks" / "post-commit"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text("#!/bin/sh\necho custom-hook\n", encoding="utf-8")

    with pytest.raises(Exception, match="--force"):
        lt_cli.main(["hooks", "install", "--repo", str(git_repo), "--yes"])
    assert "custom-hook" in hook_path.read_text(encoding="utf-8")
    assert HOOK_BLOCK_BEGIN not in hook_path.read_text(encoding="utf-8")

    lt_cli.main(["hooks", "install", "--repo", str(git_repo), "--yes", "--force"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "appended"
    content = hook_path.read_text(encoding="utf-8")
    assert "echo custom-hook" in content
    assert HOOK_BLOCK_BEGIN in content


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
    assert payload["action"] == "updated"
    # The legacy block was this repo's only project/URL binding; dropping it
    # would silently kill capture, so the baked defaults carry forward.
    assert payload["carried_project_id"] == "legacy-project"
    assert payload["carried_base_url"] == "http://192.168.1.5:8000"

    content = hook_path.read_text(encoding="utf-8")
    assert "create-analysis-graph-draft.py" not in content
    assert "git snapshot" in content
    assert 'LAB_TRACKER_PROJECT_ID="${LAB_TRACKER_PROJECT_ID:-legacy-project}"' in content
    assert 'LAB_TRACKER_BASE_URL="${LAB_TRACKER_BASE_URL:-http://192.168.1.5:8000}"' in content
    assert content.count(HOOK_BLOCK_BEGIN) == 1
    assert content.count(HOOK_BLOCK_END) == 1

    lt_cli.main(["hooks", "status", "--repo", str(git_repo)])
    status = json.loads(capsys.readouterr().out)
    assert status["baked_project_id"] == "legacy-project"


def test_setup_switch_server_updates_existing_managed_hook(git_repo, capsys) -> None:
    lt_cli.main(
        [
            "hooks",
            "install",
            "--repo",
            str(git_repo),
            "--project",
            "p-1",
            "--base-url",
            "http://old-lab:8000",
            "--yes",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    hook_path = Path(payload["hook_path"])

    lt_cli.main(
        [
            "setup",
            "switch-server",
            "--target",
            str(git_repo),
            "--base-url",
            "http://new-lab:8000",
            "--dry-run",
            "--no-repo",
        ]
    )
    dry_payload = json.loads(capsys.readouterr().out)
    assert "http://new-lab:8000" in dry_payload["hooks"]["diff"]
    assert "http://old-lab:8000" in hook_path.read_text(encoding="utf-8")

    lt_cli.main(
        [
            "setup",
            "switch-server",
            "--target",
            str(git_repo),
            "--base-url",
            "http://new-lab:8000",
            "--yes",
            "--no-repo",
        ]
    )
    apply_payload = json.loads(capsys.readouterr().out)
    assert apply_payload["hooks"]["action"] == "updated"
    content = hook_path.read_text(encoding="utf-8")
    assert 'LAB_TRACKER_BASE_URL="${LAB_TRACKER_BASE_URL:-http://new-lab:8000}"' in content
    assert 'LAB_TRACKER_PROJECT_ID="${LAB_TRACKER_PROJECT_ID:-p-1}"' in content


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
    lt_cli.main(["hooks", "install", "--repo", str(git_repo), "--yes"])
    capsys.readouterr()

    lt_cli.main(["hooks", "status", "--repo", str(git_repo)])
    status = json.loads(capsys.readouterr().out)
    assert status["hook_present"] is True
    assert status["managed_block_present"] is True
    assert status["lt_path"]
    assert status["lt_path_exists"] is True

    with pytest.raises(SystemExit, match="--yes"):
        lt_cli.main(["hooks", "uninstall", "--repo", str(git_repo)])

    lt_cli.main(["hooks", "uninstall", "--repo", str(git_repo), "--yes"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "removed-hook-file"
    assert not Path(payload["hook_path"]).exists()

    lt_cli.main(["hooks", "status", "--repo", str(git_repo)])
    status = json.loads(capsys.readouterr().out)
    assert status["hook_present"] is False
    assert status["managed_block_present"] is False


def test_hooks_uninstall_preserves_unmanaged_content(git_repo, capsys) -> None:
    hook_path = git_repo / ".git" / "hooks" / "post-commit"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text("#!/bin/sh\necho custom-hook\n", encoding="utf-8")
    lt_cli.main(["hooks", "install", "--repo", str(git_repo), "--yes", "--force"])
    capsys.readouterr()

    lt_cli.main(["hooks", "uninstall", "--repo", str(git_repo), "--yes"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "stripped-block"
    content = hook_path.read_text(encoding="utf-8")
    assert "echo custom-hook" in content
    assert HOOK_BLOCK_BEGIN not in content
