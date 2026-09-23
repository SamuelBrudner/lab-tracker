from __future__ import annotations

import hashlib
import json
import os

import httpx

import lab_tracker_client.watch as watch_module
from lab_tracker_client import LabTracker
from lab_tracker_client.watch import (
    SINK_ACQUISITION_OUTPUT,
    SINK_STAGED_NOTE,
    _event_metadata,
    _event_source_external_id,
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


def test_manifest_sync_dedupes_against_existing_note_without_upload(tmp_path, monkeypatch) -> None:
    """When GET /notes already holds a matching evidence note, sync must not

    re-upload; it records that note_id and marks the event synced (the
    EvidenceNoteIndex manifest-branch dedup).
    """

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
                "payload": {"body": "literal evidence body"},
            }
        ),
        encoding="utf-8",
    )
    scan_watch(config, mode="manifest", root=tmp_path / "outputs")
    event_path = next(config.outbox_path().glob("*.json"))
    event = read_event(event_path)

    # The evidence key the client will derive from this event: dedup is keyed on
    # (provider, external_id, content_hash), not on the note body.
    content_hash = hashlib.sha256(event["payload"]["body"].encode("utf-8")).hexdigest()
    existing_note = {
        "note_id": "note-existing",
        "metadata": {
            "evidence_source_provider": str(event["source"].get("provider") or "watch-manifest"),
            "evidence_source_external_id": _event_source_external_id(event),
            "evidence_content_hash": content_hash,
        },
    }
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/notes":
            return _json_response(
                200,
                {"data": [existing_note], "meta": {"limit": 200, "offset": 0, "total": 1}},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        summary = sync_outbox(lt, config)

    synced = read_event(event_path)
    assert summary["errors"] == []
    assert summary["results"][0]["action"] == "skipped"
    assert summary["results"][0]["reason"] == "duplicate"
    assert summary["results"][0]["note_id"] == "note-existing"
    assert synced["sync"]["status"] == "synced"
    assert synced["sync"]["note_id"] == "note-existing"
    # Only the notes index was fetched; no upload happened.
    assert [request.url.path for request in requests] == ["/notes"]


def test_second_sync_pass_is_idempotent_with_zero_requests(tmp_path, monkeypatch) -> None:
    """A terminal (already-synced) event is skipped on a re-drain: zero writes,

    zero HTTP requests on the second pass.
    """

    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "capture.md").write_text("capture text", encoding="utf-8")
    scan_watch(config, mode="files", root=inbox)

    first_requests: list[httpx.Request] = []

    def first_handler(request: httpx.Request) -> httpx.Response:
        first_requests.append(request)
        if request.method == "GET" and request.url.path == "/notes":
            return _json_response(
                200, {"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}}
            )
        if request.method == "POST" and request.url.path == "/notes/upload-file":
            return _json_response(201, {"data": {"note_id": "note-1"}})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    with LabTracker(
        base_url="http://testserver", transport=httpx.MockTransport(first_handler)
    ) as lt:
        first = sync_outbox(lt, config)
    assert first["errors"] == []
    assert first_requests  # the first pass did upload

    def second_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"second pass must issue no requests: {request.url.path}")

    with LabTracker(
        base_url="http://testserver", transport=httpx.MockTransport(second_handler)
    ) as lt:
        second = sync_outbox(lt, config)

    assert {result["action"] for result in second["results"]} == {"skipped"}
    assert second["results"][0]["reason"] == "already_synced"
    assert second["errors"] == []


def test_acquisition_output_reregistration_after_crash_is_idempotent(
    tmp_path,
    monkeypatch,
) -> None:
    """A crash between the server registration and the local status write leaves

    the event non-terminal; the retry re-registers and (with the server's
    idempotent register_acquisition_output) resolves to a single output.
    """

    monkeypatch.chdir(tmp_path)
    config = init_config()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (outputs / "output.bin").write_text("acquired", encoding="utf-8")
    scan_watch(
        config,
        mode="files",
        root=outputs,
        sink=SINK_ACQUISITION_OUTPUT,
        session_id="session-1",
    )
    event_path = next(config.outbox_path().glob("*.json"))

    register_calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/sessions/session-1/outputs":
            register_calls.append(request)
            # Server registration is idempotent: same output_id across retries.
            return _json_response(201, {"data": {"output_id": "output-1"}})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    real_record = watch_module._record_sync_success
    crashed = {"done": False}

    def crash_once(*args: object, **kwargs: object) -> None:
        if not crashed["done"]:
            crashed["done"] = True
            raise RuntimeError("crash between server registration and status write")
        real_record(*args, **kwargs)

    monkeypatch.setattr(watch_module, "_record_sync_success", crash_once)

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        first = sync_outbox(lt, config)
        # The registration succeeded server-side, but the status write crashed:
        # the event is left non-terminal (failed), not silently synced.
        assert first["results"][0]["action"] == "failed"
        assert read_event(event_path)["sync"]["status"] != "synced"

        second = sync_outbox(lt, config)

    synced = read_event(event_path)
    assert len(register_calls) == 2  # re-registered on retry
    assert second["results"][0]["output_id"] == "output-1"
    assert synced["sync"]["status"] == "synced"
    assert synced["sync"]["output_id"] == "output-1"


