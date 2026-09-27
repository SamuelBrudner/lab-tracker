"""Graph draft context packet assembly."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from lab_tracker.auth import AuthContext
from lab_tracker.claim_effective_status import (
    OPEN_PREDICTION_STATUSES,
    ClaimInterpretation,
    load_claim_interpretations,
)
from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.models import (
    REVIEW_NOTE_KEY,
    REVIEWED_AT_KEY,
    REVIEWED_BY_KEY,
    Analysis,
    Claim,
    Dataset,
    EntityRef,
    EntityType,
    ExplorationNode,
    ExternalContextPolicy,
    Goal,
    GraphChangeOp,
    GraphChangeOperation,
    GraphChangeOperationStatus,
    GraphChangeSet,
    GraphChangeSetStatus,
    GraphDraftMode,
    Note,
    NoteStatus,
    Project,
    Question,
    QuestionStatus,
    Session,
    Visualization,
    utc_now,
)
from lab_tracker.note_text import NoteTextExcerpt, is_text_content_type
from lab_tracker.services.analysis_service import AnalysisService
from lab_tracker.services.claim_service import ClaimService
from lab_tracker.services.dataset_service import DatasetService
from lab_tracker.services.exploration_service import ExplorationService
from lab_tracker.services.goal_service import GoalService
from lab_tracker.services.graph_draft_batch_policy import (
    BatchReviewer,
    as_utc,
    context_owner_for,
    note_matches_reviewer,
)
from lab_tracker.services.note_observed_at import note_observed_at
from lab_tracker.services.note_service import NoteService
from lab_tracker.services.project_service import ProjectService
from lab_tracker.services.question_service import QuestionService
from lab_tracker.services.session_service import SessionService
from lab_tracker.services.shared import is_meeting_note
from lab_tracker.services.visualization_service import VisualizationService

if TYPE_CHECKING:
    from lab_tracker.schemas import GraphSearchHit

EntityResult = (
    Project
    | Question
    | Note
    | Session
    | Dataset
    | Analysis
    | Claim
    | Visualization
    | Goal
    | ExplorationNode
)
_RECENT_CONTEXT_LIMIT = 10
# Open predictions (proposed/testing claims answering a question) shown to the
# drafter so it can propose resolve_prediction when their evidence lands.
_OPEN_PREDICTION_LIMIT = 20
QUESTION_CONTEXT_LIMIT = 50
# Slots reserved for active questions (most recently updated first) before
# staged questions fill whatever remains of QUESTION_CONTEXT_LIMIT.
ACTIVE_QUESTION_FLOOR = 50
_CAPTURE_BUNDLE_LIMIT = 6
BATCH_SOURCE_CONTEXT_CHAR_BUDGET = 256_000
CONTEXT_FIELD_CHAR_LIMIT = 240
CUE_TERM_MIN_LENGTH = 4
CUE_TERM_LIMIT = 8
CUE_MATCH_PER_TERM_LIMIT = 5
CUE_MATCHED_LIMIT = 20
CUE_MATCH_ENTITY_TYPES = ("question", "claim", "dataset", "session")
# English function words only (articles, pronouns, prepositions, auxiliaries,
# conjunctions); no domain vocabulary, so rare lab terms always survive.
CUE_STOPWORDS = frozenset(
    {
        "about", "above", "after", "again", "against", "also", "among", "and",
        "another", "any", "are", "around", "because", "been", "before", "being",
        "below", "between", "both", "but", "can", "could", "did", "does", "doing",
        "down", "during", "each", "either", "else", "even", "ever", "every", "few",
        "for", "from", "further", "had", "has", "have", "having", "her", "here",
        "hers", "him", "his", "how", "however", "into", "its", "itself", "just",
        "may", "might", "more", "most", "much", "must", "neither", "nor", "not",
        "off", "once", "only", "onto", "other", "ought", "our", "ours", "out",
        "over", "own", "same", "shall", "she", "should", "since", "some", "such",
        "than", "that", "the", "their", "theirs", "them", "then", "there", "these",
        "they", "this", "those", "through", "thus", "too", "under", "until", "upon",
        "very", "was", "were", "what", "when", "where", "whether", "which", "while",
        "who", "whom", "whose", "why", "will", "with", "within", "without", "would",
        "you", "your", "yours",
    }
)
SELECTION_REASON_ACTIVE_FLOOR = "active_floor"
SELECTION_REASON_STAGED_FILL = "staged_fill"
SELECTION_REASON_RECENT = "recent"
SELECTION_REASON_ALIAS_MATCH = "alias_match"
SELECTION_REASON_CUE_MATCH_PREFIX = "cue_match:"
SLOT_FILL_CUE_MATCH = "cue_match"
# Every list of selected graph context in a packet or project block; each
# item carries selection_reason and is counted by context_summary.slot_fill.
_SELECTED_CONTEXT_KEYS = (
    "active_or_staged_questions",
    "recent_sessions",
    "recent_datasets",
    "recent_notes",
    "recent_analyses",
    "recent_claims",
    "recent_visualizations",
    "recent_goals",
    "exploration_nodes",
    "cue_matched",
    "known_aliases",
)
_CUE_TOKEN_SPLIT = re.compile(r"[^0-9a-z]+")
# Review memory: what this reviewer already has under review (so the drafter
# cites instead of duplicating) and what they recently rejected (so a
# re-proposal must state new evidence). Reviewer-scoped and capped.
REVIEW_MEMORY_PENDING_STATUSES = frozenset(
    {
        GraphChangeSetStatus.READY,
        GraphChangeSetStatus.SUBMITTED,
        GraphChangeSetStatus.CHANGES_REQUESTED,
    }
)
# The one status set queried per project; pending and rejected are split in Python.
REVIEW_MEMORY_REJECTION_STATUSES = frozenset(
    {
        *REVIEW_MEMORY_PENDING_STATUSES,
        GraphChangeSetStatus.REJECTED,
        GraphChangeSetStatus.COMMITTED,
    }
)
_REVIEW_MEMORY_QUERY_LIMIT = 50
PENDING_PROPOSALS_ITEM_LIMIT = 25
PENDING_PROPOSALS_CHAR_BUDGET = 8_000
RECENT_REJECTIONS_ITEM_LIMIT = 15
RECENT_REJECTIONS_WINDOW_DAYS = 14
REVIEW_MEMORY_NOTE_MAX_CHARS = 200
REVIEW_MEMORY_NOT_SCOPED_WARNING = "review memory not reviewer-scoped"
_MISSING_TARGET_LABEL = "(missing)"
_CREATE_LABEL_FIELDS = ("text", "title", "statement", "raw_content")
# recent_notes.author_scope: whether the packet owner captured the note.
AUTHOR_SCOPE_OWN = "own"
AUTHOR_SCOPE_COLLEAGUE = "colleague"
# json.dumps list rendering adds ", " per item (or the brackets for the last).
_JSON_LIST_ITEM_OVERHEAD = 2

if ACTIVE_QUESTION_FLOOR > QUESTION_CONTEXT_LIMIT:
    raise ValueError("ACTIVE_QUESTION_FLOOR must not exceed QUESTION_CONTEXT_LIMIT.")


class ReviewMemoryRecords(Protocol):
    """Narrow change-set read that feeds reviewer-scoped review memory."""

    def list_review_memory_change_sets(
        self,
        project_id: UUID,
        *,
        statuses: set[GraphChangeSetStatus],
        limit: int,
    ) -> list[GraphChangeSet]: ...


@dataclass(frozen=True)
class _Rejection:
    change_set: GraphChangeSet
    operation: GraphChangeOperation
    note: str
    rejected_at: datetime


@dataclass(frozen=True)
class _QuestionContext:
    active: list[Question]
    staged: list[Question]
    superseded: list[Question]


class GraphContextBuilder:
    def __init__(
        self,
        *,
        projects: ProjectService,
        questions: QuestionService,
        notes: NoteService,
        sessions: SessionService,
        datasets: DatasetService,
        analyses: AnalysisService,
        claims: ClaimService,
        visualizations: VisualizationService,
        goals: GoalService | None = None,
        exploration: ExplorationService | None = None,
        review_memory: ReviewMemoryRecords | None = None,
    ) -> None:
        self.projects = projects
        self.questions = questions
        self.notes = notes
        self.sessions = sessions
        self.datasets = datasets
        self.analyses = analyses
        self.claims = claims
        self.visualizations = visualizations
        self.goals = goals
        self.exploration = exploration
        self.review_memory = review_memory

    def build_batch_graph_context(
        self,
        notes: list[Note],
        *,
        window: tuple[Any, Any] | None = None,
        actor: AuthContext | None = None,
        batch_note_limit: int = 100,
        context_owner: BatchReviewer | None = None,
        external_context_policy: ExternalContextPolicy = ExternalContextPolicy.OWN_NOTES_ONLY,
    ) -> dict[str, Any]:
        """Assemble a context packet covering a batch of staged notes.

        The batch may span multiple projects; graph context (questions,
        recent sessions/datasets/notes/analyses/claims/visualizations,
        known_aliases) is grouped per project. The packet is the input
        the daily-batch draft generator (lab-tracker-641) consumes.

        Caller is responsible for filtering to staged notes and choosing
        the window. The batch is capped at batch_note_limit; overflow is
        reported as truncated_note_count. ``context_owner`` is the reviewer
        whose pending proposals and recent rejections become review_memory
        and whose authorship scopes ``recent_notes``: under
        ``own_notes_only`` colleagues' notes never enter the packet, under
        ``project_notes`` they do, labelled ``author_scope=colleague``. With
        no owner there is no one to scope to, so authorship is unknown and
        nothing is filtered.
        """
        truncated_note_count = max(0, len(notes) - batch_note_limit)
        # Chronological order gives the day a contractual timeline rather than
        # relying on incidental input ordering. The model sees the day in
        # capture-clock order (client, then adapter, then server receipt);
        # which notes belong to the batch was decided upstream on created_at.
        batch_notes = sorted(
            notes,
            key=lambda item: (note_observed_at(item)[0], str(item.note_id)),
        )[:batch_note_limit]

        notes_by_project: dict[UUID, list[Note]] = {}
        for note in batch_notes:
            notes_by_project.setdefault(note.project_id, []).append(note)

        sessions_by_project: dict[UUID, list[Session]] = {}
        project_blocks: list[dict[str, Any]] = []
        for project_id, project_notes in notes_by_project.items():
            try:
                project = self.projects.get_project(project_id)
            except NotFoundError:
                continue
            batch_ids_in_project = {n.note_id for n in project_notes}
            question_context = self._question_context(project_id)
            recent_notes = self._recent_notes_excluding(
                project_id,
                batch_ids_in_project,
                owner=context_owner,
                policy=external_context_policy,
            )
            recent_sessions = self._recent_sessions(project_id)
            sessions_by_project[project_id] = recent_sessions
            recent_datasets = self._recent_datasets(project_id)
            recent_analyses = self._recent_analyses(project_id)
            recent_claims = self._recent_claims(project_id)
            open_predictions = self._open_predictions(project_id)
            interpretations = load_claim_interpretations(
                self.claims.repository, [*recent_claims, *open_predictions]
            )
            recent_visualizations = self._recent_visualizations(project_id)
            recent_goals = self._recent_goals(project_id)
            exploration_nodes = self._recent_exploration_nodes(project_id)
            cue_terms = _cue_terms([_note_cue_text(item) for item in project_notes])
            project_blocks.append(
                {
                    "id": str(project.project_id),
                    "label": project.name,
                    "status": project.status.value,
                    "note_ids_in_batch": [str(n.note_id) for n in project_notes],
                    "active_or_staged_questions": _active_or_staged_questions(
                        question_context
                    ),
                    "recent_sessions": _recent_items(_compact_session, recent_sessions),
                    "recent_datasets": _recent_items(_compact_dataset, recent_datasets),
                    "recent_notes": [
                        _compact_recent_note(item, context_owner) for item in recent_notes
                    ],
                    "recent_analyses": _recent_items(_compact_analysis, recent_analyses),
                    "recent_claims": _recent_claim_items(recent_claims, interpretations),
                    "open_predictions": _open_prediction_items(open_predictions, interpretations),
                    "recent_visualizations": _recent_items(
                        _compact_visualization, recent_visualizations
                    ),
                    "recent_goals": _recent_items(_compact_goal, recent_goals),
                    "exploration_nodes": _recent_items(
                        _compact_exploration_node, exploration_nodes
                    ),
                    "cue_terms": cue_terms,
                    "cue_matched": self._cue_matched(project_id, cue_terms),
                    "known_aliases": _known_aliases(
                        project=project,
                        questions=[*question_context.active, *question_context.staged],
                        superseded_questions=question_context.superseded,
                        sessions=recent_sessions,
                        datasets=recent_datasets,
                        analyses=recent_analyses,
                        claims=recent_claims,
                        visualizations=recent_visualizations,
                        goals=recent_goals,
                    ),
                }
            )

        (
            bounded_source_artifacts,
            source_context_included_chars,
            source_context_omitted_chars,
            source_context_truncated_note_count,
        ) = _bounded_batch_source_artifacts(
            batch_notes,
            text_asset_reader=lambda note_id, max_chars: self.notes.read_note_raw_text(
                note_id,
                max_chars=max_chars,
            )[1],
        )
        source_context_included_bytes = sum(
            int(item.get("raw_asset_text_included_bytes") or 0)
            for item in bounded_source_artifacts
        )
        source_context_omitted_bytes = sum(
            int(item.get("raw_asset_text_omitted_bytes") or 0)
            for item in bounded_source_artifacts
        )
        packet: dict[str, Any] = {
            "mode": "graph_batch",
            "batch_window": _batch_window(window, batch_notes),
            "current_user": _compact_actor(actor),
            "batch_notes": [_compact_note(item, include_raw_asset=True) for item in batch_notes],
            "capture_placement": [
                _capture_placement(note, sessions_by_project.get(note.project_id, []))
                for note in batch_notes
            ],
            "source_artifacts": bounded_source_artifacts,
            "source_context_budget_chars": BATCH_SOURCE_CONTEXT_CHAR_BUDGET,
            "source_context_included_chars": source_context_included_chars,
            "source_context_omitted_chars": source_context_omitted_chars,
            "source_context_included_bytes": source_context_included_bytes,
            "source_context_omitted_bytes": source_context_omitted_bytes,
            "source_context_truncated": (
                source_context_omitted_chars > 0 or source_context_omitted_bytes > 0
            ),
            "source_context_truncated_note_count": (
                source_context_truncated_note_count
            ),
            "projects": project_blocks,
            "context_owner": _compact_owner(context_owner),
            "external_context_policy": external_context_policy.value,
            "review_memory": self.build_review_memory(
                project_ids=set(notes_by_project),
                context_owner=context_owner,
                now=utc_now(),
            ),
            "truncated_note_count": truncated_note_count,
        }
        packet["context_summary"] = _graph_batch_context_summary(packet)
        return packet

    def build_review_memory(
        self,
        *,
        project_ids: set[UUID],
        context_owner: BatchReviewer | None,
        now: datetime,
    ) -> dict[str, Any]:
        """Reviewer-scoped pending proposals and recent rejections, capped.

        Without a user-backed reviewer (or a records port) the block is empty
        and flagged ``reviewer_scoped=False`` rather than matched on a legacy
        reviewer string.
        """

        reviewer_user_id = context_owner.reviewer_user_id if context_owner is not None else None
        if self.review_memory is None or reviewer_user_id is None:
            return _empty_review_memory()
        change_sets: list[GraphChangeSet] = []
        for project_id in sorted(project_ids, key=str):
            change_sets.extend(
                self.review_memory.list_review_memory_change_sets(
                    project_id,
                    statuses=set(REVIEW_MEMORY_REJECTION_STATUSES),
                    limit=_REVIEW_MEMORY_QUERY_LIMIT,
                )
            )
        change_sets.sort(
            key=lambda item: (-as_utc(item.created_at).timestamp(), str(item.change_set_id))
        )
        pending, truncated = self._pending_proposals(change_sets, reviewer_user_id)
        cutoff = now - timedelta(days=RECENT_REJECTIONS_WINDOW_DAYS)
        return {
            "reviewer_scoped": True,
            "reviewer_user_id": str(reviewer_user_id),
            "pending_proposals": pending,
            "pending_proposals_truncated": truncated,
            "recent_rejections": self._recent_rejections(
                change_sets, reviewer_user_id, cutoff=cutoff
            ),
        }

    def _pending_proposals(
        self,
        change_sets: list[GraphChangeSet],
        reviewer_user_id: UUID,
    ) -> tuple[list[dict[str, Any]], bool]:
        items: list[dict[str, Any]] = []
        serialized_chars = 0
        for change_set in change_sets:
            if change_set.status not in REVIEW_MEMORY_PENDING_STATUSES:
                continue
            if not _reviewed_by_user(change_set, reviewer_user_id):
                continue
            for operation in change_set.operations:
                if operation.status == GraphChangeOperationStatus.REJECTED:
                    continue
                item = {
                    "change_set_id": str(change_set.change_set_id),
                    "change_set_status": change_set.status.value,
                    "semantic_type": (
                        operation.semantic_type.value if operation.semantic_type else None
                    ),
                    "op": operation.op.value,
                    "entity_type": operation.entity_type.value,
                    "target": self._operation_target_label(operation),
                }
                item_chars = len(json.dumps(item, sort_keys=True)) + _JSON_LIST_ITEM_OVERHEAD
                if (
                    len(items) >= PENDING_PROPOSALS_ITEM_LIMIT
                    or serialized_chars + item_chars > PENDING_PROPOSALS_CHAR_BUDGET
                ):
                    return items, True
                items.append(item)
                serialized_chars += item_chars
        return items, False

    def _recent_rejections(
        self,
        change_sets: list[GraphChangeSet],
        reviewer_user_id: UUID,
        *,
        cutoff: datetime,
    ) -> list[dict[str, Any]]:
        reviewer = str(reviewer_user_id)
        rejections: list[_Rejection] = []
        for change_set in change_sets:
            set_rejection = _change_set_rejection(change_set, reviewer, cutoff=cutoff)
            for operation in change_set.operations:
                rejection = _operation_rejection(change_set, operation, reviewer, cutoff=cutoff)
                if rejection is None and set_rejection is not None:
                    rejection = _Rejection(change_set, operation, *set_rejection)
                if rejection is not None:
                    rejections.append(rejection)
        rejections.sort(key=lambda item: str(item.operation.operation_id))
        rejections.sort(key=lambda item: item.rejected_at, reverse=True)
        return [
            {
                "change_set_id": str(item.change_set.change_set_id),
                "operation_id": str(item.operation.operation_id),
                "semantic_type": (
                    item.operation.semantic_type.value
                    if item.operation.semantic_type
                    else None
                ),
                "op": item.operation.op.value,
                "entity_type": item.operation.entity_type.value,
                "target": self._operation_target_label(item.operation),
                "note": item.note[:REVIEW_MEMORY_NOTE_MAX_CHARS],
                "rejected_at": item.rejected_at.isoformat(),
            }
            for item in rejections[:RECENT_REJECTIONS_ITEM_LIMIT]
        ]

    def _operation_target_label(self, operation: GraphChangeOperation) -> str:
        if operation.op == GraphChangeOp.UPDATE:
            if operation.target_entity_id is None:
                return _MISSING_TARGET_LABEL
            try:
                entity = self.get_graph_entity(operation.entity_type, operation.target_entity_id)
            except (NotFoundError, ValidationError):
                return _MISSING_TARGET_LABEL
            return _entity_label(operation.entity_type, entity)[:REVIEW_MEMORY_NOTE_MAX_CHARS]
        for field_name in _CREATE_LABEL_FIELDS:
            value = operation.payload.get(field_name)
            if isinstance(value, str) and value.strip():
                return value.strip()[:REVIEW_MEMORY_NOTE_MAX_CHARS]
        return ""

    def prepare_note_sources_for_graph_draft(
        self,
        note_id: UUID,
        *,
        mode: GraphDraftMode,
        source_note_ids: list[UUID] | None = None,
    ) -> dict[str, Any]:
        note = self.notes.get_note(note_id)
        source_notes = (
            self._source_notes_for_capture(note)
            if source_note_ids is None
            else self._source_notes_for_ids(note, source_note_ids)
        )
        audio_notes = [
            item
            for item in source_notes
            if item.raw_asset is not None
            and item.raw_asset.content_type.lower().startswith("audio/")
        ]
        if mode == GraphDraftMode.GRAPH_CONTEXT:
            missing_transcripts = [item for item in audio_notes if not item.transcribed_text]
            if missing_transcripts:
                raise ValidationError(
                    "Voice notes must have an editable transcript before graph drafting."
                )
        image_note = _preferred_image_note(note, source_notes)
        if mode == GraphDraftMode.IMAGE_ONLY and image_note is None:
            raise ValidationError("Image-only graph drafting requires a raw image asset.")
        primary_note = image_note or note
        primary_raw_asset = primary_note.raw_asset
        image_bytes: bytes | None = None
        image_content_type: str | None = None
        if image_note is not None:
            try:
                raw_asset, image_bytes = self.notes.download_note_raw(image_note.note_id)
            except NotFoundError as exc:
                raise NotFoundError("Source image file is unavailable.") from exc
            # Other storage failures are server faults and propagate (HTTP 500
            # with a logged traceback) rather than posing as client errors.
            if not image_bytes:
                raise ValidationError("Source image file is empty.")
            image_content_type = raw_asset.content_type
            primary_raw_asset = raw_asset
        if not image_bytes and not any(
            item.transcribed_text or item.raw_content for item in source_notes
        ):
            if note.raw_asset is None:
                raise ValidationError(
                    "Graph drafting requires a note with a raw image asset or transcript text."
                )
            raise ValidationError(
                "Graph drafting requires a raw image asset, text note, or voice transcript."
            )
        source_artifacts = [_source_artifact_packet(item) for item in source_notes]
        return {
            "source_note": note,
            "source_notes": source_notes,
            "source_artifacts": source_artifacts,
            "primary_raw_asset": primary_raw_asset,
            "image_bytes": image_bytes,
            "image_content_type": image_content_type,
        }

    def _source_notes_for_capture(self, note: Note) -> list[Note]:
        bundle_id = note.metadata.get("capture_bundle_id")
        if not bundle_id:
            return [note]
        # Capture clients write string bundle ids, which the repository matches
        # in SQL; other scalar values keep the exact-value project scan. The
        # exact comparison stays because SQL matches the value's text form.
        candidates = (
            self.notes.list_notes(project_id=note.project_id, capture_bundle_id=bundle_id)
            if isinstance(bundle_id, str)
            else self.notes.list_notes(project_id=note.project_id)
        )
        bundle_notes = [
            item for item in candidates if item.metadata.get("capture_bundle_id") == bundle_id
        ]
        if not any(item.note_id == note.note_id for item in bundle_notes):
            bundle_notes.append(note)
        return sorted(
            bundle_notes,
            key=lambda item: (item.created_at, str(item.note_id)),
        )[:_CAPTURE_BUNDLE_LIMIT]

    def _source_notes_for_ids(self, note: Note, source_note_ids: list[UUID]) -> list[Note]:
        """Load a frozen source set while preserving its original order."""

        requested_ids = list(source_note_ids)
        if note.note_id not in requested_ids:
            requested_ids.insert(0, note.note_id)
        source_notes: list[Note] = []
        seen: set[UUID] = set()
        for source_note_id in requested_ids:
            if source_note_id in seen:
                continue
            seen.add(source_note_id)
            source_note = self.notes.get_note(source_note_id)
            if source_note.project_id != note.project_id:
                raise ValidationError("Graph draft source notes must share one project.")
            source_notes.append(source_note)
        return source_notes

    def build_graph_context_packet(
        self,
        note: Note,
        *,
        source_notes: list[Note],
        user_hint: str | None,
        actor: AuthContext | None = None,
        external_context_policy: ExternalContextPolicy = ExternalContextPolicy.OWN_NOTES_ONLY,
    ) -> dict[str, Any]:
        try:
            project = self.projects.get_project(note.project_id)
        except NotFoundError as exc:
            raise ValidationError(
                "Graph context cannot be built because the note project does not exist."
            ) from exc
        # A note-scoped draft is owned by the person asking for it, so that
        # is the identity recent_notes are scoped and attributed against.
        owner = context_owner_for(None, None, actor)
        question_context = self._question_context(note.project_id)
        recent_notes = self._recent_notes_excluding(
            note.project_id,
            {note.note_id},
            owner=owner,
            policy=external_context_policy,
        )
        recent_sessions = self._recent_sessions(note.project_id)
        recent_datasets = self._recent_datasets(note.project_id)
        recent_analyses = self._recent_analyses(note.project_id)
        recent_claims = self._recent_claims(note.project_id)
        open_predictions = self._open_predictions(note.project_id)
        interpretations = load_claim_interpretations(
            self.claims.repository, [*recent_claims, *open_predictions]
        )
        recent_visualizations = self._recent_visualizations(note.project_id)
        recent_goals = self._recent_goals(note.project_id)
        exploration_nodes = self._recent_exploration_nodes(note.project_id)
        cue_terms = _cue_terms([_note_cue_text(item) for item in source_notes])
        context_packet = {
            "mode": GraphDraftMode.GRAPH_CONTEXT.value,
            "user_hint": user_hint,
            "current_user": _compact_actor(actor),
            "source_note": _compact_note(note, include_raw_asset=True),
            "source_artifacts": [_source_artifact_packet(item) for item in source_notes],
            "selected_targets": [
                self._compact_target_ref(target, note.project_id) for target in note.targets
            ],
            "project": {
                "id": str(project.project_id),
                "label": project.name,
                "status": project.status.value,
            },
            "active_or_staged_questions": _active_or_staged_questions(question_context),
            "recent_sessions": _recent_items(_compact_session, recent_sessions),
            "recent_datasets": _recent_items(_compact_dataset, recent_datasets),
            "recent_notes": [_compact_recent_note(item, owner) for item in recent_notes],
            "recent_analyses": _recent_items(_compact_analysis, recent_analyses),
            "recent_claims": _recent_claim_items(recent_claims, interpretations),
            "open_predictions": _open_prediction_items(open_predictions, interpretations),
            "recent_visualizations": _recent_items(
                _compact_visualization, recent_visualizations
            ),
            "recent_goals": _recent_items(_compact_goal, recent_goals),
            "exploration_nodes": _recent_items(_compact_exploration_node, exploration_nodes),
            "cue_terms": cue_terms,
            "cue_matched": self._cue_matched(note.project_id, cue_terms),
            "known_aliases": _known_aliases(
                project=project,
                questions=[*question_context.active, *question_context.staged],
                superseded_questions=question_context.superseded,
                sessions=recent_sessions,
                datasets=recent_datasets,
                analyses=recent_analyses,
                claims=recent_claims,
                visualizations=recent_visualizations,
                goals=recent_goals,
            ),
            "unresolved_recent_captures": [
                _compact_recent_note(item, owner)
                for item in recent_notes
                if item.raw_asset is not None
                and item.metadata.get("capture_source") == "mobile_capture"
                and item.status == NoteStatus.STAGED
            ],
            "context_owner": _compact_owner(owner),
            "external_context_policy": external_context_policy.value,
        }
        context_packet["context_summary"] = _graph_context_summary(context_packet)
        return context_packet

    def image_only_context_packet(
        self,
        note: Note,
        *,
        source_notes: list[Note],
        user_hint: str | None,
    ) -> dict[str, Any]:
        context_packet = {
            "mode": GraphDraftMode.IMAGE_ONLY.value,
            "user_hint": user_hint,
            "source_note": _compact_note(note, include_raw_asset=True),
            "source_artifacts": [_source_artifact_packet(item) for item in source_notes],
            "warning": (
                "Image-only draft was explicitly requested without graph context or "
                "voice transcript grounding."
            ),
        }
        context_packet["context_summary"] = _graph_context_summary(context_packet)
        return context_packet

    def _compact_target_ref(self, target: EntityRef, project_id: UUID) -> dict[str, Any]:
        try:
            entity = self.get_graph_entity(target.entity_type, target.entity_id)
        except NotFoundError:
            return {
                "entity_type": target.entity_type.value,
                "entity_id": str(target.entity_id),
                "label": "(missing)",
            }
        if (
            target.entity_type != EntityType.VISUALIZATION
            and hasattr(entity, "project_id")
            and entity.project_id != project_id
        ):
            raise ValidationError("Target must belong to the same project.")
        return {
            "entity_type": target.entity_type.value,
            "entity_id": str(target.entity_id),
            "label": _entity_label(target.entity_type, entity),
            "status": getattr(getattr(entity, "status", None), "value", None),
        }

    def get_graph_entity(self, entity_type: EntityType, entity_id: UUID) -> EntityResult:
        getters = {
            EntityType.PROJECT: self.projects.get_project,
            EntityType.QUESTION: self.questions.get_question,
            EntityType.NOTE: self.notes.get_note,
            EntityType.SESSION: self.sessions.get_session,
            EntityType.DATASET: self.datasets.get_dataset,
            EntityType.ANALYSIS: self.analyses.get_analysis,
            EntityType.CLAIM: self.claims.get_claim,
            EntityType.VISUALIZATION: self.visualizations.get_visualization,
        }
        if self.goals is not None:
            getters[EntityType.GOAL] = self.goals.get_goal
        if self.exploration is not None:
            getters[EntityType.EXPLORATION_NODE] = self.exploration.get_exploration_node
        getter = getters.get(entity_type)
        if getter is None:
            raise ValidationError("Unsupported entity type.")
        return getter(entity_id)

    def _recent_goals(self, project_id: UUID) -> list[Goal]:
        if self.goals is None:
            return []
        goals, _ = self.goals.repository.query_goals(
            project_id=project_id,
            limit=_RECENT_CONTEXT_LIMIT,
            offset=0,
            recent_first=True,
        )
        return goals

    def _recent_exploration_nodes(self, project_id: UUID) -> list[ExplorationNode]:
        if self.exploration is None:
            return []
        nodes, _ = self.exploration.repository.query_exploration_nodes(
            project_id=project_id,
            limit=_RECENT_CONTEXT_LIMIT,
            offset=0,
            recent_first=True,
        )
        return nodes

    def _question_context(self, project_id: UUID) -> _QuestionContext:
        """Active questions fill first (newest update first); staged fill the rest."""

        active_questions, _ = self.questions.repository.query_questions(
            project_id=project_id,
            status=QuestionStatus.ACTIVE.value,
            limit=ACTIVE_QUESTION_FLOOR,
            offset=0,
            updated_first=True,
        )
        staged_limit = QUESTION_CONTEXT_LIMIT - len(active_questions)
        staged_questions: list[Question] = []
        if staged_limit > 0:
            staged_questions, _ = self.questions.repository.query_questions(
                project_id=project_id,
                status=QuestionStatus.STAGED.value,
                limit=staged_limit,
                offset=0,
                updated_first=True,
            )
        context_ids = {
            question.question_id for question in [*active_questions, *staged_questions]
        }
        superseded_candidates, _ = self.questions.repository.query_questions(
            project_id=project_id,
            status=QuestionStatus.SUPERSEDED.value,
            superseded_by_question_ids=context_ids,
            limit=QUESTION_CONTEXT_LIMIT * 2,
            offset=0,
            updated_first=True,
        )
        return _QuestionContext(
            active=active_questions,
            staged=staged_questions,
            superseded=superseded_candidates,
        )

    def _cue_matched(self, project_id: UUID, terms: list[str]) -> list[dict[str, Any]]:
        """Project-wide ranked matches for each cue term, deduplicated and bounded."""

        matched: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for term in terms:
            hits = self.questions.repository.search_graph_nodes(
                project_id=project_id,
                query=term,
                entity_types=CUE_MATCH_ENTITY_TYPES,
                limit=CUE_MATCH_PER_TERM_LIMIT,
            )
            for hit in hits:
                key = (hit.node.entity_type, hit.node.entity_id)
                if key in seen:
                    continue
                seen.add(key)
                matched.append(_compact_search_hit(hit, term))
                if len(matched) >= CUE_MATCHED_LIMIT:
                    return matched
        return matched

    def _recent_notes_excluding(
        self,
        project_id: UUID,
        excluded_note_ids: set[UUID],
        *,
        owner: BatchReviewer | None,
        policy: ExternalContextPolicy,
    ) -> list[Note]:
        """The newest project notes the packet may carry, minus the sources.

        ``own_notes_only`` keeps the owner's notes: by author id when the
        owner is user-backed (the query does the filtering, so older own
        notes are never crowded out by colleagues' newer ones), else by the
        legacy author string. ``project_notes`` keeps every author.
        """

        own_only = policy is ExternalContextPolicy.OWN_NOTES_ONLY and owner is not None
        recent_notes, _ = self.notes.repository.query_notes(
            project_id=project_id,
            created_by=(
                str(owner.reviewer_user_id)
                if own_only and owner is not None and owner.reviewer_user_id is not None
                else None
            ),
            limit=_RECENT_CONTEXT_LIMIT + len(excluded_note_ids),
            offset=0,
            recent_first=True,
        )
        return [
            note
            for note in recent_notes
            if note.note_id not in excluded_note_ids
            and (not own_only or note_matches_reviewer(note, owner))
        ][:_RECENT_CONTEXT_LIMIT]

    def _recent_sessions(self, project_id: UUID) -> list[Session]:
        recent_sessions, _ = self.sessions.repository.query_sessions(
            project_id=project_id,
            limit=_RECENT_CONTEXT_LIMIT,
            offset=0,
            recent_first=True,
        )
        return recent_sessions

    def _recent_datasets(self, project_id: UUID) -> list[Dataset]:
        recent_datasets, _ = self.datasets.repository.query_datasets(
            project_id=project_id,
            limit=_RECENT_CONTEXT_LIMIT,
            offset=0,
            recent_first=True,
        )
        return recent_datasets

    def _recent_analyses(self, project_id: UUID) -> list[Analysis]:
        recent_analyses, _ = self.analyses.repository.query_analyses(
            project_id=project_id,
            limit=_RECENT_CONTEXT_LIMIT,
            offset=0,
            recent_first=True,
        )
        return recent_analyses

    def _recent_claims(self, project_id: UUID) -> list[Claim]:
        recent_claims, _ = self.claims.repository.query_claims(
            project_id=project_id,
            limit=_RECENT_CONTEXT_LIMIT,
            offset=0,
            recent_first=True,
        )
        return recent_claims

    def _open_predictions(self, project_id: UUID) -> list[Claim]:
        """Proposed/testing claims that answer a question, oldest first, capped."""

        candidates: list[Claim] = []
        for status in sorted(OPEN_PREDICTION_STATUSES, key=lambda item: item.value):
            claims, _ = self.claims.repository.query_claims(
                project_id=project_id,
                status=status.value,
                limit=None,
                offset=0,
            )
            candidates.extend(claim for claim in claims if claim.answers_question_ids)
        candidates.sort(key=lambda claim: (claim.created_at, str(claim.claim_id)))
        return candidates[:_OPEN_PREDICTION_LIMIT]

    def _recent_visualizations(self, project_id: UUID) -> list[Visualization]:
        recent_visualizations, _ = self.visualizations.repository.query_visualizations(
            project_id=project_id,
            limit=_RECENT_CONTEXT_LIMIT,
            offset=0,
            recent_first=True,
        )
        return recent_visualizations


def _graph_context_summary(context_packet: dict[str, Any]) -> dict[str, Any]:
    source_artifacts = [
        item for item in context_packet.get("source_artifacts", []) if isinstance(item, dict)
    ]
    selected_targets = [
        item for item in context_packet.get("selected_targets", []) if isinstance(item, dict)
    ]
    source_artifact_counts: dict[str, int] = {}
    warnings: list[str] = []
    for artifact in source_artifacts:
        artifact_type = str(artifact.get("type") or "unknown")
        source_artifact_counts[artifact_type] = source_artifact_counts.get(artifact_type, 0) + 1
        if artifact_type == "audio" and not str(artifact.get("transcript_text") or "").strip():
            note_id = artifact.get("note_id") or artifact.get("artifact_id") or "unknown"
            warnings.append(f"audio source {note_id} is missing an editable transcript")
    if not source_artifacts:
        warnings.append("no source artifacts were included")
    if not any(artifact.get("type") == "image" for artifact in source_artifacts):
        warnings.append("no image source artifact was included")
    return {
        "approximate_size_bytes": len(
            json.dumps(context_packet, sort_keys=True, default=str).encode("utf-8")
        ),
        "counts": {
            "projects": 1 if context_packet.get("project") else 0,
            "source_artifacts": len(source_artifacts),
            "selected_targets": len(selected_targets),
            "active_or_staged_questions": len(
                context_packet.get("active_or_staged_questions") or []
            ),
            "recent_sessions": len(context_packet.get("recent_sessions") or []),
            "recent_datasets": len(context_packet.get("recent_datasets") or []),
            "recent_notes": len(context_packet.get("recent_notes") or []),
            "colleague_recent_notes": _colleague_note_count(
                context_packet.get("recent_notes") or []
            ),
            "recent_analyses": len(context_packet.get("recent_analyses") or []),
            "recent_claims": len(context_packet.get("recent_claims") or []),
            "open_predictions": len(context_packet.get("open_predictions") or []),
            "recent_visualizations": len(context_packet.get("recent_visualizations") or []),
            "recent_goals": len(context_packet.get("recent_goals") or []),
            "exploration_nodes": len(context_packet.get("exploration_nodes") or []),
            "cue_matched": len(context_packet.get("cue_matched") or []),
            "known_aliases": len(context_packet.get("known_aliases") or []),
            "unresolved_recent_captures": len(
                context_packet.get("unresolved_recent_captures") or []
            ),
        },
        "slot_fill": _slot_fill(_selected_context_lists(context_packet)),
        "cue_terms": list(context_packet.get("cue_terms") or []),
        "selected_targets": [
            {
                "entity_type": item.get("entity_type"),
                "entity_id": item.get("entity_id"),
                "label": item.get("label"),
            }
            for item in selected_targets
        ],
        "source_artifact_counts": source_artifact_counts,
        "warnings": warnings,
    }


def _graph_batch_context_summary(packet: dict[str, Any]) -> dict[str, Any]:
    source_artifacts = [
        item for item in packet.get("source_artifacts", []) if isinstance(item, dict)
    ]
    source_artifact_counts: dict[str, int] = {}
    warnings: list[str] = []
    for artifact in source_artifacts:
        artifact_type = str(artifact.get("type") or "unknown")
        source_artifact_counts[artifact_type] = source_artifact_counts.get(artifact_type, 0) + 1
        if artifact_type == "audio" and not str(artifact.get("transcript_text") or "").strip():
            note_id = artifact.get("note_id") or "unknown"
            warnings.append(f"audio source {note_id} is missing an editable transcript")
    if not source_artifacts:
        warnings.append("no source artifacts were included")
    truncated = int(packet.get("truncated_note_count") or 0)
    if truncated:
        warnings.append(f"batch truncated; {truncated} note(s) omitted")
    source_context_truncated = bool(packet.get("source_context_truncated"))
    source_context_omitted_chars = int(
        packet.get("source_context_omitted_chars") or 0
    )
    source_context_omitted_bytes = int(
        packet.get("source_context_omitted_bytes") or 0
    )
    source_context_truncated_note_count = int(
        packet.get("source_context_truncated_note_count") or 0
    )
    if source_context_truncated:
        warnings.append(
            "source text truncated by per-note preview and/or aggregate batch budget; "
            f"{source_context_omitted_chars} inline character(s) and "
            f"{source_context_omitted_bytes} uploaded-text byte(s) omitted across "
            f"{source_context_truncated_note_count} note(s)"
        )
    projects = packet.get("projects") or []
    batch_notes = [item for item in packet.get("batch_notes") or [] if isinstance(item, dict)]
    meeting_note_count = sum(1 for item in batch_notes if item.get("is_meeting"))
    review_memory = packet.get("review_memory")
    if not isinstance(review_memory, dict):
        review_memory = _empty_review_memory()
    pending_proposal_count = len(review_memory.get("pending_proposals") or [])
    recent_rejection_count = len(review_memory.get("recent_rejections") or [])
    reviewer_scoped = bool(review_memory.get("reviewer_scoped"))
    if not reviewer_scoped:
        warnings.append(REVIEW_MEMORY_NOT_SCOPED_WARNING)
    return {
        "approximate_size_bytes": len(
            json.dumps(packet, sort_keys=True, default=str).encode("utf-8")
        ),
        "counts": {
            "projects": len(projects),
            "batch_notes": len(packet.get("batch_notes") or []),
            "meeting_notes": meeting_note_count,
            "source_artifacts": len(source_artifacts),
            "active_or_staged_questions": sum(
                len(p.get("active_or_staged_questions") or []) for p in projects
            ),
            "recent_notes": sum(len(p.get("recent_notes") or []) for p in projects),
            "colleague_recent_notes": sum(
                _colleague_note_count(p.get("recent_notes") or []) for p in projects
            ),
            "recent_sessions": sum(len(p.get("recent_sessions") or []) for p in projects),
            "recent_datasets": sum(len(p.get("recent_datasets") or []) for p in projects),
            "recent_analyses": sum(len(p.get("recent_analyses") or []) for p in projects),
            "recent_claims": sum(len(p.get("recent_claims") or []) for p in projects),
            "open_predictions": sum(len(p.get("open_predictions") or []) for p in projects),
            "recent_visualizations": sum(
                len(p.get("recent_visualizations") or []) for p in projects
            ),
            "recent_goals": sum(len(p.get("recent_goals") or []) for p in projects),
            "exploration_nodes": sum(len(p.get("exploration_nodes") or []) for p in projects),
            "cue_matched": sum(len(p.get("cue_matched") or []) for p in projects),
            "known_aliases": sum(len(p.get("known_aliases") or []) for p in projects),
            "pending_proposals": pending_proposal_count,
            "recent_rejections": recent_rejection_count,
        },
        "review_memory": {
            "reviewer_scoped": reviewer_scoped,
            "pending_proposals": pending_proposal_count,
            "recent_rejections": recent_rejection_count,
            "pending_proposals_truncated": bool(
                review_memory.get("pending_proposals_truncated")
            ),
        },
        "slot_fill": _slot_fill(
            item
            for block in projects
            if isinstance(block, dict)
            for item in _selected_context_lists(block)
        ),
        "cue_term_count": sum(
            len(block.get("cue_terms") or []) for block in projects if isinstance(block, dict)
        ),
        "source_artifact_counts": source_artifact_counts,
        "truncated_note_count": truncated,
        "source_context_budget_chars": int(
            packet.get("source_context_budget_chars") or 0
        ),
        "source_context_included_chars": int(
            packet.get("source_context_included_chars") or 0
        ),
        "source_context_omitted_chars": source_context_omitted_chars,
        "source_context_omitted_bytes": source_context_omitted_bytes,
        "source_context_truncated": source_context_truncated,
        "source_context_truncated_note_count": (
            source_context_truncated_note_count
        ),
        "warnings": warnings,
    }


def _compact_actor(actor: AuthContext | None) -> dict[str, Any] | None:
    if actor is None:
        return None
    return {"id": str(actor.user_id), "role": actor.role.value}


def _compact_owner(owner: BatchReviewer | None) -> dict[str, Any] | None:
    if owner is None:
        return None
    return {
        "reviewer": owner.reviewer,
        "reviewer_user_id": (
            str(owner.reviewer_user_id) if owner.reviewer_user_id is not None else None
        ),
    }


def _colleague_note_count(items: list[Any]) -> int:
    return sum(
        1
        for item in items
        if isinstance(item, dict) and item.get("author_scope") == AUTHOR_SCOPE_COLLEAGUE
    )


def _empty_review_memory() -> dict[str, Any]:
    return {
        "reviewer_scoped": False,
        "reviewer_user_id": None,
        "pending_proposals": [],
        "pending_proposals_truncated": False,
        "recent_rejections": [],
    }


def _reviewed_by_user(change_set: GraphChangeSet, reviewer_user_id: UUID) -> bool:
    """The change set is this reviewer's: assigned to them, or theirs and unassigned."""

    if change_set.review_assignee_user_id is not None:
        return change_set.review_assignee_user_id == reviewer_user_id
    return change_set.created_by_user_id == reviewer_user_id


def _change_set_rejection(
    change_set: GraphChangeSet,
    reviewer: str,
    *,
    cutoff: datetime,
) -> tuple[str, datetime] | None:
    """(note, rejected_at) when this reviewer rejected the whole set recently."""

    if change_set.status != GraphChangeSetStatus.REJECTED:
        return None
    if change_set.reviewed_by != reviewer or change_set.reviewed_at is None:
        return None
    note = (change_set.review_note or "").strip()
    rejected_at = as_utc(change_set.reviewed_at)
    if not note or rejected_at < cutoff:
        return None
    return note, rejected_at


def _operation_rejection(
    change_set: GraphChangeSet,
    operation: GraphChangeOperation,
    reviewer: str,
    *,
    cutoff: datetime,
) -> _Rejection | None:
    """This reviewer's recent, note-carrying rejection of one operation."""

    if operation.status != GraphChangeOperationStatus.REJECTED:
        return None
    metadata = operation.error_metadata or {}
    if metadata.get(REVIEWED_BY_KEY) != reviewer:
        return None
    reviewed_at = metadata.get(REVIEWED_AT_KEY)
    if not isinstance(reviewed_at, str):
        return None
    note = str(operation.review_note or metadata.get(REVIEW_NOTE_KEY) or "").strip()
    rejected_at = as_utc(datetime.fromisoformat(reviewed_at))
    if not note or rejected_at < cutoff:
        return None
    return _Rejection(change_set, operation, note, rejected_at)


def _capped_text(value: str | None) -> str | None:
    if not value:
        return None
    return value[:CONTEXT_FIELD_CHAR_LIMIT]


def _with_selection_reason(payload: dict[str, Any], reason: str) -> dict[str, Any]:
    payload["selection_reason"] = reason
    return payload


def _recent_items(
    compact: Callable[[Any], dict[str, Any]],
    items: Iterable[Any],
) -> list[dict[str, Any]]:
    return [_with_selection_reason(compact(item), SELECTION_REASON_RECENT) for item in items]


def _active_or_staged_questions(context: _QuestionContext) -> list[dict[str, Any]]:
    return [
        _with_selection_reason(_compact_question(item), SELECTION_REASON_ACTIVE_FLOOR)
        for item in context.active
    ] + [
        _with_selection_reason(_compact_question(item), SELECTION_REASON_STAGED_FILL)
        for item in context.staged
    ]


def _cue_terms(texts: list[str]) -> list[str]:
    """Rare-first lexical cues from in-memory note text.

    Tokens shorter than CUE_TERM_MIN_LENGTH, all-digit tokens, and English
    function words are dropped; the rest are ordered by document frequency
    across the given texts (rarest first), then longest first, then
    alphabetically, and cut to CUE_TERM_LIMIT. Deterministic for equal input.
    """

    document_frequency: dict[str, int] = {}
    for text in texts:
        tokens = {
            token
            for token in _CUE_TOKEN_SPLIT.split(text.lower())
            if len(token) >= CUE_TERM_MIN_LENGTH
            and not token.isdigit()
            and token not in CUE_STOPWORDS
        }
        for token in tokens:
            document_frequency[token] = document_frequency.get(token, 0) + 1
    ordered = sorted(
        document_frequency,
        key=lambda token: (document_frequency[token], -len(token), token),
    )
    return ordered[:CUE_TERM_LIMIT]


def _note_cue_text(note: Note) -> str:
    return note.transcribed_text or note.raw_content or ""


def _compact_search_hit(hit: GraphSearchHit, term: str) -> dict[str, Any]:
    node = hit.node
    return {
        "entity_type": node.entity_type,
        "id": node.entity_id,
        "label": node.label,
        "detail": node.detail,
        "status": node.status,
        "updated_at": node.updated_at.isoformat() if node.updated_at is not None else None,
        "snippet": hit.snippet,
        "match_reasons": list(hit.match_reasons),
        "selection_reason": SELECTION_REASON_CUE_MATCH_PREFIX + term,
    }


def _selected_context_lists(block: dict[str, Any]) -> list[list[dict[str, Any]]]:
    return [
        [item for item in (block.get(key) or []) if isinstance(item, dict)]
        for key in _SELECTED_CONTEXT_KEYS
    ]


def _slot_fill(lists: Iterable[list[dict[str, Any]]]) -> dict[str, int]:
    """Count selection_reason values across lists; cue_match:<term> collapses to cue_match."""

    counts = {
        SELECTION_REASON_ACTIVE_FLOOR: 0,
        SELECTION_REASON_STAGED_FILL: 0,
        SLOT_FILL_CUE_MATCH: 0,
        SELECTION_REASON_RECENT: 0,
        SELECTION_REASON_ALIAS_MATCH: 0,
    }
    for items in lists:
        for item in items:
            reason = str(item.get("selection_reason") or "")
            if reason.startswith(SELECTION_REASON_CUE_MATCH_PREFIX):
                reason = SLOT_FILL_CUE_MATCH
            if reason in counts:
                counts[reason] += 1
    return counts


def _add_origin_context(payload: dict[str, Any], entity: Any) -> None:
    origin = getattr(entity, "origin", None)
    if origin is not None:
        payload["origin"] = origin.value if hasattr(origin, "value") else str(origin)
    change_set_id = getattr(entity, "change_set_id", None)
    if change_set_id is not None:
        payload["change_set_id"] = str(change_set_id)
    origin_provider = getattr(entity, "origin_provider", None)
    if origin_provider:
        payload["origin_provider"] = origin_provider
    origin_model = getattr(entity, "origin_model", None)
    if origin_model:
        payload["origin_model"] = origin_model
    origin_prompt_version = getattr(entity, "origin_prompt_version", None)
    if origin_prompt_version:
        payload["origin_prompt_version"] = origin_prompt_version


def _batch_window(
    window: tuple[Any, Any] | None,
    batch_notes: list[Note],
) -> dict[str, str] | None:
    """Day boundaries for the batch.

    Prefer the caller's explicit window; otherwise derive it from the captures
    so the day-narrative has real start/end times instead of inventing them.
    """

    if window is not None:
        return {"since": window[0].isoformat(), "until": window[1].isoformat()}
    if not batch_notes:
        return None
    timestamps = [note_observed_at(note)[0] for note in batch_notes]
    return {"since": min(timestamps).isoformat(), "until": max(timestamps).isoformat()}


def _capture_placement(note: Note, sessions: list[Session]) -> dict[str, Any]:
    """Where a capture lands in the day.

    Pre-computes the most recent session window (if any) that contains the
    note's capture time, plus the bundle it belongs to, so terse
    identifier-only captures can be placed -- or surfaced as unplaceable --
    rather than guessed. The capture time is the note's capture clock
    (client, adapter, or server receipt), reported with its source so the
    model can weigh a phone clock differently from a server timestamp.
    """

    observed_at, observed_at_source = note_observed_at(note)
    candidates = [
        session
        for session in sessions
        if as_utc(session.started_at) <= observed_at
        and (session.ended_at is None or observed_at <= as_utc(session.ended_at))
    ]
    in_session: dict[str, str] | None = None
    if candidates:
        best = max(candidates, key=lambda session: session.started_at)
        in_session = {
            "id": str(best.session_id),
            "label": (
                f"{best.session_type.value} session "
                f"{best.started_at.date().isoformat()}"
            ),
        }
    return {
        "note_id": str(note.note_id),
        "created_at": note.created_at.isoformat(),
        "observed_at": observed_at.isoformat(),
        "observed_at_source": observed_at_source.value,
        "project_id": str(note.project_id),
        "capture_bundle_id": note.metadata.get("capture_bundle_id"),
        "in_session": in_session,
    }


def _compact_note(note: Note, *, include_raw_asset: bool = False) -> dict[str, Any]:
    preview = note.transcribed_text or note.raw_content or ""
    payload: dict[str, Any] = {
        "id": str(note.note_id),
        "project_id": str(note.project_id),
        "status": note.status.value,
        "created_at": note.created_at.isoformat(),
        "updated_at": note.updated_at.isoformat(),
        "preview": preview[:400],
        "targets": [
            {"entity_type": target.entity_type.value, "entity_id": str(target.entity_id)}
            for target in note.targets
        ],
        "metadata": dict(note.metadata),
        "is_meeting": is_meeting_note(note),
        "created_by": note.created_by,
        "created_by_user_id": (
            str(note.created_by_user_id) if note.created_by_user_id is not None else None
        ),
    }
    if include_raw_asset and note.raw_asset is not None:
        payload["raw_asset"] = {
            "filename": note.raw_asset.filename,
            "content_type": note.raw_asset.content_type,
            "size_bytes": note.raw_asset.size_bytes,
            "checksum": note.raw_asset.checksum,
        }
    _add_origin_context(payload, note)
    return payload


def _note_author_scope(note: Note, owner: BatchReviewer | None) -> str | None:
    """``own`` or ``colleague`` relative to the packet owner; None without one."""

    if owner is None:
        return None
    return AUTHOR_SCOPE_OWN if note_matches_reviewer(note, owner) else AUTHOR_SCOPE_COLLEAGUE


def _compact_recent_note(note: Note, owner: BatchReviewer | None) -> dict[str, Any]:
    # captured_by_current_user and author_scope are one fact from one owner
    # resolution (assignee, else the acting user); they can never disagree.
    payload = _with_selection_reason(_compact_note(note), SELECTION_REASON_RECENT)
    scope = _note_author_scope(note, owner)
    payload["captured_by_current_user"] = None if scope is None else scope == AUTHOR_SCOPE_OWN
    payload["author_scope"] = scope
    return payload


def _preferred_image_note(source_note: Note, source_notes: list[Note]) -> Note | None:
    candidates = [
        item
        for item in source_notes
        if item.raw_asset is not None
        and item.raw_asset.content_type.lower().startswith("image/")
    ]
    if not candidates:
        return None
    if any(item.note_id == source_note.note_id for item in candidates):
        return source_note
    return sorted(candidates, key=lambda item: (item.created_at, str(item.note_id)))[0]


def _source_artifact_type(note: Note) -> str:
    content_type = note.raw_asset.content_type.lower() if note.raw_asset is not None else ""
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("audio/"):
        return "audio"
    if is_text_content_type(content_type):
        return "text"
    if note.raw_asset is not None:
        return "file"
    return "text"


def _source_artifact_packet(note: Note) -> dict[str, Any]:
    artifact_type = _source_artifact_type(note)
    payload: dict[str, Any] = {
        "type": artifact_type,
        "note_id": str(note.note_id),
        "project_id": str(note.project_id),
        "created_at": note.created_at.isoformat(),
        "status": note.status.value,
        "metadata": dict(note.metadata),
        "is_meeting": is_meeting_note(note),
        "targets": [
            {"entity_type": target.entity_type.value, "entity_id": str(target.entity_id)}
            for target in note.targets
        ],
    }
    if note.raw_asset is not None:
        payload["artifact_id"] = str(note.raw_asset.storage_id)
        payload["filename"] = note.raw_asset.filename
        payload["content_type"] = note.raw_asset.content_type
        payload["size_bytes"] = note.raw_asset.size_bytes
        payload["checksum"] = note.raw_asset.checksum
        payload["is_text"] = note.raw_asset.is_text
    if note.transcribed_text:
        payload["transcript_id"] = f"transcript:{note.note_id}"
        payload["transcript_text"] = note.transcribed_text
        payload["transcript_is_derived"] = True
    if note.raw_content:
        payload["raw_content_preview"] = note.raw_content[:1000]
        if len(note.raw_content) > 1000:
            payload["raw_content_preview_truncated"] = True
            payload["raw_content_preview_omitted_chars"] = (
                len(note.raw_content) - 1000
            )
    return payload


def _bounded_batch_source_artifacts(
    notes: list[Note],
    *,
    budget_chars: int = BATCH_SOURCE_CONTEXT_CHAR_BUDGET,
    text_asset_reader: Callable[[UUID, int], NoteTextExcerpt] | None = None,
) -> tuple[list[dict[str, Any]], int, int, int]:
    """Share one source-text budget deterministically across batch notes.

    Notes are processed in their already-canonical chronological order. At
    each step the remaining budget is divided by the remaining note count, so
    a long early transcript cannot starve every later note. Unused allocation
    rolls forward. Metadata is retained; only untrusted source text consumes
    this provider-input budget.
    """

    if budget_chars < 0:
        raise ValueError("budget_chars must not be negative.")
    artifacts: list[dict[str, Any]] = []
    remaining = budget_chars
    included = 0
    omitted = 0
    truncated_notes = 0
    total_notes = len(notes)
    for index, note in enumerate(notes):
        artifact = _source_artifact_packet(note)
        remaining_notes = total_notes - index
        allocation = remaining // remaining_notes if remaining_notes else 0
        # The long-standing raw-content preview cap is part of the effective
        # source-text bound too. Count that omission alongside any additional
        # aggregate-budget truncation so the packet never under-reports how
        # much original source text was excluded.
        note_omitted = int(
            artifact.get("raw_content_preview_omitted_chars") or 0
        )
        used = 0
        for field_name in ("transcript_text", "raw_content_preview"):
            value = artifact.get(field_name)
            if not isinstance(value, str):
                continue
            field_allowance = max(0, allocation - used)
            if len(value) > field_allowance:
                artifact[field_name] = value[:field_allowance]
                artifact[f"{field_name}_truncated"] = True
                field_omitted = len(value) - field_allowance
                artifact[f"{field_name}_omitted_chars"] = (
                    int(artifact.get(f"{field_name}_omitted_chars") or 0) + field_omitted
                )
                note_omitted += field_omitted
                used += field_allowance
            else:
                used += len(value)
        raw_asset = note.raw_asset
        if raw_asset is not None and raw_asset.is_text and text_asset_reader is not None:
            field_allowance = max(0, allocation - used)
            if field_allowance:
                try:
                    excerpt = text_asset_reader(note.note_id, field_allowance)
                except (NotFoundError, OSError, ValidationError):
                    artifact["raw_asset_text_unavailable"] = True
                    artifact["raw_asset_text_included_bytes"] = 0
                    artifact["raw_asset_text_omitted_bytes"] = raw_asset.size_bytes
                    artifact["raw_asset_text_truncated"] = raw_asset.size_bytes > 0
                else:
                    artifact["raw_asset_text"] = excerpt.text
                    artifact["raw_asset_text_included_bytes"] = excerpt.included_bytes
                    artifact["raw_asset_text_omitted_bytes"] = excerpt.omitted_bytes
                    artifact["raw_asset_text_truncated"] = excerpt.truncated
                    used += len(excerpt.text)
            else:
                artifact["raw_asset_text"] = ""
                artifact["raw_asset_text_included_bytes"] = 0
                artifact["raw_asset_text_omitted_bytes"] = raw_asset.size_bytes
                artifact["raw_asset_text_truncated"] = raw_asset.size_bytes > 0
        remaining -= used
        included += used
        omitted += note_omitted
        raw_asset_omitted = int(
            artifact.get("raw_asset_text_omitted_bytes") or 0
        )
        if note_omitted or raw_asset_omitted:
            truncated_notes += 1
        if note_omitted:
            artifact["source_text_omitted_chars"] = note_omitted
        artifacts.append(artifact)
    return artifacts, included, omitted, truncated_notes


def _compact_question(question: Question) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(question.question_id),
        "label": question.text,
        "status": question.status.value,
        "question_type": question.question_type.value,
        "parent_question_ids": [str(item) for item in question.parent_question_ids],
        "updated_at": question.updated_at.isoformat(),
    }
    if question.terminal_reason:
        payload["terminal_reason"] = question.terminal_reason
    if question.hypothesis:
        payload["hypothesis"] = _capped_text(question.hypothesis)
    _add_origin_context(payload, question)
    return payload


