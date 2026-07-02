from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from lab_tracker.repo_bridge import (
    analysis_fields_from_repo_note,
    git_code_pin,
    repo_note_capture,
)

COMMIT = "a" * 40


def _repo_note_metadata(**overrides) -> dict:
    metadata = {
        "evidence_source_provider": "git",
        "evidence_source_external_id": f"example.com/org/repo@{COMMIT}",
        "repo_git_commit": COMMIT,
        "repo_remote_url": "https://example.com/org/repo.git",
        "repo_git_branch": "main",
        "repo_git_dirty": False,
        "repo_artifacts": json.dumps(
            [
                {
                    "uri": "file:///scratch/run-1/results.csv",
                    "kind": "file",
                    "title": "results.csv",
                    "summary": "Run output.",
                    "content_hash": "sha256:" + "b" * 64,
                    "size_bytes": 42,
                }
            ]
        ),
    }
    metadata.update(overrides)
    return metadata


# --- capture extraction -----------------------------------------------------


def test_repo_note_capture_extracts_state() -> None:
    capture = repo_note_capture(_repo_note_metadata())

    assert capture is not None
    assert capture.commit == COMMIT
    assert capture.remote == "https://example.com/org/repo.git"
    assert capture.branch == "main"
    assert capture.dirty is False
    assert capture.artifacts[0]["title"] == "results.csv"


def test_repo_note_capture_rejects_non_git_notes() -> None:
    assert repo_note_capture({"evidence_source_provider": "hpc-outbox"}) is None
    assert repo_note_capture({"evidence_source_provider": "git"}) is None  # no commit
    assert repo_note_capture(None) is None


def test_repo_note_capture_reads_ci_script_keys() -> None:
    # The CI draft script emits git_commit/git_remote_origin_url/git_branch.
    capture = repo_note_capture(
        {
            "evidence_source_provider": "git",
            "git_commit": COMMIT,
            "git_remote_origin_url": "git@example.com:org/repo.git",
            "git_branch": "main",
        }
    )

    assert capture is not None
    assert capture.commit == COMMIT
    assert capture.remote == "git@example.com:org/repo.git"


def test_repo_note_capture_tolerates_malformed_artifacts() -> None:
    capture = repo_note_capture(_repo_note_metadata(repo_artifacts="not json"))

    assert capture is not None
    assert capture.artifacts == []


# --- analysis field lifting ---------------------------------------------------


def test_analysis_fields_lift_commit_and_artifacts() -> None:
    fields = analysis_fields_from_repo_note(_repo_note_metadata())

    assert fields is not None
    assert fields["code_version"] == COMMIT
    [reference] = fields["external_artifacts"]
    assert reference.source_system == "local"
    assert reference.uri == "file:///scratch/run-1/results.csv"
    assert reference.content_hash == "sha256:" + "b" * 64
    assert reference.metadata["repo_git_commit"] == COMMIT
    assert reference.metadata["title"] == "results.csv"


def test_analysis_fields_include_environment_hash_when_captured() -> None:
    fields = analysis_fields_from_repo_note(
        _repo_note_metadata(repo_environment_hash="sha256:" + "c" * 64)
    )

    assert fields is not None
    assert fields["environment_hash"] == "sha256:" + "c" * 64


def test_analysis_fields_none_for_non_repo_note() -> None:
    assert analysis_fields_from_repo_note({"evidence_source_provider": "ci"}) is None


def test_analysis_fields_skip_artifacts_without_hash_or_uri() -> None:
    fields = analysis_fields_from_repo_note(
        _repo_note_metadata(
            repo_artifacts=json.dumps(
                [
                    {"uri": "file:///x", "content_hash": ""},
                    {"uri": "", "content_hash": "sha256:" + "d" * 64},
                ]
            )
        )
    )

    assert fields is not None
    assert fields["external_artifacts"] == []


# --- git code pin --------------------------------------------------------------


def test_git_code_pin_builds_store_locator() -> None:
    pin = git_code_pin(
        store_name="analysis-repo",
        path="/src/model.py",
        commit=COMMIT,
        content_hash="sha256:" + "e" * 64,
    )

    assert pin.store_name == "analysis-repo"
    assert pin.locator == f"src/model.py@{COMMIT}"
    assert pin.uri == f"store://analysis-repo/src/model.py@{COMMIT}"


def test_git_code_pin_resolves_through_git_store() -> None:
    # The pin's locator must be exactly what store_relative_reference expects.
    from uuid import uuid4

    from lab_tracker.artifact_resolution import store_relative_reference
    from lab_tracker.models import DataStore, StoreKind

    store = DataStore(
        store_id=uuid4(),
        project_id=uuid4(),
        name="analysis-repo",
        kind=StoreKind.GIT,
        root="https://example.com/org/repo.git",
    )
    pin = git_code_pin(
        store_name="analysis-repo",
        path="src/model.py",
        commit=COMMIT,
        content_hash="sha256:" + "e" * 64,
    )

    concrete = store_relative_reference(
        store, path=pin.locator, content_hash=pin.content_hash
    )

    assert concrete is not None
    assert concrete.source_system == "git"
    assert concrete.uri == f"git+https://example.com/org/repo.git#{COMMIT}:src/model.py"


# --- external-id contract: client adapter vs CI script -------------------------


def _load_ci_script_module():
    script = Path(__file__).resolve().parents[1] / "scripts" / "create-analysis-graph-draft.py"
    spec = importlib.util.spec_from_file_location("ci_draft_script", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "remote",
    [
        "https://example.com/org/repo.git",
        "git@example.com:org/repo.git",
        "ssh://git@example.com/org/repo",
        "https://user@example.com/Org/Repo.git",
        "",
    ],
)
def test_remote_normalization_contract_between_client_and_ci_script(remote) -> None:
    """The hook path and the CI path must share one git-evidence identity."""

    from lab_tracker_client.repo import normalize_remote

    ci_script = _load_ci_script_module()

    assert ci_script._normalize_remote(remote) == normalize_remote(remote)


def test_external_id_format_contract() -> None:
    from lab_tracker_client.repo import normalize_remote

    remote = "https://example.com/org/repo.git"
    expected = f"{normalize_remote(remote)}@{COMMIT}"

    # Client adapter composes <normalized-remote>@<commit> (see
    # event_source_external_id); the CI script must compose the same string.
    assert expected == f"example.com/org/repo@{COMMIT}"
