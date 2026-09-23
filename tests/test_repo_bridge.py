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


def test_repo_note_capture_reads_dirty_tree() -> None:
    capture = repo_note_capture(_repo_note_metadata(repo_git_dirty=True))

    assert capture is not None
    assert capture.dirty is True
    assert capture.status_error == ""


def test_repo_note_capture_keeps_unknown_dirty_state_unknown() -> None:
    # The client records an unanswered `git status` as repo_git_status_error
    # with no repo_git_dirty key; that must not be read as a clean tree.
    metadata = _repo_note_metadata(repo_git_status_error="git status --porcelain timed out")
    del metadata["repo_git_dirty"]

    capture = repo_note_capture(metadata)

    assert capture is not None
    assert capture.dirty is None
    assert capture.status_error == "git status --porcelain timed out"


def test_repo_note_capture_without_dirty_flag_is_unknown() -> None:
    # CI-script notes never record a dirty flag; absence is not "clean".
    metadata = _repo_note_metadata()
    del metadata["repo_git_dirty"]

    capture = repo_note_capture(metadata)

    assert capture is not None
    assert capture.dirty is None
    assert capture.status_error == ""


@pytest.mark.parametrize(
    ("source", "expected_dirty", "expected_error"),
    [
        ({"git_dirty": True}, True, ""),
        ({"git_dirty": False}, False, ""),
        (
            {"git_dirty": None, "git_status_error": "timed out after 10s"},
            None,
            "timed out after 10s",
        ),
    ],
)
def test_repo_note_capture_round_trips_client_dirty_metadata(
    source, expected_dirty, expected_error
) -> None:
    # Producer/consumer contract: whatever the client adapter writes for the
    # dirty state (lab_tracker_client.gitinfo.dirty_metadata, "repo_" prefix)
    # reads back with the same meaning here.
    from lab_tracker_client.gitinfo import dirty_metadata

    metadata = _repo_note_metadata()
    del metadata["repo_git_dirty"]
    metadata.update(dirty_metadata(source, "repo_"))

    capture = repo_note_capture(metadata)

    assert capture is not None
    assert capture.dirty is expected_dirty
    assert capture.status_error == expected_error


@pytest.mark.parametrize("dirty", [True, False])
def test_repo_note_capture_reads_dirty_flag_stored_by_the_server(
    client, admin_auth_headers, dirty
) -> None:
    # The real producer of the metadata read here is the server: POST /notes
    # stores every note metadata value as a string (str(True) == "True"), so a
    # note that went through the API must still yield its known dirty state.
    from lab_tracker_client.gitinfo import dirty_metadata

    project = client.post("/projects", json={"name": "Repo"}, headers=admin_auth_headers)
    assert project.status_code == 201, project.text
    metadata = _repo_note_metadata()
    del metadata["repo_git_dirty"]
    metadata.update(dirty_metadata({"git_dirty": dirty}, "repo_"))
    response = client.post(
        "/notes",
        json={
            "project_id": project.json()["data"]["project_id"],
            "raw_content": "lt repo capture",
            "metadata": metadata,
        },
        headers=admin_auth_headers,
    )
    assert response.status_code == 201, response.text
    stored = response.json()["data"]["metadata"]
    assert stored["repo_git_dirty"] == str(dirty)

    capture = repo_note_capture(stored)

    assert capture is not None
    assert capture.dirty is dirty
    assert capture.status_error == ""


@pytest.mark.parametrize("raw", ["false", "true", "FALSE", " True", "yes", "", 0, 1])
def test_repo_note_capture_rejects_non_boolean_dirty_flag(raw) -> None:
    # Only a bool or the server's canonical str(bool) encoding ("True" /
    # "False") is read; anything else is not guessed at (bool("false") is
    # True) and is recorded as unknown with an explicit marker naming it.
    capture = repo_note_capture(_repo_note_metadata(repo_git_dirty=raw))

    assert capture is not None
    assert capture.dirty is None
    assert "repo_git_dirty" in capture.status_error


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