def _compact_session(session: Session) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(session.session_id),
        "label": f"{session.session_type.value} session {session.started_at.date().isoformat()}",
        "status": session.status.value,
        "session_type": session.session_type.value,
        "primary_question_id": (
            str(session.primary_question_id) if session.primary_question_id else None
        ),
        "started_at": session.started_at.isoformat(),
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
    }
    _add_origin_context(payload, session)
    return payload


def _compact_dataset(dataset: Dataset) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(dataset.dataset_id),
        "label": f"Dataset {dataset.commit_hash[:12]}",
        "status": dataset.status.value,
        "primary_question_id": str(dataset.primary_question_id),
        "question_links": [
            {
                "question_id": str(link.question_id),
                "role": link.role.value,
                "outcome_status": link.outcome_status.value,
            }
            for link in dataset.question_links
        ],
        "source_session_id": (
            str(dataset.commit_manifest.source_session_id)
            if dataset.commit_manifest.source_session_id
            else None
        ),
        "created_at": dataset.created_at.isoformat(),
    }
    if dataset.terminal_reason:
        payload["terminal_reason"] = dataset.terminal_reason
    _add_origin_context(payload, dataset)
    return payload


def _compact_analysis(analysis: Analysis) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(analysis.analysis_id),
        "label": analysis.method_hash,
        "status": analysis.status.value,
        "dataset_ids": [str(item) for item in analysis.dataset_ids],
        "code_version": analysis.code_version,
        "created_at": analysis.created_at.isoformat(),
    }
    if analysis.terminal_reason:
        payload["terminal_reason"] = analysis.terminal_reason
    _add_origin_context(payload, analysis)
    return payload


