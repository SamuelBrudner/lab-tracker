from __future__ import annotations

import inspect
import json
from hashlib import sha256
from pathlib import Path

import httpx
import pytest

import lab_tracker_client.figure as figure_module
from lab_tracker_client import (
    LabTracker,
    LTValidationError,
    capture,
    capture_figures,
    run_context,
    save_artifact,
    savefig,
)
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
def reset_figure_capture_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _reset_figure_capture_state_for_tests()
    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(tmp_path / "lab-tracker-config"))
    for key in (
        "LAB_TRACKER_PROJECT_ID",
        "LAB_TRACKER_BASE_URL",
        "LAB_TRACKER_MCP_BASE_URL",
        "LAB_TRACKER_ACCESS_TOKEN",
        "LAB_TRACKER_MCP_API_KEY",
        "LAB_TRACKER_MCP_TOKEN",
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


def test_capture_client_resolves_profile_only_connection(tmp_path: Path) -> None:
    config_dir = tmp_path / "lab-tracker-config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "base_url": "http://profile-server:8123",
                "default_project_id": "profile-project",
                "access_token": "profile-token",
            }
        ),
        encoding="utf-8",
    )

    client, project_id, owned = figure_module._resolve_capture_client(
        client=None,
        project_id=None,
    )
    try:
        assert client is not None
        assert client.base_url == "http://profile-server:8123"
        assert client.access_token == "profile-token"
        assert project_id == "profile-project"
        assert owned is True
    finally:
        if client is not None:
            client.close()


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


def test_oversized_capture_streams_hash_without_reading_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # cap1.1: an oversized artifact must never be read into RAM. Force the cap
    # low and make a full read explode; capture must still succeed via streaming.
    monkeypatch.setattr(figure_module, "FIGURE_UPLOAD_MAX_BYTES", 4)
    figure_path = tmp_path / "huge.png"
    figure_path.write_bytes(b"way-bigger-than-the-cap")
    expected_hash = sha256(figure_path.read_bytes()).hexdigest()

    def boom_read_bytes(self: Path) -> bytes:  # pragma: no cover - must not run
        raise AssertionError("oversized capture must not read the file into memory")

    monkeypatch.setattr(Path, "read_bytes", boom_read_bytes)
    metadata_seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        metadata = json.loads(_multipart_field(request.content, "metadata"))
        metadata_seen.update(metadata)
        return _json_response(
            201,
            {"data": {"note_id": "n", "project_id": "project-1", "status": "staged",
                      "metadata": metadata}},
        )

    with LabTracker(
        base_url="http://testserver",
        default_project_id="project-1",
        transport=httpx.MockTransport(handler),
    ) as lt:
        result = savefig(None, figure_path, client=lt)

    assert result.action == "imported"
    assert result.no_preview is True
    assert result.content_hash == expected_hash
    assert metadata_seen["evidence_content_hash"] == expected_hash
    assert metadata_seen["figure_full_size_bytes"] == len(b"way-bigger-than-the-cap")


def test_capture_records_host_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # cap1.3: each capture records the machine that produced it.
    monkeypatch.setenv("LAB_TRACKER_CAPTURE_HOST", "rig-2")
    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(tmp_path / "cfg"))
    metadata_seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        metadata_seen.update(json.loads(_multipart_field(request.content, "metadata")))
        return _json_response(
            201,
            {"data": {"note_id": "n", "project_id": "project-1", "status": "staged",
                      "metadata": metadata_seen}},
        )

    with LabTracker(
        base_url="http://testserver",
        default_project_id="project-1",
        transport=httpx.MockTransport(handler),
    ) as lt:
        savefig(FakeFigure(), tmp_path / "plot.png", client=lt)

    assert metadata_seen["capture_host_label"] == "rig-2"
    assert isinstance(metadata_seen["capture_install_id"], str)
    assert len(str(metadata_seen["capture_install_id"])) == 32
    assert "capture_platform" in metadata_seen
    assert (tmp_path / "cfg" / "install-id").exists()