# Notes stored before the H9/H10 client and CI fixes can still carry a
# credentialed remote; the bridge must not copy it into Analysis metadata.
SECRET = "SEKRET_TOKEN_123"
REMOTE_FORMS = [
    "https://github.com/lab/repo.git",
    "git@github.com:lab/repo.git",
    "ssh://git@github.com:2222/lab/repo.git",
    "file:///srv/git/repo.git",
    "/srv/git/repo.git",
    "  https://github.com/lab/repo.git\n",
    f"https://oauth2:{SECRET}@gitlab.example.com/lab/repo.git",
    f"https://{SECRET}@github.com/lab/repo.git",
    f"HTTPS://{SECRET}@github.com/lab/repo.git",
    f"http://x-access-token:{SECRET}@example.com:8080/lab/repo",
    f"https://user:p@ss{SECRET}@example.com/lab/repo.git",
    f"https://github.com/lab/repo.git?access_token={SECRET}",
    f"https://github.com/lab/repo.git#{SECRET}",
    f"ssh://git:{SECRET}@example.com/lab/repo.git",
    f"git+ssh://deploy:{SECRET}@example.com:22/lab/repo",
    f"git://{SECRET}@example.com/lab/repo.git",
    "deploy@example.com:lab/repo@v2.git",
]


@pytest.mark.parametrize("key", ["repo_remote_url", "git_remote_origin_url"])
@pytest.mark.parametrize("remote", REMOTE_FORMS)
def test_repo_note_capture_strips_stored_remote_credentials(key: str, remote: str) -> None:
    from lab_tracker_client.gitinfo import sanitize_remote_url

    metadata = _repo_note_metadata()
    del metadata["repo_remote_url"]
    metadata[key] = remote

    capture = repo_note_capture(metadata)
    fields = analysis_fields_from_repo_note(metadata)

    assert capture is not None
    assert capture.remote == sanitize_remote_url(remote)
    assert fields is not None
    assert SECRET not in repr(fields)


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
        path="src/model.py",
        commit=COMMIT,
        content_hash="sha256:" + "e" * 64,
    )

    assert pin.store_name == "analysis-repo"
    assert pin.locator == f"src/model.py@{COMMIT}"
    assert pin.uri == f"store://analysis-repo/src/model.py@{COMMIT}"


@pytest.mark.parametrize(
    ("path", "commit"),
    (
        ("/src/model.py", COMMIT),
        ("src/model.py ", COMMIT),
        ("src/../secret.py", COMMIT),
        ("src/run:1.py", COMMIT),
        ("src/model.py", "HEAD"),
        ("src/model.py", "A" * 40),
        ("src/model.py", "0" * 40),
        ("src/model.py", f" {COMMIT}"),
    ),
)
def test_git_code_pin_rejects_invalid_inputs_without_repair(
    path: str,
    commit: str,
) -> None:
    with pytest.raises(ValueError, match="Invalid Git-store"):
        git_code_pin(
            store_name="analysis-repo",
            path=path,
            commit=commit,
            content_hash="sha256:" + "e" * 64,
        )


def test_git_code_pin_canonicalizes_unicode_and_internal_at_identity() -> None:
    object_id = "b" * 64

    pin = git_code_pin(
        store_name="user@analysis store",
        path="Müller/@generated model.py",
        commit=object_id,
        content_hash="sha256:" + "e" * 64,
    )

    assert pin.source_system == "store"
    assert pin.content_hash == "sha256:" + "e" * 64
    assert pin.store_name == "user@analysis store"
    assert pin.locator == f"Müller/@generated model.py@{object_id}"
    assert pin.uri == (
        "store://user%40analysis%20store/"
        f"M%C3%BCller/@generated%20model.py@{object_id}"
    )


def test_git_code_pin_resolves_through_git_store() -> None:
    # The pin's locator must be exactly what store_relative_reference expects.
    from store_authority_fakes import bound_authority_proof

    from lab_tracker.artifact_resolution import (
        GitStoreResolutionTarget,
        store_relative_reference,
    )
    from lab_tracker.models import StoreKind

    proof = bound_authority_proof(
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
        proof, path=pin.locator, content_hash=pin.content_hash
    )

    assert isinstance(concrete, GitStoreResolutionTarget)
    assert concrete.logical_reference == pin
    assert concrete.remote.subprocess_value == "https://example.com/org/repo.git"
    assert concrete.pin.path.path == "src/model.py"
    assert concrete.pin.object_id.value == COMMIT


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
