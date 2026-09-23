"""Lift repo-capture evidence into verifiable analysis provenance.

The ``lt repo`` adapter stages notes whose metadata carries the captured repo
state (``repo_git_commit``, ``repo_remote_url``, ``repo_artifacts``, …). This
module is the curation-time bridge: when a reviewer accepts such a note into
the semantic graph, these helpers translate that metadata into the fields an
:class:`~lab_tracker.models.Analysis` records — the commit SHA into the opaque
``code_version`` (the project's decided encoding for commits: pins/version
strings, not a commit-entity DAG), and per-file artifact pointers into
:class:`~lab_tracker.models.ExternalArtifactReference` metadata. Direct
``file://`` pointers retain captured identity and hashes but require conversion
to a registered store-relative identity before the resolver stack may read
them.

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
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from lab_tracker.models import ExternalArtifactReference

REPO_EVIDENCE_PROVIDER = "git"

# ``scheme://authority rest`` — authority is everything up to the first
# '/', '?' or '#', so a raw '@' inside a password stays in the authority.
_SCHEME_URL = re.compile(
    r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?P<authority>[^/?#]*)(?P<rest>.*)\Z",
    re.DOTALL,
)
# Transports whose login name is addressing (``ssh://git@host``), not a secret.
_SSH_SCHEMES = frozenset({"ssh", "git+ssh", "ssh+git"})


@dataclass(frozen=True)
class RepoNoteCapture:
    """The repo state a staged ``lt repo`` note captured.

    ``dirty`` is ``True``/``False`` only when the capture recorded a boolean
    ``repo_git_dirty`` (a bool, or ``"True"``/``"False"`` as the server stores
    it). It is ``None`` (unknown) when the flag is absent — the
    client records an unanswered ``git status`` as ``repo_git_status_error``
    instead, and CI-script notes carry no flag — or is not a boolean;
    ``status_error`` then carries the recorded reason, if any. Never read an
    unknown state as a clean tree.
    """

    commit: str
    remote: str = ""
    branch: str = ""
    dirty: bool | None = None
    status_error: str = ""
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
    dirty, status_error = _dirty_state(meta)
    return RepoNoteCapture(
        commit=commit,
        remote=_sanitize_remote_url(
            str(meta.get("repo_remote_url") or meta.get("git_remote_origin_url") or "")
        ),
        branch=str(meta.get("repo_git_branch") or meta.get("git_branch") or "").strip(),
        dirty=dirty,
        status_error=status_error,
        environment_hash=str(meta.get("repo_environment_hash") or "").strip(),
        artifacts=_parse_artifacts(meta.get("repo_artifacts")),
    )


def analysis_fields_from_repo_note(
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Build AnalysisCreate-shaped fields from a repo note's metadata.

    Returns ``{"code_version": ..., "external_artifacts": [...]}`` (plus
    ``environment_hash`` when captured), or ``None`` for non-repo notes. The
    artifact pointers become ``source_system="local"`` metadata references.
    Their ``file://`` URI plus captured sha256 records identity, but project
    roles cannot resolve the host path directly. Convert the pointer to a
    registered store-relative identity before requesting bytes.
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
    """Pin a portable repo-relative code file to one immutable Git object.

    Registered Git pins use full lowercase SHA-1 or SHA-256 object IDs. Invalid
    paths and mutable or ambiguous revisions are rejected without normalization.
    """

    return ExternalArtifactReference.for_git_store(
        store_name=store_name,
        repository_path=path,
        object_id=commit,
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


# The server stores every note metadata value as a string
# (``schemas._normalize_note_metadata_for_request`` applies ``str(value)``), so
# a boolean flag sent through the API comes back as ``str(bool)``. Exactly
# those two spellings are read; anything else is not guessed at.
_STORED_DIRTY_FLAGS = {"True": True, "False": False}


def _sanitize_remote_url(remote: str) -> str:
    """Return ``remote`` with every credential-bearing part removed.

    Defence in depth for notes stored before the client and CI producers
    sanitised remotes: a credentialed remote read from note metadata must not
    be copied into Analysis artifact metadata. Mirrors
    ``lab_tracker_client.gitinfo.sanitize_remote_url`` (the server package
    does not import the client); ``tests/test_repo_bridge.py`` pins the two
    together. All userinfo is dropped from non-ssh scheme URLs, ssh URLs keep
    only the login name, query strings and fragments are dropped, and scp-like
    addressing and local paths are returned unchanged.
    """

    cleaned = remote.strip()
    if not cleaned:
        return ""
    match = _SCHEME_URL.match(cleaned)
    if match is None:
        return cleaned
    scheme = match["scheme"]
    authority = match["authority"]
    userinfo, at, host = authority.rpartition("@")
    if at:
        login = userinfo.partition(":")[0]
        keep_login = scheme.lower() in _SSH_SCHEMES and bool(login)
        authority = f"{login}@{host}" if keep_login else host
    path = re.split(r"[?#]", match["rest"], maxsplit=1)[0]
    return f"{scheme}://{authority}{path}"


def _dirty_state(meta: Mapping[str, Any]) -> tuple[bool | None, str]:
    """Read the captured dirty flag without guessing.

    A bool, or the server's canonical ``str(bool)`` encoding, is known; a
    missing flag is unknown; any other value is unknown with a marker.
    """

    status_error = str(meta.get("repo_git_status_error") or "").strip()
    raw = meta.get("repo_git_dirty")
    if isinstance(raw, bool):
        return raw, status_error
    if isinstance(raw, str) and raw in _STORED_DIRTY_FLAGS:
        return _STORED_DIRTY_FLAGS[raw], status_error
    if raw is None:
        return None, status_error
    marker = f"unrecognized repo_git_dirty value {raw!r}"
    return None, f"{marker}; {status_error}" if status_error else marker


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