def test_manifest_sync_detects_staleness_without_api_call(tmp_path, monkeypatch) -> None:
    """A manifest mutated between scan and sync is stale and must not hit the API."""

    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    run_dir = tmp_path / "outputs" / "run-1"
    run_dir.mkdir(parents=True)
    manifest = run_dir / "lab-tracker-evidence.json"
    manifest.write_text(json.dumps({"capture_id": "run-1", "summary": "v1"}), encoding="utf-8")
    scan_watch(config, mode="manifest", root=tmp_path / "outputs")
    event_path = next(config.outbox_path().glob("*.json"))
    # Mutate the manifest after scan: the stored manifest_content_hash no longer
    # matches the file on disk.
    manifest.write_text(
        json.dumps({"capture_id": "run-1", "summary": "v2 changed"}), encoding="utf-8"
    )

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        raise AssertionError("stale manifest events must not call the API")

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        summary = sync_outbox(lt, config)

    stale = read_event(event_path)
    assert summary["results"][0]["action"] == "stale"
    assert "manifest changed" in summary["errors"][0]["error"]
    assert stale["sync"]["status"] == "stale"


def test_scan_manifest_batch_reports_malformed_without_aborting(tmp_path, monkeypatch) -> None:
    """A malformed manifest in a batch is recorded in errors; valid siblings still

    import (scan-time source handling, complementary to the outbox quarantine).
    """

    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    outputs = tmp_path / "outputs"
    good_dir = outputs / "good"
    good_dir.mkdir(parents=True)
    (good_dir / "lab-tracker-evidence.json").write_text(
        json.dumps({"capture_id": "good-1", "summary": "ok"}), encoding="utf-8"
    )
    bad_dir = outputs / "bad"
    bad_dir.mkdir(parents=True)
    (bad_dir / "lab-tracker-evidence.json").write_text("{not valid json", encoding="utf-8")

    summary = scan_watch(config, mode="manifest", root=outputs)

    assert {item["capture_id"] for item in summary["imported"]} == {"good-1"}
    assert len(summary["errors"]) == 1
    assert "bad" in summary["errors"][0]["source"]
    assert summary["errors"][0]["error"]
    assert len(list(config.outbox_path().glob("*.json"))) == 1