def _recent_claim_items(
    claims: Iterable[Claim],
    interpretations: dict[UUID, ClaimInterpretation],
) -> list[dict[str, Any]]:
    return [
        _with_selection_reason(
            _compact_claim(claim, interpretations[claim.claim_id]), SELECTION_REASON_RECENT
        )
        for claim in claims
    ]


def _open_prediction_items(
    claims: Iterable[Claim],
    interpretations: dict[UUID, ClaimInterpretation],
) -> list[dict[str, Any]]:
    return [_compact_open_prediction(claim, interpretations[claim.claim_id]) for claim in claims]


def _compact_open_prediction(claim: Claim, interpretation: ClaimInterpretation) -> dict[str, Any]:
    """A claim compacted for open_predictions: the Popperian fields are always present."""

    payload = _compact_claim(claim, interpretation)
    for field_name in ("falsification_criteria", "verification_plan", "refuting_outcome"):
        payload[field_name] = _capped_text(getattr(claim, field_name))
    return payload


def _compact_claim(claim: Claim, interpretation: ClaimInterpretation) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(claim.claim_id),
        "label": claim.statement[:180],
        "status": claim.status.value,
        "effective_status": interpretation.effective_status.value,
        "superseded_by_claim_id": (
            str(interpretation.superseded_by_claim_id)
            if interpretation.superseded_by_claim_id is not None
            else None
        ),
        "contested_by_claim_ids": [str(item) for item in interpretation.contested_by_claim_ids],
        "invalidated_by_node_id": (
            str(interpretation.invalidated_by_node_id)
            if interpretation.invalidated_by_node_id is not None
            else None
        ),
        "pre_registered": interpretation.pre_registered,
        "confidence": claim.confidence,
        "supported_by_dataset_ids": [str(item) for item in claim.supported_by_dataset_ids],
        "supported_by_analysis_ids": [str(item) for item in claim.supported_by_analysis_ids],
        "answers_question_ids": [str(item) for item in claim.answers_question_ids],
        "created_at": claim.created_at.isoformat(),
    }
    if claim.terminal_reason:
        payload["terminal_reason"] = claim.terminal_reason
    if claim.falsification_criteria:
        payload["falsification_criteria"] = _capped_text(claim.falsification_criteria)
    if claim.verification_plan:
        payload["verification_plan"] = _capped_text(claim.verification_plan)
    if claim.refuting_outcome:
        payload["refuting_outcome"] = _capped_text(claim.refuting_outcome)
    _add_origin_context(payload, claim)
    return payload


