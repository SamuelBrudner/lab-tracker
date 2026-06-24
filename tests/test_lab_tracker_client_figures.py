from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import httpx
import pytest

import lab_tracker_client.figure as figure_module
from lab_tracker_client import LabTracker, capture_figures, run_context, savefig
from lab_tracker_client.figure import (
    FIGURE_CAPTURE_TIMEOUT_SECONDS,
    _reset_figure_capture_state_for_tests,
)


class FakeFigure:
    def __init__(self, payload: bytes = b"figure-bytes") -> None:
        self.payload = payload
        self.savefig_kwargs: dict[str, object] = {}

    def savefig(self, path: str | Path, **kwargs: object) -> None:
        self.savefig_kwargs = dict(kwargs)
        Path(path).write_bytes(self.payload)


def _json_response(status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def _multipart_field(body: bytes, name: str) -> str:
    marker = f'name="{name}"'.encode()
    assert marker in body
    chunk = body.split(marker, 1)[1]
    chunk = chunk.split(b"\r\n\r\n", 1)[1]
    return chunk.split(b"\r\n--", 1)[0].decode("utf-8")


@pytest.fixture(autouse=True)
def reset_figure_capture_state(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_figure_capture_state_for_tests()
    for key in (
        "LAB_TRACKER_PROJECT_ID",
        "LAB_TRACKER_BASE_URL",
        "LAB_TRACKER_MCP_BASE_URL",
        "LAB_TRACKER_ACCESS_TOKEN",
        "LAB_TRACKER_USERNAME",
        "LAB_TRACKER_PASSWORD",
        "LAB_TRACKER_MCP_USERNAME",
        "LAB_TRACKER_MCP_PASSWORD",
    ):
        monkeypatch.delenv(key, raising=False)
    yield
    _reset_figure_capture_state_for_tests()


def test_savefig_forwards_kwargs_and_uploads_under_cap(tmp_path: Path) -> None:
    figure_path = tmp_path / "plot.png"
    seen: list[httpx.Request] = []
    metadata_seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "POST"
        assert request.url.path == "/notes/upload-file"
        body = request.content
        assert b"figure-bytes" in body
        assert b"analysis_id" not in body
        assert b"dataset_id" not in body
        assert _multipart_field(body, "project_id") == "project-1"
        assert _multipart_field(body, "status") == "staged"
        assert _multipart_field(body, "client_capture_id") == "figure:plot.png"
        metadata_seen.update(json.loads(_multipart_field(body, "metadata")))
        return _json_response(
            201,
            {
                "data": {
                    "note_id": "note-figure",
                    "project_id": "project-1",
                    "status": "staged",
                    "metadata": metadata_seen,
                }
            },
        )

    fig = FakeFigure()
    with LabTracker(
        base_url="http://testserver",
        default_project_id="project-1",
        transport=httpx.MockTransport(handler),
    ) as lt:
        result = savefig(fig, figure_path, client=lt, dpi=200, bbox_inches="tight")

    full_hash = sha256(b"figure-bytes").hexdigest()
    assert fig.savefig_kwargs == {"dpi": 200, "bbox_inches": "tight"}
    assert result.action == "imported"
    assert result.note and result.note.id == "note-figure"
    assert result.no_preview is False
    assert metadata_seen["evidence_source_uri"] == figure_path.resolve().as_uri()
    assert metadata_seen["evidence_content_hash"] == full_hash
    assert metadata_seen["figure_content_hash_current"] == full_hash
    assert len(seen) == 1


def test_pdf_savefig_uploads_rendered_png_preview_under_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    figure_path = tmp_path / "plot.pdf"
    raw_pdf = b"%PDF-raw-figure"
    rendered_png = b"\x89PNG\r\nrendered-preview"
    bodies: list[bytes] = []
    metadata_seen: dict[str, object] = {}

    def fake_render_pdf_preview_png(*, fig: object, path: Path, max_bytes: int) -> bytes:
        assert isinstance(fig, FakeFigure)
        assert path == figure_path.resolve()
        assert max_bytes > len(rendered_png)
        return rendered_png

    monkeypatch.setattr(figure_module, "_render_pdf_preview_png", fake_render_pdf_preview_png)

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        metadata_seen.update(json.loads(_multipart_field(request.content, "metadata")))
        return _json_response(
            201,
            {
                "data": {
                    "note_id": "note-pdf",
                    "project_id": "project-1",
                    "status": "staged",
                    "metadata": metadata_seen,
                }
            },
        )

    with LabTracker(
        base_url="http://testserver",
        default_project_id="project-1",
        transport=httpx.MockTransport(handler),
    ) as lt:
        result = savefig(FakeFigure(raw_pdf), figure_path, client=lt)

    full_hash = sha256(raw_pdf).hexdigest()
    assert result.action == "imported"
    assert result.no_preview is False
    assert metadata_seen["evidence_content_hash"] == full_hash
    assert metadata_seen["figure_no_preview"] is False
    assert metadata_seen["figure_preview_size_bytes"] == len(rendered_png)
    assert b'filename="plot.pdf.preview.png"' in bodies[0]
    assert b"Content-Type: image/png" in bodies[0]
    assert rendered_png in bodies[0]
    assert raw_pdf not in bodies[0]


def test_pdf_without_renderer_uploads_pointer_only_under_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    figure_path = tmp_path / "plot.pdf"
    raw_pdf = b"%PDF-without-renderer"
    figure_path.write_bytes(raw_pdf)
    bodies: list[bytes] = []

    monkeypatch.setattr(figure_module, "_render_pdf_preview_png", lambda **_kwargs: None)

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        metadata = json.loads(_multipart_field(request.content, "metadata"))
        assert metadata["figure_no_preview"] is True
        return _json_response(
            201,
            {
                "data": {
                    "note_id": "note-pdf-pointer",
                    "project_id": "project-1",
                    "status": "staged",
                    "metadata": metadata,
                }
            },
        )

    with LabTracker(
        base_url="http://testserver",
        default_project_id="project-1",
        transport=httpx.MockTransport(handler),
    ) as lt:
        result = savefig(None, figure_path, client=lt)

    assert result.action == "imported"
    assert result.no_preview is True
    assert b'filename="plot.pdf.pointer.txt"' in bodies[0]
    assert b"Content-Type: text/plain" in bodies[0]
    assert b"Lab Tracker figure pointer" in bodies[0]
    assert raw_pdf not in bodies[0]
    assert "PDF preview" in capsys.readouterr().err


def test_savefig_clamps_supplied_client_timeout_for_capture_calls(tmp_path: Path) -> None:
    figure_path = tmp_path / "plot.png"
    seen_timeout: dict[str, float] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_timeout.update(request.extensions["timeout"])
        metadata = json.loads(_multipart_field(request.content, "metadata"))
        return _json_response(
            201,
            {
                "data": {
                    "note_id": "note-timeout",
                    "project_id": "project-1",
                    "status": "staged",
                    "metadata": metadata,
                }
            },
        )

    with LabTracker(
        base_url="http://testserver",
        default_project_id="project-1",
        timeout_seconds=15,
        transport=httpx.MockTransport(handler),
    ) as lt:
        result = savefig(FakeFigure(), figure_path, client=lt)
        restored_timeout = lt._client.timeout

    assert result.action == "imported"
    assert seen_timeout
    assert all(value <= FIGURE_CAPTURE_TIMEOUT_SECONDS for value in seen_timeout.values())
    assert restored_timeout.connect == 15


def test_savefig_is_fail_soft_and_circuit_breaker_short_circuits(tmp_path: Path) -> None:
    error_path = tmp_path / "error.png"

    def error_handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(500, {"error": {"message": "server unavailable"}})

    with LabTracker(
        base_url="http://testserver",
        default_project_id="project-1",
        transport=httpx.MockTransport(error_handler),
    ) as lt:
        failed = savefig(FakeFigure(), error_path, client=lt)

    assert error_path.read_bytes() == b"figure-bytes"
    assert failed.action == "failed"
    assert failed.errors
    assert FIGURE_CAPTURE_TIMEOUT_SECONDS <= 3

    def forbidden_handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(403, {"error": {"message": "not a project contributor"}})

    with LabTracker(
        base_url="http://testserver",
        default_project_id="project-1",
        transport=httpx.MockTransport(forbidden_handler),
    ) as lt:
        forbidden = savefig(FakeFigure(), tmp_path / "forbidden.png", client=lt)

    assert forbidden.action == "failed"
    assert forbidden.errors

    _reset_figure_capture_state_for_tests()
    attempts = 0

    def connect_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("offline", request=request)

    with LabTracker(
        base_url="http://testserver",
        default_project_id="project-1",
        transport=httpx.MockTransport(connect_handler),
    ) as lt:
        first = savefig(FakeFigure(b"one"), tmp_path / "one.png", client=lt)
        second = savefig(FakeFigure(b"two"), tmp_path / "two.png", client=lt)

    assert first.action == "failed"
    assert second.action == "skipped"
    assert second.reason == "circuit_open"
    assert attempts == 1


def test_circuit_open_skips_env_client_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def connect_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with LabTracker(
        base_url="http://testserver",
        default_project_id="project-1",
        transport=httpx.MockTransport(connect_handler),
    ) as lt:
        failed = savefig(FakeFigure(b"one"), tmp_path / "one.png", client=lt)

    assert failed.action == "failed"
    monkeypatch.setenv("LAB_TRACKER_PROJECT_ID", "project-1")
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "http://testserver")
    monkeypatch.setenv("LAB_TRACKER_ACCESS_TOKEN", "token")

    class ForbiddenAutoClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("circuit-open capture should not build an env client")

    monkeypatch.setattr(figure_module, "LabTracker", ForbiddenAutoClient)

    skipped = savefig(FakeFigure(b"two"), tmp_path / "two.png")

    assert skipped.action == "skipped"
    assert skipped.reason == "circuit_open"


def test_unconfigured_savefig_warns_once_without_network(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for index in range(10):
        result = savefig(FakeFigure(), tmp_path / f"plot-{index}.png")
        assert result.action == "skipped"
        assert result.reason == "unconfigured"

    stderr = capsys.readouterr().err
    assert stderr.count("Lab Tracker figure capture is unconfigured") == 1


def test_changed_coalesced_capture_patches_metadata_preserving_first_evidence_keys(
    tmp_path: Path,
) -> None:
    figure_path = tmp_path / "plot.png"
    old_metadata = {
        "evidence_source_provider": "local-figure",
        "evidence_source_uri": "file:///first/plot.png",
        "evidence_source_external_id": "figure:plot.png",
        "evidence_source_observed_at": "2026-01-01T00:00:00+00:00",
        "evidence_capture_kind": "figure",
        "evidence_content_hash": "oldhash",
        "evidence_adapter": "lab-tracker-client-figure",
        "evidence_title": "plot.png",
        "figure_no_preview": True,
        "figure_preview_size_bytes": 321,
        "kept": "yes",
    }
    patched_metadata: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/notes/upload-file":
            return _json_response(
                200,
                {
                    "data": {
                        "note_id": "note-existing",
                        "project_id": "project-1",
                        "status": "staged",
                        "metadata": old_metadata,
                    }
                },
            )
        if request.method == "PATCH" and request.url.path == "/notes/note-existing":
            patched_metadata.update(json.loads(request.content.decode("utf-8"))["metadata"])
            return _json_response(
                200,
                {
                    "data": {
                        "note_id": "note-existing",
                        "project_id": "project-1",
                        "status": "staged",
                        "metadata": patched_metadata,
                    }
                },
            )
        return _json_response(404, {"error": {"message": "not found"}})

    with LabTracker(
        base_url="http://testserver",
        default_project_id="project-1",
        transport=httpx.MockTransport(handler),
    ) as lt:
        result = savefig(FakeFigure(b"new-bytes"), figure_path, client=lt)

    new_hash = sha256(b"new-bytes").hexdigest()
    assert result.action == "coalesced"
    assert result.stale_review_bytes is True
    assert patched_metadata["evidence_source_uri"] == "file:///first/plot.png"
    assert patched_metadata["evidence_content_hash"] == "oldhash"
    assert patched_metadata["figure_content_hash_current"] == new_hash
    assert patched_metadata["content_hash_current"] == new_hash
    assert patched_metadata["figure_no_preview"] is True
    assert patched_metadata["figure_preview_size_bytes"] == 321
    assert patched_metadata["figure_review_bytes_stale"] is True
    assert patched_metadata["kept"] == "yes"


def test_coalesced_metadata_patch_transport_failure_opens_circuit(tmp_path: Path) -> None:
    figure_path = tmp_path / "plot.png"
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "POST" and request.url.path == "/notes/upload-file":
            posts += 1
            return _json_response(
                200,
                {
                    "data": {
                        "note_id": "note-existing",
                        "project_id": "project-1",
                        "status": "staged",
                        "metadata": {"evidence_content_hash": "oldhash"},
                    }
                },
            )
        if request.method == "PATCH" and request.url.path == "/notes/note-existing":
            raise httpx.ConnectError("offline", request=request)
        return _json_response(404, {"error": {"message": "not found"}})

    with LabTracker(
        base_url="http://testserver",
        default_project_id="project-1",
        transport=httpx.MockTransport(handler),
    ) as lt:
        first = savefig(FakeFigure(b"one"), figure_path, client=lt)
        second = savefig(FakeFigure(b"two"), tmp_path / "second.png", client=lt)

    assert first.action == "coalesced"
    assert first.reason == "metadata_patch_failed"
    assert first.stale_review_bytes is True
    assert second.action == "skipped"
    assert second.reason == "circuit_open"
    assert posts == 1


def test_over_cap_without_renderer_uploads_pointer_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    figure_path = tmp_path / "big.png"
    figure_path.write_bytes(b"too-large-for-preview")
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        metadata = json.loads(_multipart_field(request.content, "metadata"))
        assert metadata["figure_no_preview"] is True
        return _json_response(
            201,
            {
                "data": {
                    "note_id": "note-pointer",
                    "project_id": "project-1",
                    "status": "staged",
                    "metadata": metadata,
                }
            },
        )

    with LabTracker(
        base_url="http://testserver",
        default_project_id="project-1",
        transport=httpx.MockTransport(handler),
    ) as lt:
        result = savefig(None, figure_path, client=lt, preview_max_bytes=2)

    assert result.action == "imported"
    assert result.no_preview is True
    assert b"too-large-for-preview" not in bodies[0]
    assert b"Lab Tracker figure pointer" in bodies[0]
    assert "bounded preview" in capsys.readouterr().err


def test_preview_cap_is_clamped_to_server_upload_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(figure_module, "FIGURE_UPLOAD_MAX_BYTES", 300)
    figure_path = tmp_path / "big.png"
    figure_path.write_bytes(b"x" * 400)
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        metadata = json.loads(_multipart_field(request.content, "metadata"))
        assert metadata["figure_no_preview"] is True
        return _json_response(
            201,
            {
                "data": {
                    "note_id": "note-pointer",
                    "project_id": "project-1",
                    "status": "staged",
                    "metadata": metadata,
                }
            },
        )

    with LabTracker(
        base_url="http://testserver",
        default_project_id="project-1",
        transport=httpx.MockTransport(handler),
    ) as lt:
        result = savefig(None, figure_path, client=lt, preview_max_bytes=10_000)

    assert result.action == "imported"
    assert result.no_preview is True
    assert b"x" * 100 not in bodies[0]
    assert b"Lab Tracker figure pointer" in bodies[0]


def test_run_context_adds_scalar_metadata_and_expires(tmp_path: Path) -> None:
    metadata_payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        metadata = json.loads(_multipart_field(request.content, "metadata"))
        metadata_payloads.append(metadata)
        return _json_response(
            201,
            {
                "data": {
                    "note_id": f"note-{len(metadata_payloads)}",
                    "project_id": "project-1",
                    "status": "staged",
                    "metadata": metadata,
                }
            },
        )

    with LabTracker(
        base_url="http://testserver",
        default_project_id="project-1",
        transport=httpx.MockTransport(handler),
    ) as lt:
        with run_context(ttl_seconds=60, extra={"trial": 7}):
            savefig(FakeFigure(b"with-context"), tmp_path / "context.png", client=lt)
        with run_context(ttl_seconds=0, extra={"expired": "yes"}):
            savefig(FakeFigure(b"expired-context"), tmp_path / "expired.png", client=lt)

    assert metadata_payloads[0]["run_trial"] == 7
    assert isinstance(metadata_payloads[0]["run_git_dirty"], bool)
    assert "run_expired" not in metadata_payloads[1]


def test_capture_figures_captures_new_and_modified_files_after_body_exception(
    tmp_path: Path,
) -> None:
    untouched = tmp_path / "untouched.png"
    modified = tmp_path / "modified.png"
    untouched.write_bytes(b"same")
    modified.write_bytes(b"before")
    uploaded_names: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        uploaded_names.append(_multipart_field(request.content, "client_capture_id"))
        metadata = json.loads(_multipart_field(request.content, "metadata"))
        return _json_response(
            201,
            {
                "data": {
                    "note_id": f"note-{len(uploaded_names)}",
                    "project_id": "project-1",
                    "status": "staged",
                    "metadata": metadata,
                }
            },
        )

    with LabTracker(
        base_url="http://testserver",
        default_project_id="project-1",
        transport=httpx.MockTransport(handler),
    ) as lt:
        context = capture_figures(tmp_path, client=lt)
        with pytest.raises(RuntimeError, match="boom"), context:
            modified.write_bytes(b"after")
            (tmp_path / "new.png").write_bytes(b"new")
            raise RuntimeError("boom")

    assert {Path(result.path).name for result in context.results} == {"modified.png", "new.png"}
    assert "figure:modified.png" in uploaded_names
    assert "figure:new.png" in uploaded_names
    assert "figure:untouched.png" not in uploaded_names
