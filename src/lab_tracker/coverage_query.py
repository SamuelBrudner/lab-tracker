"""Derived capture-coverage reads: how much of a project's record a person reviewed.

Nothing here is stored. Every number is derived at read time from ``notes``,
``graph_change_sets``, and ``graph_change_operations``, so coverage can never
drift from the records it describes and no migration is involved.

Reviewed-ness is a bounded Python join over the change sets' JSON
``source_note_ids`` column (the precedent is
``successful_source_note_ids_at_window_end`` in the batch repository): it is
exact by design — a row cap would silently misreport coverage.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import GraphChangeOperationModel, GraphChangeSetModel, NoteModel
from lab_tracker.db_types import ensure_uuid
from lab_tracker.models import (
    GraphChangeOperationStatus,
    GraphChangeSetStatus,
    NoteArchiveReason,
    NoteStatus,
    ProjectCoverageCaptureSource,
    ProjectCoverageReport,
    ProjectCoverageSummary,
)
from lab_tracker.sqlalchemy_mapper_parts.common import as_utc

# A staged capture counts as reviewed once a person closed a draft over it,
# whichever way the review went.
REVIEWED_CHANGE_SET_STATUSES = frozenset(
    {GraphChangeSetStatus.COMMITTED.value, GraphChangeSetStatus.REJECTED.value}
)
# Drafts still waiting on a person. ``drafting`` and ``committing`` are
# lease-protected transients and ``failed`` is not a review, so their notes
# stay unreviewed without counting as pending.
PENDING_CHANGE_SET_STATUSES = frozenset(
    {
        GraphChangeSetStatus.READY.value,
        GraphChangeSetStatus.SUBMITTED.value,
        GraphChangeSetStatus.CHANGES_REQUESTED.value,
    }
)
CAPTURE_SOURCE_LISTING_LIMIT = 50

# Note-metadata keys written by capture clients; the server names them only here.
EVIDENCE_SOURCE_PROVIDER_KEY = "evidence_source_provider"
EVIDENCE_ADAPTER_KEY = "evidence_adapter"
CAPTURE_INSTALL_ID_KEY = "capture_install_id"
CAPTURE_HOST_LABEL_KEY = "capture_host_label"
CAPTURE_SOURCE_METADATA_KEYS = (
    EVIDENCE_SOURCE_PROVIDER_KEY,
    EVIDENCE_ADAPTER_KEY,
    CAPTURE_INSTALL_ID_KEY,
    CAPTURE_HOST_LABEL_KEY,
)
# Historical spellings of the note id(s) inside a persisted operation source ref
# (the same tuple graph_draft_validation normalises on write).
_SOURCE_REF_NOTE_ID_KEYS = ("source_note_ids", "source_note_id", "note_id")


@dataclass
class _ReviewedNotes:
    """Note ids a project's closed drafts named, and the subset a committed draft absorbed."""

    reviewed: set[UUID] = field(default_factory=set)
    committed: set[UUID] = field(default_factory=set)


def project_coverage_summary(session: OrmSession, project_id: UUID) -> ProjectCoverageSummary:
    """Derive the compact coverage block for one project."""

    session.flush()
    scoped_project_id = str(project_id)
    reviewed = _reviewed_notes_by_project(session, [scoped_project_id]).get(
        scoped_project_id, _ReviewedNotes()
    )
    staged = _staged_notes(session, [scoped_project_id]).get(scoped_project_id, [])
    unreviewed_at = [
        created_at for note_id, created_at in staged if note_id not in reviewed.reviewed
    ]
    absorbed = {note_id for note_id, _ in staged if note_id in reviewed.committed}
    unplaced = absorbed - _cited_note_ids(session, scoped_project_id)
    pending_change_sets, open_clarification_requests = _pending_change_sets(
        session, scoped_project_id
    )
    return ProjectCoverageSummary(
        project_id=project_id,
        unreviewed_count=len(unreviewed_at),
        oldest_unreviewed_at=min(unreviewed_at) if unreviewed_at else None,
        unplaced_count=len(unplaced),
        archived_unreviewed_count=_archived_unreviewed_count(session, scoped_project_id),
        pending_change_sets=pending_change_sets,
        open_clarification_requests=open_clarification_requests,
        last_capture_at=_last_capture_at(session, scoped_project_id),
    )


def project_coverage_report(session: OrmSession, project_id: UUID) -> ProjectCoverageReport:
    """The coverage summary plus the bounded per-source last-seen listing."""

    summary = project_coverage_summary(session, project_id)
    capture_sources, truncated = _capture_sources(session, str(project_id))
    return ProjectCoverageReport(
        **summary.model_dump(),
        capture_sources=capture_sources,
        capture_sources_truncated=truncated,
    )


def unreviewed_capture_counts_by_project(
    session: OrmSession,
    project_ids: list[str],
) -> defaultdict[str, int]:
    """Count staged notes no committed or rejected draft has named, per project."""

    counts: defaultdict[str, int] = defaultdict(int)
    if not project_ids:
        return counts
    reviewed = _reviewed_notes_by_project(session, project_ids)
    for project_id, notes in _staged_notes(session, project_ids).items():
        reviewed_ids = reviewed.get(project_id, _ReviewedNotes()).reviewed
        counts[project_id] = sum(1 for note_id, _ in notes if note_id not in reviewed_ids)
    return counts