def _compact_visualization(visualization: Visualization) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(visualization.viz_id),
        "label": visualization.caption or visualization.file_path,
        "analysis_id": str(visualization.analysis_id),
        "viz_type": visualization.viz_type,
        "file_path": visualization.file_path,
        "related_claim_ids": [str(item) for item in visualization.related_claim_ids],
        "created_at": visualization.created_at.isoformat(),
    }
    _add_origin_context(payload, visualization)
    return payload


def _compact_exploration_node(node: ExplorationNode) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(node.node_id),
        "label": node.title,
        "node_type": node.node_type.value,
        "status": node.status.value,
        "target": {
            "entity_type": node.target.entity_type.value,
            "entity_id": str(node.target.entity_id),
        },
        "alternatives_considered": [
            _capped_text(item) for item in node.alternatives_considered if item
        ],
        "invalidates_node_id": (
            str(node.invalidates_node_id) if node.invalidates_node_id else None
        ),
        "invalidates_claim_id": (
            str(node.invalidates_claim_id) if node.invalidates_claim_id else None
        ),
        "parent_node_ids": [str(item) for item in node.parent_node_ids],
        "created_at": node.created_at.isoformat(),
        "updated_at": node.updated_at.isoformat(),
    }
    for field_name in ("choice", "rationale", "hypothesis", "failure_mode", "lesson", "trigger"):
        value = getattr(node, field_name)
        if value:
            payload[field_name] = _capped_text(value)
    _add_origin_context(payload, node)
    return payload


