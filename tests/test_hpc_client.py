from __future__ import annotations

import hashlib
import json

import httpx

from lab_tracker_client import LabTracker
from lab_tracker_client.hpc import (
    begin_event,
    event_from_manifest,
    event_source_external_id,
    finish_event,
    init_config,
    load_config,
    outbox_status,
    parse_sbatch_job,
    read_event,
    render_event_note,
    sync_outbox,
    watch_manifests,
)


def _json_response(status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def _clear_hpc_env(monkeypatch) -> None:
    monkeypatch.delenv("LAB_TRACKER_HPC_CONFIG", raising=False)
    monkeypatch.delenv("LAB_TRACKER_HPC_OUTBOX", raising=False)
    monkeypatch.delenv("LAB_TRACKER_HPC_RUN_ID", raising=False)
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    monkeypatch.delenv("SLURM_ARRAY_TASK_ID", raising=False)


def test_init_config_begin_event_and_status(tmp_path, monkeypatch) -> None:
    _clear_hpc_env(monkeypatch)
    monkeypatch.chdir(tmp_path)

    config = init_config(project_id="project-1", cluster="bouchet")
    loaded = load_config()
    event, path = begin_event(
        loaded,
        run_id="run-1",
        question_id="question-1",
        tags=["pilot"],
    )

    assert config.config_path == tmp_path / ".lab-tracker" / "hpc.json"
    assert loaded.outbox_path() == tmp_path / ".lab-tracker" / "outbox" / "hpc"
    assert path.exists()
    assert read_event(path)["sync"]["status"] == "pending"
    assert event["scheduler"]["state"] == "running"
    assert "# HPC run run-1 begin" in render_event_note(event)
    assert outbox_status(loaded.outbox_path())["pending"] == 1


def test_parse_sbatch_job_variants() -> None:
    assert parse_sbatch_job("Submitted batch job 12345").job_id == "12345"
    parsed = parse_sbatch_job("12345_7;bouchet", fallback_cluster="other")

    assert parsed is not None
    assert parsed.job_id == "12345"
    assert parsed.array_task_id == "7"
    assert parsed.cluster == "bouchet"


def test_watch_manifest_writes_deterministic_finish_event(tmp_path, monkeypatch) -> None:
    _clear_hpc_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1", cluster="generic")
    run_dir = tmp_path / "outputs" / "run-42"
    run_dir.mkdir(parents=True)
    manifest = run_dir / "lab-tracker-hpc-run.json"
    manifest.write_text(
        json.dumps(
            {
                "run_id": "run-42",
                "event_type": "finish",
                "summary": "Decoded stimulus identity from held-out trials.",
                "scheduler": {"job_id": "998", "state": "completed"},
                "metrics": {"heldout_accuracy": 0.87},
                "artifacts": [
                    {
                        "uri": "file:///scratch/run-42/summary.png",
                        "kind": "figure",
                        "title": "heldout summary",
                        "summary": "Accuracy by stimulus condition.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    summary = watch_manifests(config, root=tmp_path / "outputs")
    second_summary = watch_manifests(config, root=tmp_path / "outputs")
    event = event_from_manifest(config, manifest)

    assert summary["matched"] == 1
    assert len(list(config.outbox_path().glob("*.json"))) == 1
    assert second_summary["matched"] == 1
    assert event["run_id"] == "run-42"
    assert event["artifacts"][0]["summary"] == "Accuracy by stimulus condition."


def test_sync_outbox_uploads_note_and_requests_analysis_graph_draft(
    tmp_path,
    monkeypatch,
) -> None:
    _clear_hpc_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1", cluster="bouchet")
    event, path = finish_event(
        config,
        run_id="run-1",
        exit_code=0,
        artifacts=["file:///scratch/run-1/results.csv"],
        metrics=["accuracy=0.91"],
        summary="Finished decoding analysis.",
    )
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
            assert b"HPC run run-1 finish" in body
            assert b"Finished decoding analysis." in body
            assert b"hpc-outbox" in body
            assert b"hpc_run_id" in body
            assert b"results.csv" in body
            return _json_response(
                201,
                {
                    "data": {
                        "note_id": "note-hpc",
                        "project_id": "project-1",
                        "status": "staged",
                    }
                },
            )
        if request.method == "POST" and request.url.path == "/notes/note-hpc/analysis-graph-drafts":
            return _json_response(
                201,
                {"data": {"change_set_id": "draft-1", "project_id": "project-1"}},
            )
        return _json_response(500, {"error": {"message": "unexpected request"}})

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        summary = sync_outbox(lt, config, request_draft=True)

    synced = read_event(path)
    assert event["run_id"] == "run-1"
    assert summary["errors"] == []
    assert summary["results"][0]["note_id"] == "note-hpc"
    assert summary["results"][0]["change_set_id"] == "draft-1"
    assert synced["sync"]["status"] == "synced"
    assert synced["sync"]["note_id"] == "note-hpc"
    assert synced["sync"]["change_set_id"] == "draft-1"
    assert [request.url.path for request in requests] == [
        "/notes",
        "/notes/upload-file",
        "/notes/note-hpc/analysis-graph-drafts",
    ]


def test_sync_failure_marks_event_failed_and_retries(tmp_path, monkeypatch) -> None:
    _clear_hpc_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1", cluster="generic")
    _event, path = finish_event(config, run_id="run-retry", exit_code=1)

    def failing_handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return _json_response(503, {"error": {"message": "server down"}})

    with LabTracker(
        base_url="http://testserver",
        transport=httpx.MockTransport(failing_handler),
    ) as lt:
        failed = sync_outbox(lt, config)

    assert failed["errors"][0]["error"] == "server down"
    assert read_event(path)["sync"]["status"] == "failed"
    assert read_event(path)["sync"]["attempts"] == 1

    def retry_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/notes":
            return _json_response(
                200,
                {"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}},
            )
        if request.method == "POST" and request.url.path == "/notes/upload-file":
            return _json_response(201, {"data": {"note_id": "note-retry"}})
        return _json_response(500, {"error": {"message": "unexpected request"}})

    with LabTracker(
        base_url="http://testserver",
        transport=httpx.MockTransport(retry_handler),
    ) as lt:
        retried = sync_outbox(lt, config)

    assert retried["errors"] == []
    assert read_event(path)["sync"]["status"] == "synced"
    assert read_event(path)["sync"]["note_id"] == "note-retry"


def _matching_note_payload(path, *, note_id: str) -> dict:
    """A GET /notes record whose evidence key matches the on-disk event."""

    event = read_event(path)
    evidence = render_event_note(event)
    return {
        "note_id": note_id,
        "project_id": str(event["project_id"]),
        "status": "staged",
        "metadata": {
            "evidence_source_provider": "hpc-outbox",
            "evidence_source_external_id": event_source_external_id(event),
            "evidence_content_hash": hashlib.sha256(evidence.encode("utf-8")).hexdigest(),
        },
    }


def test_sync_skips_duplicate_when_server_already_holds_the_evidence(
    tmp_path, monkeypatch
) -> None:
    _clear_hpc_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1", cluster="bouchet")
    _event, path = finish_event(config, run_id="run-dup", exit_code=0)
    existing = _matching_note_payload(path, note_id="note-existing")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/notes":
            return _json_response(
                200,
                {"data": [existing], "meta": {"limit": 200, "offset": 0, "total": 1}},
            )
        return _json_response(500, {"error": {"message": "unexpected request"}})

    with LabTracker(
        base_url="http://testserver", transport=httpx.MockTransport(handler)
    ) as lt:
        summary = sync_outbox(lt, config)

    assert summary["errors"] == []
    assert summary["results"][0]["action"] == "skipped"
    assert summary["results"][0]["reason"] == "duplicate"
    assert summary["results"][0]["note_id"] == "note-existing"
    # The matching note was adopted without any upload.
    assert [request.method for request in requests] == ["GET"]
    synced = read_event(path)
    assert synced["sync"]["status"] == "synced"
    assert synced["sync"]["note_id"] == "note-existing"


def test_second_sync_pass_is_idempotent_with_zero_requests(tmp_path, monkeypatch) -> None:
    _clear_hpc_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1", cluster="bouchet")
    _event, path = finish_event(config, run_id="run-idem", exit_code=0)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/notes":
            return _json_response(
                200,
                {"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}},
            )
        if request.method == "POST" and request.url.path == "/notes/upload-file":
            return _json_response(201, {"data": {"note_id": "note-once"}})
        return _json_response(500, {"error": {"message": "unexpected request"}})

    with LabTracker(
        base_url="http://testserver", transport=httpx.MockTransport(handler)
    ) as lt:
        first = sync_outbox(lt, config)
        requests_after_first = len(requests)
        second = sync_outbox(lt, config)

    assert first["results"][0]["action"] == "imported"
    assert read_event(path)["sync"]["status"] == "synced"
    assert second["results"][0]["action"] == "skipped"
    assert second["results"][0]["reason"] == "already_synced"
    assert second["results"][0]["note_id"] == "note-once"
    # The terminal event short-circuits before any HTTP traffic.
    assert len(requests) == requests_after_first


def test_retry_after_persisted_upload_failure_dedupes_against_server(
    tmp_path, monkeypatch
) -> None:
    _clear_hpc_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1", cluster="bouchet")
    _event, path = finish_event(config, run_id="run-crash", exit_code=0)
    existing = _matching_note_payload(path, note_id="note-persisted")
    state = {"uploaded": False}
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/notes":
            data = [existing] if state["uploaded"] else []
            return _json_response(
                200,
                {"data": data, "meta": {"limit": 200, "offset": 0, "total": len(data)}},
            )
        if request.method == "POST" and request.url.path == "/notes/upload-file":
            # The server persists the note but the client sees a failure.
            state["uploaded"] = True
            return _json_response(503, {"error": {"message": "gateway timeout"}})
        return _json_response(500, {"error": {"message": "unexpected request"}})

    with LabTracker(
        base_url="http://testserver", transport=httpx.MockTransport(handler)
    ) as lt:
        failed = sync_outbox(lt, config)
        retried = sync_outbox(lt, config)

    assert failed["errors"] and read_event(path)["sync"]["attempts"] == 1
    assert retried["errors"] == []
    assert retried["results"][0]["action"] == "skipped"
    assert retried["results"][0]["reason"] == "duplicate"
    # Exactly one upload ever reached the server; the retry adopted the match.
    uploads = [request for request in requests if request.method == "POST"]
    assert len(uploads) == 1
    synced = read_event(path)
    assert synced["sync"]["status"] == "synced"
    assert synced["sync"]["note_id"] == "note-persisted"