def _reviewed_notes_by_project(
    session: OrmSession,
    project_ids: list[str],
) -> dict[str, _ReviewedNotes]:
    reviewed: defaultdict[str, _ReviewedNotes] = defaultdict(_ReviewedNotes)
    if not project_ids:
        return {}
    rows = session.execute(
        select(
            GraphChangeSetModel.project_id,
            GraphChangeSetModel.status,
            GraphChangeSetModel.source_note_id,
            GraphChangeSetModel.source_note_ids,
        ).where(
            GraphChangeSetModel.project_id.in_(project_ids),
            GraphChangeSetModel.status.in_(sorted(REVIEWED_CHANGE_SET_STATUSES)),
        )
    )
    for project_id, status, source_note_id, source_note_ids in rows:
        note_ids = {ensure_uuid(source_note_id)}
        note_ids.update(ensure_uuid(str(note_id)) for note_id in (source_note_ids or []))
        entry = reviewed[str(project_id)]
        entry.reviewed.update(note_ids)
        if status == GraphChangeSetStatus.COMMITTED.value:
            entry.committed.update(note_ids)
    return dict(reviewed)


def _staged_notes(
    session: OrmSession,
    project_ids: list[str],
) -> dict[str, list[tuple[UUID, datetime]]]:
    staged: defaultdict[str, list[tuple[UUID, datetime]]] = defaultdict(list)
    if not project_ids:
        return {}
    rows = session.execute(
        select(NoteModel.project_id, NoteModel.note_id, NoteModel.created_at).where(
            NoteModel.project_id.in_(project_ids),
            NoteModel.status == NoteStatus.STAGED.value,
        )
    )
    for project_id, note_id, created_at in rows:
        staged[str(project_id)].append((ensure_uuid(note_id), as_utc(created_at)))
    return dict(staged)


def _cited_note_ids(session: OrmSession, project_id: str) -> set[UUID]:
    """Note ids an applied operation of a committed draft cites in its source refs."""

    rows = session.execute(
        select(GraphChangeOperationModel.source_refs)
        .join(
            GraphChangeSetModel,
            GraphChangeSetModel.change_set_id == GraphChangeOperationModel.change_set_id,
        )
        .where(
            GraphChangeSetModel.project_id == project_id,
            GraphChangeSetModel.status == GraphChangeSetStatus.COMMITTED.value,
            GraphChangeOperationModel.status == GraphChangeOperationStatus.APPLIED.value,
        )
    )
    cited: set[UUID] = set()
    for (source_refs,) in rows:
        for source_ref in source_refs or []:
            if isinstance(source_ref, dict):
                cited.update(_source_ref_note_ids(source_ref))
    return cited


def _source_ref_note_ids(source_ref: dict[str, Any]) -> set[UUID]:
    note_ids: set[UUID] = set()
    for key in _SOURCE_REF_NOTE_ID_KEYS:
        value = source_ref.get(key)
        if value is None:
            continue
        values = value if isinstance(value, list) else [value]
        note_ids.update(ensure_uuid(str(item)) for item in values)
    return note_ids


def _archived_unreviewed_count(session: OrmSession, project_id: str) -> int:
    count = session.execute(
        select(func.count())
        .select_from(NoteModel)
        .where(
            NoteModel.project_id == project_id,
            NoteModel.status == NoteStatus.ARCHIVED.value,
            NoteModel.archived_reason == NoteArchiveReason.ARCHIVED_UNREVIEWED.value,
        )
    ).scalar_one()
    return int(count)


def _pending_change_sets(session: OrmSession, project_id: str) -> tuple[int, int]:
    """(drafts waiting on a person, clarification requests those drafts carry)."""

    rows = session.execute(
        select(GraphChangeSetModel.clarification_requests).where(
            GraphChangeSetModel.project_id == project_id,
            GraphChangeSetModel.status.in_(sorted(PENDING_CHANGE_SET_STATUSES)),
        )
    )
    pending = 0
    open_requests = 0
    for (clarification_requests,) in rows:
        pending += 1
        open_requests += len(clarification_requests or [])
    return pending, open_requests


def _last_capture_at(session: OrmSession, project_id: str) -> datetime | None:
    value = session.execute(
        select(func.max(NoteModel.created_at)).where(NoteModel.project_id == project_id)
    ).scalar_one()
    return as_utc(value) if value is not None else None


def _capture_sources(
    session: OrmSession,
    project_id: str,
) -> tuple[list[ProjectCoverageCaptureSource], bool]:
    """Group the project's notes by capture source, most recently delivering first."""

    source_columns = [
        NoteModel.note_metadata[key].as_string() for key in CAPTURE_SOURCE_METADATA_KEYS
    ]
    last_capture_at = func.max(NoteModel.created_at)
    rows = session.execute(
        select(*source_columns, func.count(NoteModel.note_id), last_capture_at)
        .where(NoteModel.project_id == project_id)
        .group_by(*source_columns)
        .order_by(last_capture_at.desc(), *source_columns)
        .limit(CAPTURE_SOURCE_LISTING_LIMIT + 1)
    ).all()
    truncated = len(rows) > CAPTURE_SOURCE_LISTING_LIMIT
    sources = [
        ProjectCoverageCaptureSource(
            evidence_source_provider=provider,
            evidence_adapter=adapter,
            capture_install_id=install_id,
            capture_host_label=host_label,
            note_count=int(note_count),
            last_capture_at=as_utc(captured_at),
        )
        for provider, adapter, install_id, host_label, note_count, captured_at in rows[
            :CAPTURE_SOURCE_LISTING_LIMIT
        ]
    ]
    return sources, truncated