def _compact_goal(goal: Goal) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(goal.goal_id),
        "label": goal.title,
        "goal_type": goal.goal_type.value,
        "status": goal.status.value,
        "target_date": goal.target_date.isoformat() if goal.target_date else None,
        "external_ref": goal.external_ref,
        "attributes": dict(goal.attributes),
        "links": [
            {
                "entity_type": link.target.entity_type.value,
                "entity_id": str(link.target.entity_id),
                "relation": link.relation.value,
                "link_status": link.link_status.value,
                "slot": link.slot,
            }
            for link in goal.links
        ],
        "created_at": goal.created_at.isoformat(),
    }
    _add_origin_context(payload, goal)
    return payload


def _known_aliases(
    *,
    project: Project,
    questions: list[Question],
    superseded_questions: list[Question],
    sessions: list[Session],
    datasets: list[Dataset],
    analyses: list[Analysis],
    claims: list[Claim],
    visualizations: list[Visualization],
    goals: list[Goal],
) -> list[dict[str, Any]]:
    aliases: list[dict[str, Any]] = [
        {
            "entity_type": EntityType.PROJECT.value,
            "entity_id": str(project.project_id),
            "aliases": [project.name],
        }
    ]
    aliases.extend(
        {
            "entity_type": EntityType.QUESTION.value,
            "entity_id": str(item.question_id),
            "aliases": [item.text],
        }
        for item in questions
    )
    aliases.extend(
        {
            "entity_type": EntityType.QUESTION.value,
            "entity_id": str(item.superseded_by_question_id),
            "aliases": [item.text],
            "superseded_entity_id": str(item.question_id),
            "relationship": "superseded_alias_for_replacement",
        }
        for item in superseded_questions
        if item.superseded_by_question_id is not None
    )
    aliases.extend(
        {
            "entity_type": EntityType.SESSION.value,
            "entity_id": str(item.session_id),
            "aliases": [
                f"{item.session_type.value} session {item.started_at.date().isoformat()}",
                item.link_code,
            ],
        }
        for item in sessions
    )
    aliases.extend(
        {
            "entity_type": EntityType.DATASET.value,
            "entity_id": str(item.dataset_id),
            "aliases": [item.commit_hash, f"Dataset {item.commit_hash[:12]}"],
        }
        for item in datasets
    )
    aliases.extend(
        {
            "entity_type": EntityType.ANALYSIS.value,
            "entity_id": str(item.analysis_id),
            "aliases": [item.method_hash, item.code_version],
        }
        for item in analyses
    )
    aliases.extend(
        {
            "entity_type": EntityType.CLAIM.value,
            "entity_id": str(item.claim_id),
            "aliases": [item.statement[:180]],
        }
        for item in claims
    )
    aliases.extend(
        {
            "entity_type": EntityType.VISUALIZATION.value,
            "entity_id": str(item.viz_id),
            "aliases": [item.caption or item.file_path, item.file_path],
        }
        for item in visualizations
    )
    aliases.extend(
        {
            "entity_type": EntityType.GOAL.value,
            "entity_id": str(item.goal_id),
            "aliases": [item.title],
        }
        for item in goals
    )
    return [_with_selection_reason(alias, SELECTION_REASON_ALIAS_MATCH) for alias in aliases]


