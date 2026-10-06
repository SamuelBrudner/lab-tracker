from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

import httpx
import pytest

from lab_tracker_client import LabTracker
from lab_tracker_client.client import LTValidationError
from lab_tracker_client.gitinfo import CommitFilter
from lab_tracker_client.repo import (
    ALREADY_CAPTURED_REASON,
    RECAPTURED_REASON,
    artifact_from_path,
    capture_commit,
    environment_fingerprint,
    event_client_capture_id,
    event_metadata,
    event_source_external_id,
    init_config,
    load_config,
    make_event,
    normalize_remote,
    outbox_status,
    read_event,
    render_event_note,
    resolve_outbox_path,
    sync_outbox,
    sync_outbox_path,
    validate_event,
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
    assert "## Diff" in event["payload"]["body"]
    assert "diff --git a/analysis.py b/analysis.py" in event["payload"]["body"]
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


def test_render_event_note_contains_bounded_commit_diff(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    commit = _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    event, _path, _action = capture_commit(config)

    note = render_event_note(event)

    assert note == event["payload"]["body"]
    assert commit in note
    assert "https://example.com/org/repo.git" in note
    assert "# Git Commit Evidence" in note
    assert "## Diff" in note
    assert "diff --git a/analysis.py b/analysis.py" in note


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


# --- capture keys: one commit, several events ---------------------------------

CAPTURE_ID_REUSED = (
    "Note client_capture_id 'example.com/org/repo@abc' was already used with "
    "different field(s): raw_asset, metadata."
)


def _form_field(request: httpx.Request, name: str) -> str:
    body = request.content.decode("utf-8", errors="replace")
    match = re.search(rf'name="{name}"\r\n\r\n(.*?)\r\n--', body, re.S)
    assert match, f"{name} missing from the upload"
    return match.group(1)


def _upload_handler(respond):
    """MockTransport handler: an empty note index, uploads answered by ``respond``."""

    uploads: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/notes":
            return _json_response(
                200, {"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}}
            )
        if request.method == "POST" and request.url.path == "/notes/upload-file":
            uploads.append(request)
            return respond(request)
        return _json_response(500, {"error": {"message": "unexpected request"}})

    return handler, uploads


def _staged(request: httpx.Request) -> httpx.Response:
    note_id = f"note-{hashlib.sha256(request.content).hexdigest()[:8]}"
    return _json_response(
        201, {"data": {"note_id": note_id, "project_id": "project-1", "status": "staged"}}
    )


def _conflict(message: str = CAPTURE_ID_REUSED):
    def respond(_request: httpx.Request) -> httpx.Response:
        return _json_response(409, {"error": {"code": "conflict", "message": message}})

    return respond


def _identity_taken(request: httpx.Request) -> httpx.Response:
    """Another capture holds the commit identity key; every other key is new."""

    if ":" in _form_field(request, "client_capture_id").rsplit("@", 1)[1]:
        return _staged(request)
    return _conflict()(request)


def _mark_synced(path) -> None:
    event = read_event(path)
    event["sync"] = {"status": "synced", "attempts": 1, "note_id": "note-1"}
    path.write_text(json.dumps(event), encoding="utf-8")


def test_only_the_commit_capture_uses_the_commit_identity_as_capture_id(
    tmp_path, monkeypatch
) -> None:
    _clear_repo_env(monkeypatch)
    commit = _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    identity = f"example.com/org/repo@{commit}"

    hook, hook_path, _ = capture_commit(config)
    finish, _, _ = capture_commit(config, event_type="finish", summary="Run one.")
    report, _, _ = capture_commit(config, event_type="report")
    _mark_synced(hook_path)
    recaptured, _, action = capture_commit(config, summary="Post-sync annotation")

    assert action == "recaptured"
    events = (hook, finish, report, recaptured)
    # One evidence identity for the commit; one capture key per intended note.
    assert {event_source_external_id(event) for event in events} == {identity}
    assert [event_client_capture_id(event) for event in events] == [
        identity,
        f"{identity}:finish:{finish['event_id']}",
        f"{identity}:report:{report['event_id']}",
        f"{identity}:commit:{recaptured['event_id']}",
    ]


def test_event_capture_ids_stay_distinct_within_the_server_bound(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    source = {"repo_remote_url": "https://example.com/" + "lab/" * 40 + "repo.git"}

    runs = [make_event(config, event_type="finish", source=source) for _ in range(2)]
    keys = [event_client_capture_id(event) for event in runs]

    assert all(len(key) <= 120 for key in keys)
    assert keys[0] != keys[1]


def test_sync_uploads_each_event_at_one_commit_under_its_own_capture_id(
    tmp_path, monkeypatch
) -> None:
    _clear_repo_env(monkeypatch)
    commit = _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    identity = f"example.com/org/repo@{commit}"
    handler, uploads = _upload_handler(_staged)

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        capture_commit(config)
        first = sync_outbox(lt, config)
        finish, _, _ = capture_commit(config, event_type="finish", summary="Run one.")
        second = sync_outbox(lt, config)

    assert first["errors"] == second["errors"] == []
    assert [_form_field(request, "client_capture_id") for request in uploads] == [
        identity,
        f"{identity}:finish:{finish['event_id']}",
    ]
    metadata = [json.loads(_form_field(request, "metadata")) for request in uploads]
    assert {item["evidence_source_external_id"] for item in metadata} == {identity}


def test_sync_settles_a_bare_commit_capture_already_captured_elsewhere(
    tmp_path, monkeypatch
) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    # A config-default question is not an annotation: the hook adds it to every capture.
    config = init_config(project_id="project-1", default_question_id="q-default")
    _event, path, _action = capture_commit(config)
    handler, uploads = _upload_handler(_conflict())

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        first = sync_outbox(lt, config)
        second = sync_outbox(lt, config)

    assert first["errors"] == []
    [result] = first["results"]
    assert (result["action"], result["reason"]) == ("skipped", ALREADY_CAPTURED_REASON)
    assert "note_id" not in result
    sync = read_event(path)["sync"]
    assert (sync["status"], sync["reason"]) == ("synced", ALREADY_CAPTURED_REASON)
    assert "note_id" not in sync
    assert "last_error" not in sync
    status = outbox_status(config.outbox_path())
    assert (status["synced"], status["failed"]) == (1, 0)
    assert status["events"][0]["sync_reason"] == ALREADY_CAPTURED_REASON
    # Settled means terminal: the next drain neither re-uploads nor re-fails it.
    assert second["results"][0]["reason"] == "already_synced"
    assert len(uploads) == 1


@pytest.mark.parametrize(
    ("capture_kwargs", "field"),
    [
        ({"summary": "Sweep over latency window"}, "summary"),
        ({"question_id": "q-explicit"}, "question_id"),
        ({"dataset_ids": ["ds-1"]}, "dataset_ids"),
        ({"tags": ["pilot"]}, "tags"),
        ({"artifacts": [{"uri": "file:///results/out.csv", "title": "out.csv"}]}, "artifacts"),
    ],
)
def test_sync_recaptures_an_annotated_commit_capture_already_captured_elsewhere(
    tmp_path, monkeypatch, capture_kwargs, field
) -> None:
    """Marking it synced would drop the annotation, so it lands as a note of its own."""

    _clear_repo_env(monkeypatch)
    commit = _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    identity = f"example.com/org/repo@{commit}"
    event, path, _action = capture_commit(config, **capture_kwargs)
    handler, uploads = _upload_handler(_identity_taken)

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        first = sync_outbox(lt, config)
        second = sync_outbox(lt, config)

    assert first["errors"] == second["errors"] == []
    [result] = first["results"]
    assert (result["action"], result["reason"]) == ("imported", RECAPTURED_REASON)
    recaptured = read_event(Path(result["path"]))
    assert recaptured["sync"]["note_id"] == result["note_id"]
    assert recaptured[field] == event[field]
    settled = read_event(path)["sync"]
    assert (settled["status"], settled["reason"]) == ("synced", ALREADY_CAPTURED_REASON)
    assert settled["recaptured_event_id"] == recaptured["event_id"]
    # Both events are terminal after one sync: the next drain uploads nothing.
    assert [_form_field(request, "client_capture_id") for request in uploads] == [
        identity,
        f"{identity}:commit:{recaptured['event_id']}",
    ]


def test_an_interrupted_recapture_resumes_as_the_same_event(tmp_path, monkeypatch) -> None:
    """A sync that died before settling the capture finds its recapture, not a new one."""

    _clear_repo_env(monkeypatch)
    commit = _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    _event, path, _action = capture_commit(config, tags=["pilot"])
    unsettled = path.read_text(encoding="utf-8")
    handler, uploads = _upload_handler(_identity_taken)

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        sync_outbox(lt, config)
        path.write_text(unsettled, encoding="utf-8")
        again = sync_outbox(lt, config)

    assert again["errors"] == []
    assert len(list(config.outbox_path().glob("*.json"))) == 2
    keys = [_form_field(request, "client_capture_id") for request in uploads]
    assert keys.count(f"example.com/org/repo@{commit}") == 2
    assert len(keys) == 3
    assert read_event(path)["sync"]["reason"] == ALREADY_CAPTURED_REASON


def test_a_failed_recapture_retries_without_reopening_the_settled_capture(
    tmp_path, monkeypatch
) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    _event, path, _action = capture_commit(config, tags=["pilot"])
    outage = [True]

    def respond(request: httpx.Request) -> httpx.Response:
        if outage[0] and ":" in _form_field(request, "client_capture_id").rsplit("@", 1)[1]:
            return _json_response(503, {"error": {"message": "maintenance"}})
        return _identity_taken(request)

    handler, uploads = _upload_handler(respond)

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        failed = sync_outbox(lt, config)
        outage[0] = False
        retried = sync_outbox(lt, config)

    [error] = failed["errors"]
    assert (error["action"], error["reason"]) == ("failed", RECAPTURED_REASON)
    assert "maintenance" in error["error"]
    assert Path(error["path"]) != path
    assert read_event(path)["sync"]["status"] == "synced"
    assert retried["errors"] == []
    assert read_event(Path(error["path"]))["sync"]["status"] == "synced"
    # The settled capture is never uploaded again; only the recapture retries.
    assert len(uploads) == 3


@pytest.mark.parametrize(
    ("default_question_id", "capture_kwargs", "recaptured"),
    [
        (None, {}, False),
        (None, {"summary": "Sweep over latency window"}, True),
        # Without question_id_source a config default may have been explicit.
        ("q-default", {}, True),
    ],
)
def test_sync_judges_a_capture_recorded_before_annotation_flags_by_content(
    tmp_path, monkeypatch, default_question_id, capture_kwargs, recaptured
) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1", default_question_id=default_question_id)
    _event, path, _action = capture_commit(config, **capture_kwargs)
    legacy = read_event(path)
    legacy.pop("question_id_source")
    legacy["payload"].pop("summary_is_explicit")
    path.write_text(json.dumps(legacy), encoding="utf-8")
    handler, uploads = _upload_handler(_identity_taken)

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        summary = sync_outbox(lt, config)

    assert summary["errors"] == []
    assert read_event(path)["sync"]["reason"] == ALREADY_CAPTURED_REASON
    assert len(uploads) == (2 if recaptured else 1)


@pytest.mark.parametrize(
    ("event_type", "message"),
    [
        # A per-event key refused: the same event was uploaded with other content.
        ("finish", CAPTURE_ID_REUSED),
        ("report", CAPTURE_ID_REUSED),
        # Any other conflict is not evidence that the commit was captured.
        ("commit", "Project is archived."),
    ],
)
def test_sync_keeps_other_capture_conflicts_failed(
    tmp_path, monkeypatch, event_type, message
) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    _event, path, _action = capture_commit(config, event_type=event_type)
    handler, _uploads = _upload_handler(_conflict(message))

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        summary = sync_outbox(lt, config)

    [error] = summary["errors"]
    assert message in error["error"]
    assert read_event(path)["sync"]["status"] == "failed"


def _clone(source, destination, *, remote: str) -> None:
    subprocess.run(["git", "clone", "-q", str(source), str(destination)], check=True)
    subprocess.run(
        ["git", "-C", str(destination), "remote", "set-url", "origin", remote], check=True
    )


def test_every_event_at_one_commit_syncs_against_the_real_server(
    tmp_path, monkeypatch, client, admin_auth_headers
) -> None:
    _clear_repo_env(monkeypatch)
    project = client.post("/projects", json={"name": "Repo keys"}, headers=admin_auth_headers)
    assert project.status_code in (200, 201), project.text
    project_id = project.json()["data"]["project_id"]
    token = admin_auth_headers["Authorization"].split(" ", 1)[1]
    laptop = tmp_path / "laptop"
    laptop.mkdir()
    _init_git_repo(laptop)
    results = tmp_path / "results"
    results.mkdir()
    (results / "decoding.csv").write_text("trial,score\n1,0.9\n", encoding="utf-8")

    def sync(checkout):
        with LabTracker(
            base_url="http://testserver", access_token=token, transport=client._transport
        ) as lt:
            return sync_outbox_path(lt, checkout / ".lab-tracker" / "outbox" / "repo")

    def configure(checkout):
        return init_config(
            project_id=project_id, config_path=checkout / ".lab-tracker" / "repo.json"
        )

    monkeypatch.setenv("LAB_TRACKER_CAPTURE_HOST", "laptop")
    config = configure(laptop)
    capture_commit(config, cwd=laptop)
    hook = sync(laptop)
    artifact = artifact_from_path(results / "decoding.csv", root=results)
    _finish, finish_path, _ = capture_commit(
        config, event_type="finish", cwd=laptop, artifacts=[artifact]
    )
    run_finish = sync(laptop)
    _annotated, _, recapture_action = capture_commit(
        config, cwd=laptop, summary="Sweep over latency window"
    )
    annotation = sync(laptop)

    for summary in (hook, run_finish, annotation):
        assert summary["errors"] == []
        assert [item["action"] for item in summary["results"] if item["action"] != "skipped"] == [
            "imported"
        ]
    assert recapture_action == "recaptured"

    # A lost upload response, replayed without the evidence index: the server's
    # capture key alone returns the note it already made instead of a second one.
    finish_note_id = read_event(finish_path)["sync"]["note_id"]
    replayed = read_event(finish_path)
    replayed["sync"] = {"status": "pending", "attempts": 1}
    finish_path.write_text(json.dumps(replayed), encoding="utf-8")
    with monkeypatch.context() as patch:
        patch.setattr("lab_tracker_client.repo.outbox_note_index", lambda *a, **k: {})
        replay = sync(laptop)
    assert replay["errors"] == []
    assert read_event(finish_path)["sync"]["note_id"] == finish_note_id

    # Another machine's bare hook capture of the same commit settles; an
    # annotated one keeps its tag as a note of its own.
    for name, tags in (("ci", None), ("desk", ["pilot"])):
        checkout = tmp_path / name
        _clone(laptop, checkout, remote="git@example.com:org/repo.git")
        monkeypatch.setenv("LAB_TRACKER_CAPTURE_HOST", name)
        capture_commit(configure(checkout), cwd=checkout, tags=tags)
    ci, desk = sync(tmp_path / "ci"), sync(tmp_path / "desk")

    assert ci["errors"] == desk["errors"] == []
    assert [item["reason"] for item in ci["results"]] == [ALREADY_CAPTURED_REASON]
    assert [item["reason"] for item in desk["results"]] == [RECAPTURED_REASON]
    notes = client.get(
        "/notes", params={"project_id": project_id}, headers=admin_auth_headers
    ).json()["data"]
    assert sorted(note["metadata"]["repo_event_type"] for note in notes) == [
        "commit",
        "commit",
        "commit",
        "finish",
    ]
    assert [note["metadata"].get("repo_tags") for note in notes].count("pilot") == 1


# --- declared targets --------------------------------------------------------


def test_make_event_labels_question_source(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1", default_question_id="q-default")

    defaulted = make_event(config, event_type="commit")
    explicit = make_event(config, event_type="commit", question_id="question-1")

    assert defaulted["question_id"] == "q-default"
    assert defaulted["question_id_source"] == "config_default"
    assert explicit["question_id"] == "question-1"
    assert explicit["question_id_source"] == "explicit"


def test_validate_event_rejects_unknown_question_id_source(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    event = make_event(config, event_type="commit", question_id="question-1")

    with pytest.raises(LTValidationError):
        validate_event({**event, "question_id_source": "bogus"})


def test_capture_commit_hook_refire_keeps_config_default_source(tmp_path, monkeypatch) -> None:
    """A bare re-fire must not relabel a config-default question as explicit."""

    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1", default_question_id="q-default")
    capture_commit(config)

    _event, path, action = capture_commit(config)  # bare, hook-style

    assert action == "unchanged"
    assert read_event(path)["question_id_source"] == "config_default"

    # Declaring the same question explicitly is a real annotation: it upgrades
    # the recorded provenance instead of being swallowed as "unchanged".
    annotated, _path, annotated_action = capture_commit(config, question_id="q-default")
    assert annotated_action == "updated"
    assert annotated["question_id_source"] == "explicit"


def test_sync_outbox_passes_declared_targets_and_source(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1", default_question_id="q-default")
    capture_commit(config, dataset_ids=["ds-1"])
    uploads: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/notes":
            return _json_response(
                200, {"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}}
            )
        if request.method == "POST" and request.url.path == "/notes/upload-file":
            uploads.append(request.content)
            return _json_response(
                201,
                {"data": {"note_id": "note-repo", "project_id": "project-1", "status": "staged"}},
            )
        return _json_response(500, {"error": {"message": "unexpected request"}})

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        summary = sync_outbox(lt, config)

    assert summary["errors"] == []
    assert len(uploads) == 1
    body = uploads[0]
    assert b'name="targets"' in body
    assert b'"entity_type": "question"' in body
    assert b'"entity_id": "q-default"' in body
    assert b'"entity_type": "dataset"' in body
    assert b"declared_target_source" in body
    assert b"config_default" in body


# --- commit filter config + outbox resolution ---------------------------------


def test_repo_config_round_trips_commit_filter(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    config_path = config.config_path
    assert config_path is not None

    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written["commit_filter"] == {
        "skip_merges": True,
        "skip_fixups": True,
        "skip_wip": False,
        "skip_path_globs": [],
    }
    assert load_config().commit_filter == CommitFilter()

    written["commit_filter"]["skip_wip"] = True
    written["commit_filter"]["skip_path_globs"] = ["docs/*"]
    config_path.write_text(json.dumps(written), encoding="utf-8")
    assert load_config().commit_filter == CommitFilter(skip_wip=True, skip_path_globs=("docs/*",))

    written["commit_filter"]["skip_marges"] = True
    config_path.write_text(json.dumps(written), encoding="utf-8")
    with pytest.raises(LTValidationError, match="skip_marges"):
        load_config()

    # A config written before the filter existed keeps the defaults.
    del written["commit_filter"]
    config_path.write_text(json.dumps(written), encoding="utf-8")
    assert load_config().commit_filter == CommitFilter()

    written["commit_filter"] = "merges"
    config_path.write_text(json.dumps(written), encoding="utf-8")
    with pytest.raises(LTValidationError, match="JSON object"):
        load_config()


def test_resolve_outbox_path_without_config_uses_default_and_env(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    default = (tmp_path / ".lab-tracker" / "outbox" / "repo").resolve()

    assert resolve_outbox_path(tmp_path) == (default, None)
    assert not default.exists()  # resolving never creates an outbox

    monkeypatch.setenv("LAB_TRACKER_REPO_OUTBOX", str(tmp_path / "custom"))
    assert resolve_outbox_path(tmp_path) == ((tmp_path / "custom").resolve(), None)
    monkeypatch.delenv("LAB_TRACKER_REPO_OUTBOX")

    # A config that exists resolves through it...
    config = init_config(
        project_id="project-1",
        outbox="queue/repo",
        config_path=tmp_path / ".lab-tracker" / "repo.json",
    )
    assert resolve_outbox_path(tmp_path) == ((tmp_path / "queue" / "repo").resolve(), None)

    # ...and a broken one falls back to the default while naming the error.
    assert config.config_path is not None
    config.config_path.write_text("{not json", encoding="utf-8")
    outbox, error = resolve_outbox_path(tmp_path)
    assert outbox == default
    assert error is not None
    assert "could not be loaded" in error
