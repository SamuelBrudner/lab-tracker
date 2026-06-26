from __future__ import annotations

import json

import httpx

from lab_tracker_client import LabTracker
from lab_tracker_client.watch import (
    SINK_ACQUISITION_OUTPUT,
    SINK_STAGED_NOTE,
    _event_metadata,
    event_from_manifest,
    init_config,
    outbox_status,
    read_event,
    scan_watch,
    sync_outbox,
)


def _json_response(status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def test_scan_files_writes_idempotent_staged_note_event(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("LAB_TRACKER_WATCH_CONFIG", raising=False)
    monkeypatch.delenv("LAB_TRACKER_WATCH_OUTBOX", raising=False)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    evidence = inbox / "capture.md"
    evidence.write_text("capture text", encoding="utf-8")

    summary = scan_watch(config, mode="files", root=inbox)
    second_summary = scan_watch(config, mode="files", root=inbox)

    event_files = list(config.outbox_path().glob("*.json"))
    event = read_event(event_files[0])
    assert summary["matched"] == 1
    assert second_summary["imported"][0]["already_present"] is True
    assert len(event_files) == 1
    assert event["sink"] == SINK_STAGED_NOTE
    assert event["context"]["project_id"] == "project-1"
    assert outbox_status(config.outbox_path())["pending"] == 1


def test_scan_records_capture_host_on_event_and_note_metadata(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("LAB_TRACKER_WATCH_CONFIG", raising=False)
    monkeypatch.delenv("LAB_TRACKER_WATCH_OUTBOX", raising=False)
    monkeypatch.setenv("LAB_TRACKER_CAPTURE_HOST", "rig-7")
    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "capture.md").write_text("capture text", encoding="utf-8")

    scan_watch(config, mode="files", root=inbox)
    event = read_event(next(iter(config.outbox_path().glob("*.json"))))

    assert event["host"]["capture_host_label"] == "rig-7"
    assert len(event["host"]["capture_install_id"]) == 32
    note_metadata = _event_metadata(event)
    assert note_metadata["capture_host_label"] == "rig-7"
    assert note_metadata["capture_install_id"] == event["host"]["capture_install_id"]


def test_sync_file_event_uploads_staged_note_and_requests_draft(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "capture.md").write_text("capture text", encoding="utf-8")
    scan_watch(config, mode="files", root=inbox, tags=["pilot"])
    event_path = next(config.outbox_path().glob("*.json"))
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/notes":
            return _json_response(
                200,
                {"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}},
            )
        if request.method == "POST" and request.url.path == "/notes/upload-file":
            body = request.content
            assert b"capture text" in body
            assert b"watch_capture_id" in body
            assert b"pilot" in body
            return _json_response(201, {"data": {"note_id": "note-watch"}})
        if (
            request.method == "POST"
            and request.url.path == "/notes/note-watch/analysis-graph-drafts"
        ):
            return _json_response(201, {"data": {"change_set_id": "draft-watch"}})
        return _json_response(500, {"error": {"message": "unexpected request"}})

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        summary = sync_outbox(lt, config, request_draft=True)

    synced = read_event(event_path)
    assert summary["errors"] == []
    assert summary["results"][0]["note_id"] == "note-watch"
    assert summary["results"][0]["change_set_id"] == "draft-watch"
    assert synced["sync"]["status"] == "synced"
    assert [request.url.path for request in requests] == [
        "/notes",
        "/notes/upload-file",
        "/notes/note-watch/analysis-graph-drafts",
    ]


def test_manifest_event_syncs_markdown_note(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    run_dir = tmp_path / "outputs" / "run-1"
    run_dir.mkdir(parents=True)
    manifest = run_dir / "lab-tracker-evidence.json"
    manifest.write_text(
        json.dumps(
            {
                "capture_id": "run-1",
                "summary": "Decoded held-out trials.",
                "artifacts": [{"uri": "file:///scratch/run-1/summary.png", "title": "summary"}],
                "metrics": {"accuracy": 0.91},
            }
        ),
        encoding="utf-8",
    )

    summary = scan_watch(config, mode="manifest", root=tmp_path / "outputs")
    event = event_from_manifest(config, manifest)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/notes":
            return _json_response(
                200,
                {"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}},
            )
        if request.method == "POST" and request.url.path == "/notes/upload-file":
            body = request.content
            assert b"Decoded held-out trials." in body
            assert b"summary.png" in body
            assert b"accuracy" in body
            return _json_response(201, {"data": {"note_id": "note-manifest"}})
        return _json_response(500, {"error": {"message": "unexpected request"}})

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        synced = sync_outbox(lt, config)

    assert summary["matched"] == 1
    assert event["capture_id"] == "run-1"
    assert synced["errors"] == []
    assert synced["results"][0]["note_id"] == "note-manifest"


def test_sync_marks_changed_file_stale(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    evidence = inbox / "capture.md"
    evidence.write_text("first", encoding="utf-8")
    scan_watch(config, mode="files", root=inbox)
    event_path = next(config.outbox_path().glob("*.json"))
    evidence.write_text("second", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        raise AssertionError("stale events must not call the API")

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        summary = sync_outbox(lt, config)

    stale = read_event(event_path)
    assert summary["results"][0]["action"] == "stale"
    assert summary["errors"][0]["error"].startswith("watched file changed")
    assert stale["sync"]["status"] == "stale"


def test_sync_acquisition_output_registers_session_output(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = init_config()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    output = outputs / "output.bin"
    output.write_text("acquired", encoding="utf-8")
    scan_watch(
        config,
        mode="files",
        root=outputs,
        sink=SINK_ACQUISITION_OUTPUT,
        session_id="session-1",
    )
    event_path = next(config.outbox_path().glob("*.json"))
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST" and request.url.path == "/sessions/session-1/outputs":
            payload = json.loads(request.content)
            assert payload["file_path"] == "output.bin"
            assert payload["size_bytes"] == len("acquired")
            return _json_response(201, {"data": {"output_id": "output-1"}})
        return _json_response(500, {"error": {"message": "unexpected request"}})

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        summary = sync_outbox(lt, config, request_draft=True)

    synced = read_event(event_path)
    assert summary["errors"] == []
    assert summary["results"][0]["output_id"] == "output-1"
    assert synced["sync"]["status"] == "synced"
    assert synced["sync"]["output_id"] == "output-1"
    assert [request.url.path for request in requests] == ["/sessions/session-1/outputs"]
