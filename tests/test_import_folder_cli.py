from __future__ import annotations

import argparse
import json

import httpx
import pytest

from lab_tracker_client import LabTracker, file_sha256
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
    assert summary["skipped"][0]["source_external_id"] == lt_cli._local_folder_external_id(
        tmp_path.resolve(),
        "a.txt",
    )
    assert all(request.method == "GET" for request in requests)


def test_import_folder_discovery_skips_symlinked_files(tmp_path) -> None:
    root = tmp_path / "inbox"
    root.mkdir()
    real_file = root / "capture.md"
    real_file.write_text("capture text", encoding="utf-8")
    outside_file = tmp_path / "outside-secret.txt"
    outside_file.write_text("secret text", encoding="utf-8")
    symlink = root / "linked-secret.txt"
    try:
        symlink.symlink_to(outside_file)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    discovered = lt_cli._discover_import_files(
        root,
        include_patterns=["*"],
        exclude_patterns=[],
    )

    assert discovered == [real_file]


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
    assert summary["imported"][0][
        "source_external_id"
    ] == lt_cli._local_folder_external_id(tmp_path.resolve(), "capture.md")
    assert [request.method for request in requests] == ["GET", "POST"]


def test_import_folder_prefetches_notes_once_for_batch_dedupe(tmp_path) -> None:
    duplicate_path = tmp_path / "duplicate.md"
    duplicate_path.write_text("same content", encoding="utf-8")
    new_path = tmp_path / "new.md"
    new_path.write_text("new content", encoding="utf-8")
    duplicate_external_id = lt_cli._local_folder_external_id(
        tmp_path.resolve(),
        "duplicate.md",
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/notes":
            return _json_response(
                200,
                {
                    "data": [
                        {
                            "note_id": "note-existing",
                            "metadata": {
                                "evidence_source_provider": "local-folder",
                                "evidence_source_external_id": duplicate_external_id,
                                "evidence_content_hash": file_sha256(duplicate_path),
                            },
                        }
                    ],
                    "meta": {"limit": 200, "offset": 0, "total": 1},
                },
            )
        if request.method == "POST" and request.url.path == "/notes/upload-file":
            return _json_response(
                201,
                {"data": {"note_id": "note-new", "project_id": "project-1"}},
            )
        return _json_response(404, {"error": {"message": "not found"}})

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        summary = lt_cli._cmd_import_folder(lt, _args(root=str(tmp_path)))

    assert [request.method for request in requests] == ["GET", "POST"]
    assert summary["skipped"][0]["note_id"] == "note-existing"
    assert summary["imported"][0]["note_id"] == "note-new"


def test_import_folder_main_exits_nonzero_after_error_summary(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "capture.md").write_text("capture text", encoding="utf-8")

    class FailingClient:
        def build_evidence_note_index(self, *, project_id: str):  # noqa: ARG002
            return {}

        def import_evidence_file(self, **_kwargs):
            raise RuntimeError("server down")

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        lt_cli.LabTracker,
        "from_env",
        classmethod(lambda cls: FailingClient()),  # noqa: ARG005
    )

    with pytest.raises(SystemExit) as exc_info:
        lt_cli.main(["import-folder", "--project", "project-1", "--root", str(tmp_path)])

    assert exc_info.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "import-folder"
    assert payload["errors"][0]["error"] == "server down"
