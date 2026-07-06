"""Lift repo-capture evidence into verifiable analysis provenance.

The ``lt repo`` adapter stages notes whose metadata carries the captured repo
state (``repo_git_commit``, ``repo_remote_url``, ``repo_artifacts``, …). This
module is the curation-time bridge: when a reviewer accepts such a note into
the semantic graph, these helpers translate that metadata into the fields an
:class:`~lab_tracker.models.Analysis` records — the commit SHA into the opaque
``code_version`` (the project's decided encoding for commits: pins/version
strings, not a commit-entity DAG), and per-file artifact pointers into
:class:`~lab_tracker.models.ExternalArtifactReference` values the resolver
stack can verify (and, for moved files, recover by content hash).

The helpers are pure and side-effect free; draft-authoring surfaces (the
assistant, graph-draft tooling, or a human filling an AnalysisCreate payload)
call them and stay behind the human review gate. Nothing is committed here.

Evidence identity contract: repo notes use ``evidence_source_provider="git"``
and ``evidence_source_external_id=<normalized-remote>@<commit>`` — shared
between :func:`lab_tracker_client.repo.event_source_external_id` and
``scripts/create-analysis-graph-draft.py`` so both producers dedup to one
identity per commit (see ``tests/test_repo_bridge.py`` for the contract test).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from lab_tracker.models import ExternalArtifactReference

REPO_EVIDENCE_PROVIDER = "git"


@dataclass(frozen=True)
class RepoNoteCapture:
    """The repo state a staged ``lt repo`` note captured."""

    commit: str
    remote: str = ""
    branch: str = ""
    dirty: bool = False
    environment_hash: str = ""
    artifacts: list[dict[str, Any]] = field(default_factory=list)


def repo_note_capture(
    metadata: Mapping[str, Any] | None,
) -> RepoNoteCapture | None:
    """Extract the captured repo state from a note's metadata, if present.

    Returns ``None`` unless the note is git-provider evidence carrying a commit
    (both the ``lt repo`` adapter and the CI draft script qualify).
    """

    meta = dict(metadata or {})
    provider = str(meta.get("evidence_source_provider") or "").strip().lower()
    commit = str(meta.get("repo_git_commit") or meta.get("git_commit") or "").strip()
    if provider != REPO_EVIDENCE_PROVIDER or not commit:
        return None
    return RepoNoteCapture(
        commit=commit,
        remote=str(
            meta.get("repo_remote_url") or meta.get("git_remote_origin_url") or ""
        ).strip(),
        branch=str(meta.get("repo_git_branch") or meta.get("git_branch") or "").strip(),
        dirty=bool(meta.get("repo_git_dirty")),
        environment_hash=str(meta.get("repo_environment_hash") or "").strip(),
        artifacts=_parse_artifacts(meta.get("repo_artifacts")),
    )


def analysis_fields_from_repo_note(
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Build AnalysisCreate-shaped fields from a repo note's metadata.

    Returns ``{"code_version": ..., "external_artifacts": [...]}`` (plus
    ``environment_hash`` when captured), or ``None`` for non-repo notes. The
    artifact pointers become ``source_system="local"`` references — their
    ``file://`` URI plus captured sha256 makes them verifiable today and
    recoverable by content hash if the file later moves.
    """

    capture = repo_note_capture(metadata)
    if capture is None:
        return None
    references = [
        reference
        for artifact in capture.artifacts
        if (reference := _artifact_reference(capture, artifact)) is not None
    ]
    fields: dict[str, Any] = {
        "code_version": capture.commit,
        "external_artifacts": references,
    }
    if capture.environment_hash:
        fields["environment_hash"] = capture.environment_hash
    return fields


def git_code_pin(
    *,
    store_name: str,
    path: str,
    commit: str,
    content_hash: str,
) -> ExternalArtifactReference:
    """Pin a repo-relative code file at a commit against a registered git store.

    The ``<path>@<commit>`` locator is the git-store convention that
    ``store_relative_reference`` translates into a ``git+`` URI the GitResolver
    can fetch and verify (lt-81s6.6).
    """

    return ExternalArtifactReference.for_store(
        store_name=store_name,
        locator=f"{path.strip().lstrip('/')}@{commit.strip()}",
        content_hash=content_hash,
    )


def _artifact_reference(
    capture: RepoNoteCapture, artifact: Mapping[str, Any]
) -> ExternalArtifactReference | None:
    uri = str(artifact.get("uri") or "").strip()
    content_hash = str(artifact.get("content_hash") or "").strip()
    if not uri or not content_hash:
        return None
    metadata: dict[str, Any] = {
        key: artifact[key]
        for key in ("title", "kind", "summary", "size_bytes")
        if artifact.get(key)
    }
    metadata["repo_git_commit"] = capture.commit
    if capture.remote:
        metadata["repo_remote_url"] = capture.remote
    return ExternalArtifactReference(
        source_system="local",
        uri=uri,
        content_hash=content_hash,
        metadata=metadata,
    )


def _parse_artifacts(raw: Any) -> list[dict[str, Any]]:
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return []
    else:
        parsed = raw
    if not isinstance(parsed, list):
        return []
    return [dict(item) for item in parsed if isinstance(item, Mapping)]
