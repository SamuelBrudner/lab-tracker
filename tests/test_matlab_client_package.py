from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATLAB_ROOT = ROOT / "matlab"
PACKAGE_ROOT = MATLAB_ROOT / "+labtracker"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_matlab_package_files_exist() -> None:
    expected = {
        "+labtracker/Client.m",
        "+labtracker/fromEnv.m",
        "+labtracker/savefig.m",
        "+labtracker/uploadFigure.m",
        "+labtracker/+internal/sha256File.m",
        "+labtracker/+internal/fileUri.m",
        "+labtracker/+internal/figureMetadata.m",
        "examples/capture_figure_smoke.m",
    }

    assert expected <= {
        path.relative_to(MATLAB_ROOT).as_posix()
        for path in MATLAB_ROOT.rglob("*.m")
    }


def test_matlab_client_talks_to_retained_api_without_python_bridge() -> None:
    source = _read(PACKAGE_ROOT / "Client.m")
    combined = "\n".join(_read(path) for path in MATLAB_ROOT.rglob("*.m"))

    assert "/notes/upload-file" in source
    assert "/auth/login" in source
    assert "LAB_TRACKER_ACCESS_TOKEN" in source
    assert "LAB_TRACKER_PROJECT_ID" in source
    assert "X-LabTracker-Surface" in source
    assert "MultipartFormProvider" in source
    assert "FileProvider" in source
    assert "py." not in combined
    assert "pyrun" not in combined
    assert "system('python" not in combined
    assert 'system("python' not in combined


def test_matlab_figure_capture_records_python_client_compatible_metadata() -> None:
    metadata_source = _read(PACKAGE_ROOT / "+internal" / "figureMetadata.m")
    upload_source = _read(PACKAGE_ROOT / "uploadFigure.m")
    savefig_source = _read(PACKAGE_ROOT / "savefig.m")

    for field in (
        "evidence_source_provider",
        "evidence_source_uri",
        "evidence_source_external_id",
        "evidence_source_observed_at",
        "evidence_capture_kind",
        "evidence_content_hash",
        "evidence_adapter",
        "evidence_title",
        "figure_content_hash_current",
        "figure_client_capture_id",
    ):
        assert field in metadata_source
    assert "local-figure" in metadata_source
    assert "lab-tracker-matlab-figure" in metadata_source
    assert "client_capture_id" in _read(PACKAGE_ROOT / "Client.m")
    assert "unconfigured" in upload_source
    assert "capture_failed" in upload_source
    assert "exportgraphics" in savefig_source
    assert "saveas" in savefig_source


def test_matlab_docs_are_linked_from_public_docs() -> None:
    readme = _read(ROOT / "README.md")
    retained_surface = _read(ROOT / "docs" / "retained-v1-surface.md")
    matlab_doc = _read(ROOT / "docs" / "lab-tracker-matlab.md")

    assert "docs/lab-tracker-matlab.md" in readme
    assert "labtracker.savefig" in retained_surface
    assert "without installing\nor calling Python" in matlab_doc
    assert "LAB_TRACKER_ACCESS_TOKEN" in matlab_doc
    assert "capture_figure_smoke.m" in matlab_doc


def test_matlab_runtime_smoke_runner_and_runbook_exist() -> None:
    """The MATLAB runtime path is either executable or a documented deferral.

    Guards the tqyw contract: a guarded runner that no-ops without a MATLAB
    license, and a manual runbook, both asserting a real success action so a
    fail-soft (skipped/failed) capture cannot pass as green.
    """
    runner = ROOT / "scripts" / "matlab-smoke.sh"
    assert runner.exists(), "scripts/matlab-smoke.sh (the guarded MATLAB runner) is missing"
    runner_source = _read(runner)
    # Skips green without MATLAB, runs the example, and checks the success action.
    assert "command -v matlab" in runner_source
    assert "capture_figure_smoke.m" in runner_source
    assert "imported" in runner_source and "coalesced" in runner_source

    matlab_doc = _read(ROOT / "docs" / "lab-tracker-matlab.md")
    assert "Runtime smoke validation" in matlab_doc
    assert "scripts/matlab-smoke.sh" in matlab_doc
    # The runbook must warn that a green exit alone does not prove a capture.
    assert "result.action" in matlab_doc
