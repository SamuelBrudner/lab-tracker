from __future__ import annotations

import hashlib
import subprocess

import httpx
import pytest

from lab_tracker_client import LabTracker
from lab_tracker_client.client import LTValidationError
from lab_tracker_client.repo import (
    artifact_from_path,
    capture_commit,
    environment_fingerprint,
    event_metadata,
    event_source_external_id,
    init_config,
    load_config,
    make_event,
    normalize_remote,
    outbox_status,
    read_event,
    render_event_note,
    sync_outbox,
)


def _json_response(status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def _clear_repo_env(monkeypatch) -> None:
    monkeypatch.delenv("LAB_TRACKER_REPO_CONFIG", raising=False)
    monkeypatch.delenv("LAB_TRACKER_REPO_OUTBOX", raising=False)
    monkeypatch.delenv("LAB_TRACKER_REPO_RUN_ID", raising=False)
    monkeypatch.delenv("LAB_TRACKER_CONTAINER_REF", raising=False)


def _init_git_repo(path) -> str:
    """Create a real git repo with one commit; return the commit SHA."""

    def run(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    run("init", "-q")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    run("config", "commit.gpgsign", "false")
    run("remote", "add", "origin", "https://example.com/org/repo.git")
    # The .lab-tracker capture dir is host-local scratch; users gitignore it, so
    # writing the config there must not dirty the working tree.
    (path / ".gitignore").write_text(".lab-tracker/\n", encoding="utf-8")
    (path / "analysis.py").write_text("print('hi')\n", encoding="utf-8")
    run("add", ".gitignore", "analysis.py")
    run("commit", "-q", "-m", "initial analysis")
    return run("rev-parse", "HEAD")


# --- config + capture -----------------------------------------------------


def test_init_config_and_capture_commit(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    commit = _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    config = init_config(project_id="project-1", default_question_id="question-1")
    loaded = load_config()
    event, path, action = capture_commit(loaded, tags=["pilot"])

    assert config.config_path == tmp_path / ".lab-tracker" / "repo.json"
    assert loaded.outbox_path() == tmp_path / ".lab-tracker" / "outbox" / "repo"
    assert path.exists()
    assert event["event_type"] == "commit"
    assert event["source"]["git_commit"] == commit
    assert event["source"]["git_branch"] in {"main", "master"}
    assert event["source"]["repo_remote_url"] == "https://example.com/org/repo.git"
    assert event["source"]["git_dirty"] is False
    assert event["question_id"] == "question-1"
    assert read_event(path)["sync"]["status"] == "pending"
    assert outbox_status(loaded.outbox_path())["pending"] == 1


def test_capture_commit_is_idempotent_per_commit(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")

    _event_a, path_a, action_a = capture_commit(config)
    _event_b, path_b, action_b = capture_commit(config)

    # A repeated post-commit hook for the same commit must not pile up events.
    assert path_a == path_b
    assert (action_a, action_b) == ("captured", "unchanged")
    assert len(list(config.outbox_path().glob("*.json"))) == 1


def test_capture_commit_annotation_updates_pending_event(tmp_path, monkeypatch) -> None:
    """--summary/--question on an already-captured commit must not be dropped."""

    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")

    _hook_event, path, _action = capture_commit(config)  # hook-style default capture
    annotated, annotated_path, action = capture_commit(
        config, summary="IMPORTANT user summary", question_id="q-42", tags=["t1"]
    )

    assert action == "updated"
    assert annotated_path == path
    on_disk = read_event(path)
    assert on_disk["summary"] == "IMPORTANT user summary"
    assert on_disk["question_id"] == "q-42"
    assert on_disk["tags"] == ["t1"]
    assert on_disk["sync"]["status"] == "pending"
    assert annotated["summary"] == "IMPORTANT user summary"
    assert len(list(config.outbox_path().glob("*.json"))) == 1


def test_capture_commit_hook_refire_preserves_annotation(tmp_path, monkeypatch) -> None:
    """A bare hook re-fire must not revert an earlier annotation to defaults."""

    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    capture_commit(config, summary="IMPORTANT user summary", question_id="q-42")

    _event, path, action = capture_commit(config)  # bare, hook-style

    assert action == "unchanged"
    on_disk = read_event(path)
    assert on_disk["summary"] == "IMPORTANT user summary"
    assert on_disk["question_id"] == "q-42"


def test_capture_commit_after_sync_writes_new_event(tmp_path, monkeypatch) -> None:
    """Annotating a synced commit must not desync the staged note."""

    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    _event, path, _action = capture_commit(config)
    synced = read_event(path)
    synced["sync"] = {"status": "synced", "attempts": 1, "note_id": "note-1"}
    path.write_text(__import__("json").dumps(synced), encoding="utf-8")

    annotated, new_path, action = capture_commit(config, summary="Post-sync annotation")

    assert action == "recaptured"
    assert new_path != path
    assert annotated["summary"] == "Post-sync annotation"
    assert read_event(path)["sync"]["note_id"] == "note-1"  # original untouched
    assert len(list(config.outbox_path().glob("*.json"))) == 2


def test_capture_commit_records_dirty_tree(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    (tmp_path / "analysis.py").write_text("print('changed')\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")

    event, _path, _action = capture_commit(config)

    assert event["source"]["git_dirty"] is True


def test_render_event_note_contains_commit_state(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    commit = _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    event, _path, _action = capture_commit(config)

    note = render_event_note(event)

    assert commit in note
    assert "https://example.com/org/repo.git" in note
    assert "## Repository State" in note
    assert "## Research Context" in note


# --- external id / normalization ------------------------------------------


def test_normalize_remote_variants() -> None:
    expected = "example.com/org/repo"
    assert normalize_remote("https://example.com/org/repo.git") == expected
    assert normalize_remote("git@example.com:org/repo.git") == expected
    assert normalize_remote("ssh://git@example.com/org/repo") == expected
    assert normalize_remote("") == ""


def test_event_source_external_id_is_remote_at_commit(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    commit = _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    event, _path, _action = capture_commit(config)

    assert event_source_external_id(event) == f"example.com/org/repo@{commit}"


def test_make_event_without_git_falls_back(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    monkeypatch.chdir(tmp_path)  # not a git repo
    config = init_config(project_id="project-1")

    event = make_event(config, event_type="commit")

    # No commit to pin, but the event is still valid and carries a run id.
    assert event["source"].get("git_commit", "") == ""
    assert event["run_id"]
    assert event["source"]["git_dirty"] is False


# --- artifacts --------------------------------------------------------------


def test_artifact_from_path_fingerprints_file(tmp_path) -> None:
    out = tmp_path / "results" / "summary.csv"
    out.parent.mkdir()
    out.write_bytes(b"a,b\n1,2\n")

    artifact = artifact_from_path(out, root=tmp_path, summary="Run output.")

    assert artifact["uri"] == out.as_uri()
    assert artifact["title"] == "results/summary.csv"
    assert artifact["kind"] == "file"
    assert artifact["summary"] == "Run output."
    assert artifact["content_hash"] == "sha256:" + hashlib.sha256(b"a,b\n1,2\n").hexdigest()
    assert artifact["size_bytes"] == len(b"a,b\n1,2\n")


def test_artifact_from_path_missing_file_raises(tmp_path) -> None:

    with pytest.raises(LTValidationError, match="not found"):
        artifact_from_path(tmp_path / "gone.csv")


def test_finish_events_stay_distinct_per_run(tmp_path, monkeypatch) -> None:
    """Two runs at the same commit must not merge into one finish event."""

    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")

    _e1, path_1, action_1 = capture_commit(config, event_type="finish", summary="Run one.")
    _e2, path_2, action_2 = capture_commit(config, event_type="finish", summary="Run two.")

    assert (action_1, action_2) == ("captured", "captured")
    assert path_1 != path_2
    assert read_event(path_1)["summary"] == "Run one."
    assert read_event(path_2)["summary"] == "Run two."


def test_capture_with_artifacts_renders_pointer_section(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    (tmp_path / "out.csv").write_bytes(b"x\n")
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")

    event, _path, _action = capture_commit(
        config,
        event_type="finish",
        artifacts=[artifact_from_path(tmp_path / "out.csv", root=tmp_path)],
    )
    note = render_event_note(event)

    assert event["artifacts"][0]["content_hash"].startswith("sha256:")
    assert "## Artifact Pointers" in note
    assert "out.csv" in note


# --- environment fingerprint -------------------------------------------------


def test_environment_fingerprint_hashes_lockfiles(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("LAB_TRACKER_CONTAINER_REF", raising=False)
    (tmp_path / "uv.lock").write_text("locked deps v1\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")

    first = environment_fingerprint(tmp_path)
    unchanged = environment_fingerprint(tmp_path)
    (tmp_path / "uv.lock").write_text("locked deps v2\n", encoding="utf-8")
    changed = environment_fingerprint(tmp_path)

    assert first["repo_environment_hash"].startswith("sha256:")
    assert first["repo_environment_files"] == "uv.lock,pyproject.toml"
    assert first["repo_environment_python"]
    assert unchanged["repo_environment_hash"] == first["repo_environment_hash"]
    assert changed["repo_environment_hash"] != first["repo_environment_hash"]


def test_environment_fingerprint_empty_without_lockfiles(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("LAB_TRACKER_CONTAINER_REF", raising=False)

    assert environment_fingerprint(tmp_path) == {}


def test_environment_fingerprint_includes_container_ref(tmp_path, monkeypatch) -> None:
    (tmp_path / "requirements.txt").write_text("numpy\n", encoding="utf-8")
    monkeypatch.delenv("LAB_TRACKER_CONTAINER_REF", raising=False)
    bare = environment_fingerprint(tmp_path)
    monkeypatch.setenv("LAB_TRACKER_CONTAINER_REF", "docker.io/lab/analysis:1.2")
    with_container = environment_fingerprint(tmp_path)

    assert with_container["repo_environment_container"] == "docker.io/lab/analysis:1.2"
    assert with_container["repo_environment_hash"] != bare["repo_environment_hash"]


def test_capture_records_environment_in_event_and_metadata(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    monkeypatch.delenv("LAB_TRACKER_CONTAINER_REF", raising=False)
    _init_git_repo(tmp_path)
    (tmp_path / "uv.lock").write_text("locked deps\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")

    event, _path, _action = capture_commit(config)
    metadata = event_metadata(
        event,
        source_uri="file:///outbox/event.json",
        source_external_id="example.com/org/repo@abc",
        content_hash="deadbeef",
    )

    assert event["environment"]["repo_environment_hash"].startswith("sha256:")
    assert metadata["repo_environment_hash"] == event["environment"]["repo_environment_hash"]
    assert metadata["repo_environment_files"] == "uv.lock"


def test_lockfile_change_updates_pending_capture(tmp_path, monkeypatch) -> None:
    """An environment change at the same commit refreshes the pending event."""

    _clear_repo_env(monkeypatch)
    monkeypatch.delenv("LAB_TRACKER_CONTAINER_REF", raising=False)
    _init_git_repo(tmp_path)
    (tmp_path / "uv.lock").write_text("locked deps v1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    _e1, path, first_action = capture_commit(config)

    (tmp_path / "uv.lock").write_text("locked deps v2\n", encoding="utf-8")
    event, same_path, action = capture_commit(config)

    assert (first_action, action) == ("captured", "updated")
    assert same_path == path
    assert read_event(path)["environment"] == event["environment"]


# --- sync -----------------------------------------------------------------


def test_sync_outbox_uploads_staged_note_and_requests_draft(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    _event, path, _action = capture_commit(config, summary="Pinned the analysis commit.")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/notes":
            return _json_response(
                200, {"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}}
            )
        if request.method == "POST" and request.url.path == "/notes/upload-file":
            body = request.content
            assert b"Repo commit" in body
            assert b"Pinned the analysis commit." in body
            assert b"evidence_source_provider" in body
            assert b"repo_git_commit" in body
            return _json_response(
                201,
                {"data": {"note_id": "note-repo", "project_id": "project-1", "status": "staged"}},
            )
        draft_path = "/notes/note-repo/analysis-graph-drafts"
        if request.method == "POST" and request.url.path == draft_path:
            return _json_response(
                201, {"data": {"change_set_id": "draft-1", "project_id": "project-1"}}
            )
        return _json_response(500, {"error": {"message": "unexpected request"}})

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        summary = sync_outbox(lt, config, request_draft=True)

    synced = read_event(path)
    assert summary["errors"] == []
    assert summary["results"][0]["note_id"] == "note-repo"
    assert summary["results"][0]["change_set_id"] == "draft-1"
    assert synced["sync"]["status"] == "synced"
    assert synced["sync"]["note_id"] == "note-repo"
    assert [r.url.path for r in requests] == [
        "/notes",
        "/notes/upload-file",
        "/notes/note-repo/analysis-graph-drafts",
    ]


def test_sync_outbox_dedups_duplicate_commit_capture(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    _event, path, _action = capture_commit(config)
    uploads: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/notes":
            return _json_response(
                200, {"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}}
            )
        if request.method == "POST" and request.url.path == "/notes/upload-file":
            uploads.append(request)
            return _json_response(
                201,
                {"data": {"note_id": "note-repo", "project_id": "project-1", "status": "staged"}},
            )
        return _json_response(500, {"error": {"message": "unexpected request"}})

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        first = sync_outbox(lt, config)
        second = sync_outbox(lt, config)

    assert first["results"][0]["action"] == "imported"
    # Second run sees the event already synced -> no second upload.
    assert second["results"][0]["action"] == "skipped"
    assert len(uploads) == 1
    assert read_event(path)["sync"]["status"] == "synced"


def test_sync_outbox_dry_run_makes_no_changes(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    _event, path, _action = capture_commit(config)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/notes":
            return _json_response(
                200, {"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}}
            )
        return _json_response(500, {"error": {"message": "unexpected write in dry run"}})

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        summary = sync_outbox(lt, config, dry_run=True)

    assert summary["results"][0]["action"] == "skipped"
    assert read_event(path)["sync"]["status"] == "pending"
