from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import httpx


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "import-evidence-note.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("import_evidence_note", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _args(**overrides: object) -> argparse.Namespace:
    values = {
        "adapter": "test-importer",
        "capture_kind": None,
        "content_type": None,
        "file": None,
        "metadata_json": None,
        "note_status": "committed",
        "observed_at": "2026-05-21T18:00:00+00:00",
        "project_id": "project-1",
        "source_external_id": None,
        "source_provider": "google-drive",
        "source_uri": "gdrive://item-1",
        "text": None,
        "title": None,
        "transcribed_text": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_importer_creates_text_note_with_evidence_metadata() -> None:
    script = _load_script()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"data": {"note_id": "note-1"}})

    client = httpx.Client(base_url="http://testserver", transport=httpx.MockTransport(handler))
    note = script._create_text_note(
        client,
        _args(text="Daily note text", title="Daily text"),
        headers={"Authorization": "Bearer test"},
    )

    assert note["note_id"] == "note-1"
    request = requests[0]
    assert request.url.path == "/notes"
    payload = json.loads(request.content.decode("utf-8"))
    assert payload["raw_content"] == "Daily note text"
    assert payload["metadata"]["evidence_source_provider"] == "google-drive"
    assert payload["metadata"]["evidence_source_uri"] == "gdrive://item-1"
    assert payload["metadata"]["evidence_capture_kind"] == "text"
    assert payload["metadata"]["evidence_adapter"] == "test-importer"
    assert payload["metadata"]["evidence_title"] == "Daily text"
    assert payload["metadata"]["evidence_content_hash"]
    client.close()


def test_importer_uploads_file_note_with_evidence_metadata(tmp_path: Path) -> None:
    script = _load_script()
    evidence_path = tmp_path / "bench.md"
    evidence_path.write_text("bench observation", encoding="utf-8")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"data": {"note_id": "note-2"}})

    client = httpx.Client(base_url="http://testserver", transport=httpx.MockTransport(handler))
    note = script._upload_file_note(
        client,
        _args(file=evidence_path, capture_kind="notebook_photo", content_type="text/markdown"),
        headers={},
    )

    assert note["note_id"] == "note-2"
    request = requests[0]
    body = request.content
    assert request.url.path == "/notes/upload-file"
    assert b"bench observation" in body
    assert b"evidence_source_provider" in body
    assert b"google-drive" in body
    assert b"notebook_photo" in body
    client.close()
