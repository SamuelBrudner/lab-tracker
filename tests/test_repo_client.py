from __future__ import annotations

import subprocess

import httpx

from lab_tracker_client import LabTracker
from lab_tracker_client.repo import (
    capture_commit,
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
    event, path = capture_commit(loaded, tags=["pilot"])

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

    _event_a, path_a = capture_commit(config)
    _event_b, path_b = capture_commit(config)

    # A repeated post-commit hook for the same commit must not pile up events.
    assert path_a == path_b
    assert len(list(config.outbox_path().glob("*.json"))) == 1


def test_capture_commit_records_dirty_tree(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    (tmp_path / "analysis.py").write_text("print('changed')\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")

    event, _path = capture_commit(config)

    assert event["source"]["git_dirty"] is True


def test_render_event_note_contains_commit_state(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    commit = _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    event, _path = capture_commit(config)

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
    event, _path = capture_commit(config)

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


# --- sync -----------------------------------------------------------------


def test_sync_outbox_uploads_staged_note_and_requests_draft(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    _event, path = capture_commit(config, summary="Pinned the analysis commit.")
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
    _event, path = capture_commit(config)
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
    _event, path = capture_commit(config)

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
