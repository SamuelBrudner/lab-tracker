"""Rule-based provenance proposals from the git tree a capture ran in.

A capture made from uncommitted code has no commit to name, but the client can
still record *which code* it was: the git tree id of the working copy at that
moment (``run_git_worktree_tree`` from ``run_context`` and ``lt run``,
``capture_git_worktree_tree`` from a plain figure/file capture,
``hpc_git_worktree_tree`` from ``lt hpc begin``/``finish``). An ``lt repo``
commit note records its commit's own tree as ``repo_git_tree``. Two equal tree
ids are the same bytes of code, so a capture whose tree equals a commit's tree
was made from exactly that commit's code -- whether the commit came before the
capture (a clean checkout) or after it (the person committed what they ran).

This module turns such equalities into matches the provenance-link service
proposes as ``was_derived_from`` links with ``basis=worktree_tree_match``:
from the capture note to the *earliest other* note carrying the same
``repo_git_tree``. Like the other deterministic detectors it only proposes;
a person accepts or rejects each link.

Everything here is pure: the caller supplies the project's carrier notes and
receives the matches.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from lab_tracker.models import Note

# Worktree tree keys in precedence order: the first that resolves wins, so a
# note proposes at most one commit. A plain capture's tree is taken at save
# time without the saved file, the closest record of the code that wrote it;
# a run context's is taken when the run began.
WORKTREE_TREE_METADATA_KEYS: tuple[str, ...] = (
    "capture_git_worktree_tree",
    "run_git_worktree_tree",
    "hpc_git_worktree_tree",
)
# Stamped by ``lt repo`` commit events: ``git rev-parse <commit>^{tree}``.
COMMIT_TREE_METADATA_KEY = "repo_git_tree"
TREE_MATCH_METADATA_KEYS: tuple[str, ...] = (
    *WORKTREE_TREE_METADATA_KEYS,
    COMMIT_TREE_METADATA_KEY,
)
# A full git tree id in the SHA-1 (40 hex) or SHA-256 (64 hex) object format.
# Abbreviations are never trusted: a tree match is an equality, not a prefix.
_TREE_ID = re.compile(r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


@dataclass(frozen=True)
class TreeMatch:
    """One capture made from the exact code of a committed tree."""

    note_id: UUID
    commit_note_id: UUID
    metadata_key: str
    tree: str


def normalize_tree_id(value: object) -> str:
    """The lowercase tree id when ``value`` is a full git tree id, else ``""``."""

    text = str(value or "").strip().lower()
    return text if _TREE_ID.match(text) else ""


def _capture_order(note: Note) -> tuple[datetime, str]:
    return (note.created_at, str(note.note_id))


def commit_notes_by_tree(notes: Iterable[Note]) -> dict[str, list[UUID]]:
    """Every note carrying ``repo_git_tree``, per tree, earliest capture first."""

    carriers: dict[str, list[Note]] = {}
    for note in notes:
        tree = normalize_tree_id(note.metadata.get(COMMIT_TREE_METADATA_KEY))
        if tree:
            carriers.setdefault(tree, []).append(note)
    return {
        tree: [note.note_id for note in sorted(group, key=_capture_order)]
        for tree, group in carriers.items()
    }


def tree_matches_for_note(
    note: Note, commits_by_tree: Mapping[str, Sequence[UUID]]
) -> list[TreeMatch]:
    """The earliest other commit note sharing the note's worktree tree, if any."""

    for key in WORKTREE_TREE_METADATA_KEYS:
        tree = normalize_tree_id(note.metadata.get(key))
        if not tree:
            continue
        for commit_note_id in commits_by_tree.get(tree, ()):
            if commit_note_id == note.note_id:
                continue
            return [
                TreeMatch(
                    note_id=note.note_id,
                    commit_note_id=commit_note_id,
                    metadata_key=key,
                    tree=tree,
                )
            ]
    return []


def tree_matches_for_notes(notes: Iterable[Note]) -> list[TreeMatch]:
    """Worktree-tree matches for a project's carrier notes, in capture order."""

    ordered = sorted(notes, key=_capture_order)
    commits_by_tree = commit_notes_by_tree(ordered)
    if not commits_by_tree:
        return []
    return [match for note in ordered for match in tree_matches_for_note(note, commits_by_tree)]


__all__ = [
    "COMMIT_TREE_METADATA_KEY",
    "TREE_MATCH_METADATA_KEYS",
    "WORKTREE_TREE_METADATA_KEYS",
    "TreeMatch",
    "commit_notes_by_tree",
    "normalize_tree_id",
    "tree_matches_for_note",
    "tree_matches_for_notes",
]