def _notes_handler(uploads: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/notes":
            return _json_response(
                200,
                {"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}},
            )
        if request.method == "POST" and request.url.path == "/notes/upload-file":
            note_id = f"note-{len(uploads) + 1}"
            uploads.append(note_id)
            return _json_response(201, {"data": {"note_id": note_id}})
        return _json_response(500, {"error": {"message": "unexpected request"}})

    return handler


def test_stale_events_are_terminal_and_do_not_starve_pending_under_limit(
    tmp_path, monkeypatch
) -> None:
    """M80: a pending event behind N stale events still syncs under ``limit=N``."""

    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for name in ("a.md", "b.md", "c.md"):
        (inbox / name).write_text(f"first {name}", encoding="utf-8")
    scan = scan_watch(config, mode="files", root=inbox)
    by_event_path = sorted(scan["imported"], key=lambda item: item["event_path"])
    # Rewrite the two sources whose events sort first so they go stale and
    # sit in front of the remaining pending event in drain order.
    stale_sources = [item["source"] for item in by_event_path[:2]]
    pending_event_path = by_event_path[2]["event_path"]
    for source in stale_sources:
        with open(source, "w", encoding="utf-8") as handle:
            handle.write("rewritten after scan")

    uploads: list[str] = []
    handler = _notes_handler(uploads)
    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        first = sync_outbox(lt, config, limit=2)
        second = sync_outbox(lt, config, limit=2)

    assert [result["action"] for result in first["results"]] == ["stale", "stale"]
    assert len(first["errors"]) == 2
    # The stale events are terminal: reported as skipped without an error and
    # without spending the limit, so the pending event is reached.
    second_by_path = {result["path"]: result for result in second["results"]}
    assert second_by_path[pending_event_path]["action"] == "imported"
    assert [second_by_path[item["event_path"]]["reason"] for item in by_event_path[:2]] == [
        "stale",
        "stale",
    ]
    assert second["errors"] == []
    assert uploads == ["note-1"]
    assert read_event(pending_event_path)["sync"]["status"] == "synced"
    status = outbox_status(config.outbox_path())
    assert status["stale"] == 2
    assert status.get("pending", 0) == 0


def test_stale_event_is_not_rechecked_on_later_syncs(tmp_path, monkeypatch) -> None:
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
        sync_outbox(lt, config)
        after_first = read_event(event_path)
        sync_outbox(lt, config)

    after_second = read_event(event_path)
    assert after_first["sync"]["status"] == "stale"
    assert after_second["sync"] == after_first["sync"]


def test_rescan_rearms_stale_event_when_file_matches_again(tmp_path, monkeypatch) -> None:
    """A file touched between scan and sync (same bytes, new mtime) goes stale;

    the next scan re-arms the same event with the fresh fingerprint so the
    capture is not lost, which is the documented retry path for stale events.
    """

    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    evidence = inbox / "capture.md"
    evidence.write_text("same bytes", encoding="utf-8")
    scan_watch(config, mode="files", root=inbox)
    event_path = next(config.outbox_path().glob("*.json"))
    stat = evidence.stat()
    os.utime(evidence, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))

    uploads: list[str] = []
    handler = _notes_handler(uploads)
    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        stale = sync_outbox(lt, config)
        rescan = scan_watch(config, mode="files", root=inbox)
        synced = sync_outbox(lt, config)

    assert stale["results"][0]["action"] == "stale"
    assert rescan["imported"][0]["event_path"] == str(event_path)
    assert rescan["imported"][0]["rearmed"] is True
    assert synced["results"][0]["action"] == "imported"
    assert uploads == ["note-1"]
    assert len(list(config.outbox_path().glob("*.json"))) == 1
    assert read_event(event_path)["sync"]["status"] == "synced"


def test_draft_retry_on_synced_event_ignores_later_source_changes(tmp_path, monkeypatch) -> None:
    """Terminal ``stale`` must never overwrite an already-delivered event."""

    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    evidence = inbox / "capture.md"
    evidence.write_text("first", encoding="utf-8")
    scan_watch(config, mode="files", root=inbox)
    event_path = next(config.outbox_path().glob("*.json"))
    uploads: list[str] = []
    upload_handler = _notes_handler(uploads)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/analysis-graph-drafts"):
            return _json_response(201, {"data": {"change_set_id": "draft-1"}})
        return upload_handler(request)

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        sync_outbox(lt, config)
        evidence.write_text("second", encoding="utf-8")
        retried = sync_outbox(lt, config, request_draft=True)

    synced = read_event(event_path)
    assert retried["results"][0]["action"] == "synced"
    assert synced["sync"]["status"] == "synced"
    assert synced["sync"]["note_id"] == "note-1"
    assert synced["sync"]["change_set_id"] == "draft-1"


def test_scan_attaches_session_from_link_code_in_path_and_sync_sends_target(
    tmp_path, monkeypatch
) -> None:
    from lab_tracker_client.session_context import encode_session_link_code

    monkeypatch.delenv("LAB_TRACKER_SESSION_ID", raising=False)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    session_id = "3d4f6a1e-9c2b-4a8e-8f01-2b3c4d5e6f70"
    code = encode_session_link_code(session_id)
    inbox = tmp_path / "rig2"
    session_dir = inbox / f"session001_LT-{code}"
    session_dir.mkdir(parents=True)
    (session_dir / "trace.md").write_text("bench note", encoding="utf-8")
    (inbox / "loose.md").write_text("no session here", encoding="utf-8")

    scan_watch(config, mode="files", root=inbox)
    events = {read_event(path)["source"]["relative_path"]: read_event(path)
              for path in config.outbox_path().glob("*.json")}
    linked = events[f"session001_LT-{code}/trace.md"]
    loose = events["loose.md"]
    assert linked["context"]["session_id"] == session_id
    assert linked["source"]["session_source"] == "path"
    assert loose["context"]["session_id"] is None
    assert "session_source" not in loose["source"]
    assert _event_metadata(linked)["watch_session_id"] == session_id
    assert _event_metadata(linked)["watch_session_source"] == "path"

    uploads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/notes":
            return _json_response(
                200, {"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}}
            )
        if request.method == "POST" and request.url.path == "/notes/upload-file":
            body = request.content.decode("utf-8", errors="replace")
            uploads.append({"has_targets": 'name="targets"' in body, "body": body})
            return _json_response(201, {"data": {"note_id": f"note-{len(uploads)}"}})
        return _json_response(500, {"error": {"message": "unexpected request"}})

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        summary = sync_outbox(lt, config)

    assert summary["errors"] == []
    linked_upload = next(item for item in uploads if "bench note" in item["body"])
    loose_upload = next(item for item in uploads if "no session here" in item["body"])
    assert linked_upload["has_targets"] is True
    assert f'"entity_id": "{session_id}"' in linked_upload["body"]
    assert '"entity_type": "session"' in linked_upload["body"]
    assert loose_upload["has_targets"] is False


def test_scan_uses_the_checkout_active_session_when_the_path_names_none(
    tmp_path, monkeypatch
) -> None:
    from lab_tracker_client.session_context import set_active_session

    monkeypatch.delenv("LAB_TRACKER_SESSION_ID", raising=False)
    monkeypatch.delenv("LAB_TRACKER_SESSION_CONTEXT", raising=False)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    session_id = "3d4f6a1e-9c2b-4a8e-8f01-2b3c4d5e6f70"
    set_active_session(session_id, start=tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "capture.md").write_text("bench note", encoding="utf-8")

    scan_watch(config, mode="files", root=inbox)
    event = read_event(next(config.outbox_path().glob("*.json")))
    assert event["context"]["session_id"] == session_id
    assert event["source"]["session_source"] == "active"

    # An explicit --session (here a link code) still wins over the checkout.
    explicit = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
    from lab_tracker_client.session_context import encode_session_link_code

    (inbox / "second.md").write_text("another", encoding="utf-8")
    scan_watch(config, mode="files", root=inbox, session_id=encode_session_link_code(explicit))
    events = [read_event(path) for path in config.outbox_path().glob("*.json")]
    second = next(item for item in events if item["source"]["relative_path"] == "second.md")
    assert second["context"]["session_id"] == explicit
    assert second["source"]["session_source"] == "config"


def test_sync_inherits_the_project_bound_after_the_scan(tmp_path, monkeypatch) -> None:
    """An event queued before `lt project bind` must not stay stuck: the sync
    fills the project from lt_ids.json (then the environment) instead of
    failing forever on an empty project_id."""

    monkeypatch.delenv("LAB_TRACKER_PROJECT_ID", raising=False)
    monkeypatch.delenv("LAB_TRACKER_SESSION_ID", raising=False)
    monkeypatch.chdir(tmp_path)
    config = init_config()
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "early.md").write_text("queued before binding", encoding="utf-8")
    scan_watch(config, mode="files", root=inbox)
    event_path = next(config.outbox_path().glob("*.json"))
    assert read_event(event_path)["context"]["project_id"] is None

    def failing_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no request expected without a project: {request.url}")

    transport = httpx.MockTransport(failing_handler)
    with LabTracker(base_url="http://testserver", transport=transport) as lt:
        unresolved = sync_outbox(lt, config)
    assert unresolved["results"][0]["action"] == "failed"
    assert "lt project bind" in unresolved["results"][0]["error"]

    (tmp_path / "lt_ids.json").write_text(json.dumps({"project_id": "project-9"}), encoding="utf-8")
    seen_projects: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/notes":
            seen_projects.append(request.url.params.get("project_id", ""))
            return _json_response(
                200, {"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}}
            )
        if request.method == "POST" and request.url.path == "/notes/upload-file":
            assert b"project-9" in request.content
            return _json_response(201, {"data": {"note_id": "note-late"}})
        return _json_response(500, {"error": {"message": "unexpected request"}})

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        summary = sync_outbox(lt, config)
    assert summary["errors"] == []
    assert summary["results"][0]["note_id"] == "note-late"
    assert seen_projects == ["project-9"]

