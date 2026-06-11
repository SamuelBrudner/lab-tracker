from __future__ import annotations

import argparse

import httpx

from lab_tracker_client import LabTracker
from lab_tracker_client import cli as lt_cli


def _json_response(status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def _args(**overrides: object) -> argparse.Namespace:
    values = {
        "adapter_name": "test-import-folder",
        "dry_run": False,
        "exclude": [],
        "include": [],
        "limit": None,
        "project": "project-1",
        "provider": "local-folder",
        "root": "",
        "status": "staged",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_import_folder_filters_limit_and_dry_run(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "ignored.tmp").write_text("ignore", encoding="utf-8")
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "b.md").write_text("bravo", encoding="utf-8")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/notes":
            return _json_response(
                200,
                {"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}},
            )
        return _json_response(500, {"error": {"message": "unexpected request"}})

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        summary = lt_cli._cmd_import_folder(
            lt,
            _args(
                root=str(tmp_path),
                include=["*.txt", "sub/*.md"],
                exclude=["*.tmp"],
                dry_run=True,
                limit=1,
            ),
        )

    assert summary["matched"] == 2
    assert summary["processed"] == 1
    assert summary["imported"] == []
    assert summary["errors"] == []
    assert summary["skipped"][0]["reason"] == "dry_run"
    assert summary["skipped"][0]["source_external_id"] == "a.txt"
    assert all(request.method == "GET" for request in requests)


def test_import_folder_imports_files_with_summary(tmp_path) -> None:
    evidence_path = tmp_path / "capture.md"
    evidence_path.write_text("capture text", encoding="utf-8")
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
            assert b"evidence_source_provider" in body
            assert b"local-folder" in body
            assert b"capture.md" in body
            assert b"staged" in body
            return _json_response(
                201,
                {
                    "data": {
                        "note_id": "note-imported",
                        "project_id": "project-1",
                        "status": "staged",
                    }
                },
            )
        return _json_response(404, {"error": {"message": "not found"}})

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        summary = lt_cli._cmd_import_folder(lt, _args(root=str(tmp_path)))

    assert summary["matched"] == 1
    assert summary["processed"] == 1
    assert summary["skipped"] == []
    assert summary["errors"] == []
    assert summary["imported"][0]["note_id"] == "note-imported"
    assert summary["imported"][0]["source_external_id"] == "capture.md"
    assert [request.method for request in requests] == ["GET", "POST"]