def test_run_context_records_run_id_and_code_pointer(
    tmp_path: Path,
) -> None:
    # cap1.4: run_context carries a run id and a repo-relative code pointer.
    metadata_seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        metadata_seen.update(json.loads(_multipart_field(request.content, "metadata")))
        return _json_response(
            201,
            {"data": {"note_id": "n", "project_id": "project-1", "status": "staged",
                      "metadata": metadata_seen}},
        )

    with LabTracker(
        base_url="http://testserver",
        default_project_id="project-1",
        transport=httpx.MockTransport(handler),
    ) as lt, run_context():
        savefig(FakeFigure(), tmp_path / "ctx.png", client=lt)

    assert len(str(metadata_seen["run_id"])) == 32
    assert str(metadata_seen["run_code_file"]).endswith("test_lab_tracker_client_figures.py")
    assert "test_run_context_records_run_id_and_code_pointer" in str(
        metadata_seen["run_code_symbol"]
    )
    assert metadata_seen["run_code_line"]
    assert len(str(metadata_seen["run_code_region_hash"])) == 64


def test_capture_generic_kind_namespaces_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # cap1.2: capture(kind=...) captures any file and labels it by kind.
    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(tmp_path / "cfg"))
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        meta = json.loads(_multipart_field(request.content, "metadata"))
        seen["cid"] = _multipart_field(request.content, "client_capture_id")
        seen["meta"] = meta
        return _json_response(
            201,
            {"data": {"note_id": "n", "project_id": "project-1", "status": "staged",
                      "metadata": meta}},
        )

    with LabTracker(
        base_url="http://testserver",
        default_project_id="project-1",
        transport=httpx.MockTransport(handler),
    ) as lt, capture(tmp_path, patterns=["*.csv"], kind="dataset", client=lt):
        (tmp_path / "results.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    meta = seen["meta"]
    assert isinstance(meta, dict)
    assert seen["cid"] == "dataset:results.csv"
    assert meta["evidence_source_provider"] == "local-dataset"
    assert meta["evidence_capture_kind"] == "dataset"
    assert meta["evidence_adapter"] == "lab-tracker-client-dataset"
    assert meta["dataset_content_hash_current"] == meta["evidence_content_hash"]
    assert meta["dataset_no_preview"] is False
    # No leakage of the figure namespace onto a non-figure capture.
    assert not any(str(key).startswith("figure_") for key in meta)


def test_capture_requires_patterns_and_kind(tmp_path: Path) -> None:
    with pytest.raises(LTValidationError):
        capture(tmp_path, patterns=[], kind="dataset")
    with pytest.raises(LTValidationError):
        capture(tmp_path, patterns=["*.csv"], kind="   ")


def test_save_artifact_writes_before_capture_and_records_exact_producer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata_seen: dict[str, object] = {}
    writer_paths: list[Path] = []

    def handler(request: httpx.Request) -> httpx.Response:
        metadata_seen.update(json.loads(_multipart_field(request.content, "metadata")))
        return _json_response(
            201,
            {
                "data": {
                    "note_id": "note-artifact",
                    "project_id": "project-1",
                    "status": "staged",
                    "metadata": metadata_seen,
                }
            },
        )

    def write_csv(path: Path) -> None:
        writer_paths.append(path)
        path.write_text("a,b\n1,2\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    with LabTracker(
        base_url="http://testserver",
        default_project_id="project-1",
        transport=httpx.MockTransport(handler),
    ) as lt:
        expected_line = inspect.currentframe().f_lineno + 1
        result = save_artifact("summary.csv", kind="table", writer=write_csv, client=lt)

    expected_commit = figure_module._git_output(
        "rev-parse", "HEAD", cwd=Path(__file__).resolve().parent
    )
    expected_dirty = bool(
        figure_module._git_output(
            "status", "--porcelain", cwd=Path(__file__).resolve().parent
        )
    )
    assert result.action == "imported"
    assert writer_paths == [(tmp_path / "summary.csv").resolve()]
    assert (tmp_path / "summary.csv").read_text(encoding="utf-8") == "a,b\n1,2\n"
    assert metadata_seen["producer_code_line"] == expected_line
    assert str(metadata_seen["producer_code_file"]).endswith(
        "tests/test_lab_tracker_client_figures.py"
    )
    assert "test_save_artifact_writes_before_capture" in str(
        metadata_seen["producer_code_symbol"]
    )
    assert len(str(metadata_seen["producer_code_region_hash"])) == 64
    assert metadata_seen["producer_git_commit"] == expected_commit
    assert metadata_seen["producer_git_dirty"] is expected_dirty
    assert metadata_seen["evidence_capture_kind"] == "table"
    assert metadata_seen["evidence_source_uri"] == (tmp_path / "summary.csv").as_uri()


def test_save_artifact_omits_dirty_flag_when_git_status_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata_seen: dict[str, object] = {}
    original = figure_module._git_output_checked

    def git_output(*args: str, cwd: str | Path | None = None) -> str | None:
        if args == ("status", "--porcelain"):
            return None
        return original(*args, cwd=cwd)

    def handler(request: httpx.Request) -> httpx.Response:
        metadata_seen.update(json.loads(_multipart_field(request.content, "metadata")))
        return _json_response(
            201,
            {
                "data": {
                    "note_id": "note-unknown-git-state",
                    "project_id": "project-1",
                    "status": "staged",
                    "metadata": metadata_seen,
                }
            },
        )

    monkeypatch.setattr(figure_module, "_git_output_checked", git_output)
    with LabTracker(
        base_url="http://testserver",
        default_project_id="project-1",
        transport=httpx.MockTransport(handler),
    ) as lt:
        result = save_artifact(
            tmp_path / "unknown.csv",
            kind="table",
            writer=lambda path: path.write_text("value\n1\n", encoding="utf-8"),
            client=lt,
        )

    assert result.action == "imported"
    assert "producer_git_dirty" not in metadata_seen


def test_save_artifact_reuses_the_logical_capture_id_for_idempotent_writes(
    tmp_path: Path,
) -> None:
    post_ids: list[str] = []
    first_metadata: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        client_capture_id = _multipart_field(request.content, "client_capture_id")
        post_ids.append(client_capture_id)
        metadata = json.loads(_multipart_field(request.content, "metadata"))
        if not first_metadata:
            first_metadata.update(metadata)
        return _json_response(
            201 if len(post_ids) == 1 else 200,
            {
                "data": {
                    "note_id": "note-idempotent",
                    "project_id": "project-1",
                    "status": "staged",
                    "metadata": first_metadata,
                }
            },
        )

    output = tmp_path / "stable.csv"
    with LabTracker(
        base_url="http://testserver",
        default_project_id="project-1",
        transport=httpx.MockTransport(handler),
    ) as lt:
        first = save_artifact(
            output,
            kind="table",
            writer=lambda path: path.write_text("value\n1\n", encoding="utf-8"),
            client=lt,
        )
        second = save_artifact(
            output,
            kind="table",
            writer=lambda path: path.write_text("value\n1\n", encoding="utf-8"),
            client=lt,
        )

    assert first.action == "imported"
    assert second.action == "coalesced"
    assert post_ids == ["table:stable.csv", "table:stable.csv"]
    assert first.client_capture_id == second.client_capture_id


def test_savefig_records_the_external_save_call_not_wrapper_internals(tmp_path: Path) -> None:
    metadata_seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        metadata_seen.update(json.loads(_multipart_field(request.content, "metadata")))
        return _json_response(
            201,
            {
                "data": {
                    "note_id": "note-figure-producer",
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
        expected_line = inspect.currentframe().f_lineno + 1
        savefig(FakeFigure(), tmp_path / "producer.png", client=lt)

    assert metadata_seen["producer_code_line"] == expected_line
    assert str(metadata_seen["producer_code_file"]).endswith(
        "tests/test_lab_tracker_client_figures.py"
    )
    assert "test_savefig_records_the_external_save_call" in str(
        metadata_seen["producer_code_symbol"]
    )
    assert metadata_seen["evidence_capture_kind"] == "figure"
    assert not any(key.startswith("capture_scope_") for key in metadata_seen)


def test_save_artifact_supports_binary_and_pointer_only_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(figure_module, "FIGURE_UPLOAD_MAX_BYTES", 4)
    payload = b"binary-model-payload"
    metadata_seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert payload not in request.content
        metadata_seen.update(json.loads(_multipart_field(request.content, "metadata")))
        return _json_response(
            201,
            {
                "data": {
                    "note_id": "note-model",
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
        result = save_artifact(
            tmp_path / "model.bin",
            kind="model",
            writer=lambda path: path.write_bytes(payload),
            client=lt,
        )

    assert result.action == "imported"
    assert result.no_preview is True
    assert result.content_hash == sha256(payload).hexdigest()
    assert metadata_seen["model_full_size_bytes"] == len(payload)
    assert metadata_seen["model_no_preview"] is True


def test_save_artifact_writer_failure_propagates_without_capture(tmp_path: Path) -> None:
    api_calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal api_calls
        api_calls += 1
        raise AssertionError("the API must not run after a failed writer")

    def failed_writer(_path: Path) -> None:
        raise RuntimeError("writer failed")

    with LabTracker(
        base_url="http://testserver",
        default_project_id="project-1",
        transport=httpx.MockTransport(handler),
    ) as lt, pytest.raises(RuntimeError, match="writer failed"):
        save_artifact(
            tmp_path / "missing.csv",
            kind="table",
            writer=failed_writer,
            client=lt,
        )

    assert api_calls == 0
    assert not (tmp_path / "missing.csv").exists()


def test_save_artifact_capture_failure_never_removes_successful_output(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(500, {"error": {"message": "capture unavailable"}})

    output = tmp_path / "kept.txt"
    with LabTracker(
        base_url="http://testserver",
        default_project_id="project-1",
        transport=httpx.MockTransport(handler),
    ) as lt:
        result = save_artifact(
            output,
            kind="text-output",
            writer=lambda path: path.write_text("keep me", encoding="utf-8"),
            client=lt,
        )

    assert result.action == "failed"
    assert output.read_text(encoding="utf-8") == "keep me"


def test_capture_scan_records_scope_not_fabricated_producer(tmp_path: Path) -> None:
    metadata_seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        metadata_seen.update(json.loads(_multipart_field(request.content, "metadata")))
        return _json_response(
            201,
            {
                "data": {
                    "note_id": "note-scanned",
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
        context = capture(tmp_path, patterns=["*.csv"], kind="table", client=lt)
        expected_line = inspect.currentframe().f_lineno + 1
        with context:
            (tmp_path / "scanned.csv").write_text("value\n1\n", encoding="utf-8")

    assert metadata_seen["capture_scope_code_line"] == expected_line
    assert str(metadata_seen["capture_scope_code_file"]).endswith(
        "tests/test_lab_tracker_client_figures.py"
    )
    assert "capture_scope_git_commit" in metadata_seen
    assert not any(key.startswith("producer_") for key in metadata_seen)


def test_capture_context_eager_writes_keep_distinct_producer_lines_and_no_duplicates(
    tmp_path: Path,
) -> None:
    uploaded: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        metadata = json.loads(_multipart_field(request.content, "metadata"))
        uploaded.append(metadata)
        return _json_response(
            201,
            {
                "data": {
                    "note_id": f"note-{len(uploaded)}",
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
    ) as lt, capture(tmp_path, patterns=["*.csv"], kind="table", client=lt) as context:
        first_line = inspect.currentframe().f_lineno + 1
        context.save_artifact(tmp_path / "first.csv", writer=lambda p: p.write_text("1"))
        second_line = inspect.currentframe().f_lineno + 1
        context.save_artifact(tmp_path / "second.csv", writer=lambda p: p.write_text("2"))

    assert len(context.results) == 2
    assert len(uploaded) == 2
    assert [metadata["producer_code_line"] for metadata in uploaded] == [
        first_line,
        second_line,
    ]
    assert all("capture_scope_code_line" in metadata for metadata in uploaded)


def test_save_artifact_validates_kind_and_writer_before_writing(tmp_path: Path) -> None:
    wrote = False

    def writer(_path: Path) -> None:
        nonlocal wrote
        wrote = True

    with pytest.raises(LTValidationError, match="non-empty kind"):
        save_artifact(tmp_path / "bad", kind=" ", writer=writer)
    with pytest.raises(LTValidationError, match="callable writer"):
        save_artifact(tmp_path / "bad", kind="table", writer=None)  # type: ignore[arg-type]

    assert wrote is False
