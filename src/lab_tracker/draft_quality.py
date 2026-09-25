"""Draft-quality ledger: pure aggregation over stored graph-draft review outcomes.

The ledger answers "how did AI proposals fare in human review?" for one
project. It is computed from ``graph_change_sets`` LEFT JOIN
``graph_change_operations`` rows that the repository projects into
:class:`DraftQualityRow`; nothing here reads persistence or a clock, so the
arithmetic is testable with hand-built rows.

Counts are bucketed by provider x model x prompt version x semantic type so a
prompt revision can be compared against its predecessor, and per-group
change-set statistics (clarification counts, median seconds to first accept
and to review) sit beside them. The ledger is a read of the review record,
never a gate: it auto-accepts nothing and changes no status.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from statistics import median
from typing import Final
from uuid import UUID

from lab_tracker.models import (
    EDITED_AT_KEY,
    EDITED_BY_KEY,
    AcceptanceMode,
    DraftQualityCell,
    DraftQualityGroupStats,
    DraftQualityLedger,
    GraphChangeOperationStatus,
    GraphChangeSetStatus,
    GraphDraftSemanticType,
)

# The review-audit keys that must survive a commit so "edited before accept"
# stays countable after the operation is APPLIED.
REVIEW_AUDIT_METADATA_KEYS: Final[frozenset[str]] = frozenset({EDITED_AT_KEY, EDITED_BY_KEY})

# ACCEPTED is the reviewer's decision; APPLIED is that same decision after a
# commit. Both are "accepted" for the ledger.
ACCEPTED_OPERATION_STATUSES: Final[frozenset[GraphChangeOperationStatus]] = frozenset(
    {GraphChangeOperationStatus.ACCEPTED, GraphChangeOperationStatus.APPLIED}
)

GroupKey = tuple[str, str, str]
CellKey = tuple[str, str, str, GraphDraftSemanticType | None]


@dataclass(frozen=True, slots=True)
class DraftQualityRow:
    """One (change set, operation) pair as projected by the repository.

    ``operation_status`` (and the other operation fields) are ``None`` when the
    LEFT JOIN produced a change set with no operations; such rows still count
    towards the change-set statistics but never towards a cell.
    """

    change_set_id: UUID
    provider: str
    model: str
    prompt_version: str
    change_set_status: GraphChangeSetStatus
    change_set_created_at: datetime
    reviewed_at: datetime | None
    clarification_request_count: int
    semantic_type: GraphDraftSemanticType | None
    operation_status: GraphChangeOperationStatus | None
    acceptance_mode: AcceptanceMode | None
    accepted_at: datetime | None
    edited_before_accept: bool

    @property
    def group_key(self) -> GroupKey:
        return (self.provider, self.model, self.prompt_version)

    @property
    def cell_key(self) -> CellKey:
        return (self.provider, self.model, self.prompt_version, self.semantic_type)

    @property
    def is_accepted(self) -> bool:
        return self.operation_status in ACCEPTED_OPERATION_STATUSES


def seconds_between(start: datetime, end: datetime) -> float:
    """Elapsed seconds from ``start`` to ``end``; a negative span is a data error."""

    if end < start:
        raise ValueError(f"end {end.isoformat()} precedes start {start.isoformat()}.")
    return (end - start).total_seconds()


@dataclass(slots=True)
class _CellCounts:
    proposed: int = 0
    accepted_total: int = 0
    accepted_human_selected: int = 0
    accepted_bulk_accepted: int = 0
    edited_before_accept: int = 0
    rejected: int = 0
    left_proposed_at_commit: int = 0

    def add(self, row: DraftQualityRow) -> None:
        self.proposed += 1
        if row.is_accepted:
            self.accepted_total += 1
            if row.acceptance_mode == AcceptanceMode.HUMAN_SELECTED:
                self.accepted_human_selected += 1
            elif row.acceptance_mode == AcceptanceMode.BULK_ACCEPTED:
                self.accepted_bulk_accepted += 1
            if row.edited_before_accept:
                self.edited_before_accept += 1
        elif row.operation_status == GraphChangeOperationStatus.REJECTED:
            self.rejected += 1
        elif (
            row.operation_status == GraphChangeOperationStatus.PROPOSED
            and row.change_set_status == GraphChangeSetStatus.COMMITTED
        ):
            self.left_proposed_at_commit += 1


@dataclass(slots=True)
class _ChangeSetFacts:
    """Set-level values seen once per change set, plus its earliest accept."""

    created_at: datetime
    reviewed_at: datetime | None
    clarification_request_count: int
    first_accepted_at: datetime | None = None

    def note_accept(self, accepted_at: datetime | None) -> None:
        if accepted_at is None:
            return
        if self.first_accepted_at is None or accepted_at < self.first_accepted_at:
            self.first_accepted_at = accepted_at


@dataclass(slots=True)
class _GroupFacts:
    change_sets: dict[UUID, _ChangeSetFacts] = field(default_factory=dict)

    def observe(self, row: DraftQualityRow) -> None:
        facts = self.change_sets.get(row.change_set_id)
        if facts is None:
            facts = _ChangeSetFacts(
                created_at=row.change_set_created_at,
                reviewed_at=row.reviewed_at,
                clarification_request_count=row.clarification_request_count,
            )
            self.change_sets[row.change_set_id] = facts
        if row.is_accepted:
            facts.note_accept(row.accepted_at)

    def stats(self, key: GroupKey) -> DraftQualityGroupStats:
        provider, model, prompt_version = key
        sets = list(self.change_sets.values())
        accept_spans = [
            seconds_between(item.created_at, item.first_accepted_at)
            for item in sets
            if item.first_accepted_at is not None
        ]
        review_spans = [
            seconds_between(item.created_at, item.reviewed_at)
            for item in sets
            if item.reviewed_at is not None
        ]
        return DraftQualityGroupStats(
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            change_set_count=len(sets),
            clarification_request_count=sum(item.clarification_request_count for item in sets),
            change_sets_with_clarifications=sum(
                1 for item in sets if item.clarification_request_count > 0
            ),
            median_seconds_to_first_accept=median(accept_spans) if accept_spans else None,
            median_seconds_to_review=median(review_spans) if review_spans else None,
        )


def _cell_sort_key(key: CellKey) -> tuple[str, str, str, bool, str]:
    provider, model, prompt_version, semantic_type = key
    return (
        provider,
        model,
        prompt_version,
        semantic_type is not None,
        semantic_type.value if semantic_type is not None else "",
    )


def aggregate_draft_quality(
    project_id: UUID,
    since: datetime | None,
    rows: Sequence[DraftQualityRow],
) -> DraftQualityLedger:
    """Fold projected rows into a deterministic ledger (input order is irrelevant)."""

    cells: dict[CellKey, _CellCounts] = {}
    groups: dict[GroupKey, _GroupFacts] = {}
    change_set_ids: set[UUID] = set()
    for row in rows:
        change_set_ids.add(row.change_set_id)
        groups.setdefault(row.group_key, _GroupFacts()).observe(row)
        if row.operation_status is None:
            continue
        cells.setdefault(row.cell_key, _CellCounts()).add(row)
    return DraftQualityLedger(
        project_id=project_id,
        since=since,
        change_set_count=len(change_set_ids),
        cells=[
            DraftQualityCell(
                provider=key[0],
                model=key[1],
                prompt_version=key[2],
                semantic_type=key[3],
                proposed=counts.proposed,
                accepted_total=counts.accepted_total,
                accepted_human_selected=counts.accepted_human_selected,
                accepted_bulk_accepted=counts.accepted_bulk_accepted,
                edited_before_accept=counts.edited_before_accept,
                rejected=counts.rejected,
                left_proposed_at_commit=counts.left_proposed_at_commit,
            )
            for key, counts in sorted(cells.items(), key=lambda item: _cell_sort_key(item[0]))
        ],
        groups=[groups[key].stats(key) for key in sorted(groups)],
    )
