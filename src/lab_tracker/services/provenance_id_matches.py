"""Rule-based provenance-link proposals from identifiers a capture already carries.

A figure saved inside ``run_context`` records the git commit it was made
from; a repo or HPC event records its commit; a watched file records the
session its folder named. When one of those identifiers matches exactly one
entity in the project, the link is a fact, not an inference. This module
turns such facts into proposed :class:`~lab_tracker.models.ProvenanceLink`
rows with ``basis=exact_id_match`` (the same human-gated rows and review
surface the content-hash detector feeds), so the daily review still decides
and the model is never asked to guess what an id already says.

Everything here is pure: the caller supplies the notes, sessions, and
committed analyses of one project and receives the matches.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID

from lab_tracker.models import Analysis, EntityRef, EntityType, Note, Session

# Note-metadata keys the capture clients stamp with a git commit
# (figure run context, repo and HPC events, the deprecated git snapshot).
ID_MATCH_COMMIT_METADATA_KEYS: tuple[str, ...] = (
    "run_git_commit",
    "repo_git_commit",
    "hpc_git_commit",
    "git_commit",
)
# Note-metadata keys the capture clients stamp with a session id
# (a watched folder's session, a figure saved under an active session).
ID_MATCH_SESSION_METADATA_KEYS: tuple[str, ...] = ("watch_session_id", "capture_session_id")
# Shorter git prefixes are too ambiguous to count as an exact match.
MIN_COMMIT_PREFIX_LENGTH = 7


@dataclass(frozen=True)
class IdMatch:
    """One capture whose metadata names exactly one project entity."""

    note_id: UUID
    target: EntityRef
    metadata_key: str
    metadata_value: str


def commit_matches(commit: str, code_version: str) -> bool:
    """True when one value is a prefix of the other and both are long enough."""

    left = commit.strip().lower()
    right = code_version.strip().lower()
    if len(left) < MIN_COMMIT_PREFIX_LENGTH or len(right) < MIN_COMMIT_PREFIX_LENGTH:
        return False
    return left.startswith(right) or right.startswith(left)


def _metadata_value(note: Note, key: str) -> str:
    return str(note.metadata.get(key) or "").strip()


def _already_linked(note: Note, target: EntityRef) -> bool:
    return any(
        existing.entity_type == target.entity_type and existing.entity_id == target.entity_id
        for existing in note.targets
    )


def session_id_matches(note: Note, sessions: Sequence[Session]) -> list[IdMatch]:
    """Sessions the note's metadata names by id, one match per session."""

    known = {session.session_id for session in sessions}
    matches: list[IdMatch] = []
    seen: set[UUID] = set()
    for key in ID_MATCH_SESSION_METADATA_KEYS:
        value = _metadata_value(note, key)
        if not value:
            continue
        try:
            session_id = UUID(value)
        except ValueError:
            continue
        if session_id not in known or session_id in seen:
            continue
        seen.add(session_id)
        matches.append(
            IdMatch(
                note_id=note.note_id,
                target=EntityRef(entity_type=EntityType.SESSION, entity_id=session_id),
                metadata_key=key,
                metadata_value=value,
            )
        )
    return matches


def analysis_id_matches(note: Note, analyses: Sequence[Analysis]) -> list[IdMatch]:
    """The one committed analysis whose ``code_version`` the note's commit names.

    No candidate is silence; several candidates are an ambiguity the model
    (and the person) can weigh, never a rule-based claim. The first metadata
    key that resolves wins, so a note never proposes two analyses.
    """

    for key in ID_MATCH_COMMIT_METADATA_KEYS:
        value = _metadata_value(note, key)
        if not value:
            continue
        candidates = [item for item in analyses if commit_matches(value, item.code_version)]
        if len(candidates) != 1:
            continue
        return [
            IdMatch(
                note_id=note.note_id,
                target=EntityRef(
                    entity_type=EntityType.ANALYSIS, entity_id=candidates[0].analysis_id
                ),
                metadata_key=key,
                metadata_value=value,
            )
        ]
    return []


def id_matches_for_note(
    note: Note,
    *,
    sessions: Sequence[Session],
    analyses: Sequence[Analysis],
) -> list[IdMatch]:
    """Exact-id matches for one note, skipping targets the note already carries."""

    return [
        match
        for match in (*session_id_matches(note, sessions), *analysis_id_matches(note, analyses))
        if not _already_linked(note, match.target)
    ]


def id_matches_for_notes(
    notes: Iterable[Note],
    *,
    sessions: Sequence[Session],
    analyses: Sequence[Analysis],
) -> list[IdMatch]:
    return [
        match
        for note in notes
        for match in id_matches_for_note(note, sessions=sessions, analyses=analyses)
    ]


__all__ = [
    "ID_MATCH_COMMIT_METADATA_KEYS",
    "ID_MATCH_SESSION_METADATA_KEYS",
    "MIN_COMMIT_PREFIX_LENGTH",
    "IdMatch",
    "analysis_id_matches",
    "commit_matches",
    "id_matches_for_note",
    "id_matches_for_notes",
    "session_id_matches",
]