def _entity_label(entity_type: EntityType, entity: EntityResult) -> str:
    if entity_type == EntityType.PROJECT:
        return entity.name
    if entity_type == EntityType.QUESTION:
        return entity.text
    if entity_type == EntityType.NOTE:
        return entity.transcribed_text or entity.raw_content or "(binary note)"
    if entity_type == EntityType.SESSION:
        return f"{entity.session_type.value} session {entity.started_at.date().isoformat()}"
    if entity_type == EntityType.DATASET:
        return f"Dataset {entity.commit_hash[:12]}"
    if entity_type == EntityType.ANALYSIS:
        return entity.method_hash
    if entity_type == EntityType.CLAIM:
        return entity.statement[:180]
    if entity_type == EntityType.VISUALIZATION:
        return entity.caption or entity.file_path
    if entity_type == EntityType.GOAL:
        return entity.title
    if entity_type == EntityType.EXPLORATION_NODE:
        return entity.title
    return str(entity_id(entity_type, entity))


def entity_id(entity_type: EntityType, entity: EntityResult) -> UUID:
    if entity_type == EntityType.PROJECT:
        return entity.project_id
    if entity_type == EntityType.QUESTION:
        return entity.question_id
    if entity_type == EntityType.NOTE:
        return entity.note_id
    if entity_type == EntityType.SESSION:
        return entity.session_id
    if entity_type == EntityType.DATASET:
        return entity.dataset_id
    if entity_type == EntityType.ANALYSIS:
        return entity.analysis_id
    if entity_type == EntityType.CLAIM:
        return entity.claim_id
    if entity_type == EntityType.VISUALIZATION:
        return entity.viz_id
    if entity_type == EntityType.GOAL:
        return entity.goal_id
    if entity_type == EntityType.EXPLORATION_NODE:
        return entity.node_id
    raise ValidationError("Unsupported entity type.")
