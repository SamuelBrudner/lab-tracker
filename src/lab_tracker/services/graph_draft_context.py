"""Graph draft context packet assembly."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from lab_tracker.auth import AuthContext
from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.models import (
    Analysis,
    Claim,
    Dataset,
    EntityRef,
    EntityType,
    Goal,
    GraphDraftMode,
    Note,
    NoteStatus,
    Project,
    Question,
    QuestionStatus,
    Session,
    Visualization,
)
from lab_tracker.services.analysis_service import AnalysisService
from lab_tracker.services.claim_service import ClaimService
from lab_tracker.services.dataset_service import DatasetService
from lab_tracker.services.goal_service import GoalService
from lab_tracker.services.note_service import NoteService
from lab_tracker.services.project_service import ProjectService
from lab_tracker.services.question_service import QuestionService
from lab_tracker.services.session_service import SessionService
from lab_tracker.services.shared import is_meeting_note
from lab_tracker.services.visualization_service import VisualizationService

EntityResult = (
    Project | Question | Note | Session | Dataset | Analysis | Claim | Visualization | Goal
)
_RECENT_CONTEXT_LIMIT = 10
_QUESTION_CONTEXT_LIMIT = 50
_CAPTURE_BUNDLE_LIMIT = 6
SOURCE_ARTIFACT_TEXT_LIMIT_CHARS = 64_000
_SOURCE_ARTIFACT_PREVIEW_CHARS = 1_000


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

    def build_batch_graph_context(
        self,
        notes: list[Note],
        *,
        window: tuple[Any, Any] | None = None,
        actor: AuthContext | None = None,
        batch_note_limit: int = 100,
    ) -> dict[str, Any]:
        """Assemble a context packet covering a batch of staged notes.

        The batch may span multiple projects; graph context (questions,
        recent sessions/datasets/notes/analyses/claims/visualizations,
        known_aliases) is grouped per project. The packet is the input
        the daily-batch draft generator (lab-tracker-641) consumes.

        Caller is responsible for filtering to staged notes and choosing
        the window. The batch is capped at batch_note_limit; overflow is
        reported as truncated_note_count.
        """
        truncated_note_count = max(0, len(notes) - batch_note_limit)
        # Chronological order gives the day a contractual timeline rather than
        # relying on incidental input ordering.
        batch_notes = sorted(
            notes[:batch_note_limit],
            key=lambda item: (item.created_at, str(item.note_id)),
        )

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
            active_or_staged, superseded = self._question_context(project_id)
            recent_notes = self._recent_notes_excluding(project_id, batch_ids_in_project)
            recent_sessions = self._recent_sessions(project_id)
            sessions_by_project[project_id] = recent_sessions
            recent_datasets = self._recent_datasets(project_id)
            recent_analyses = self._recent_analyses(project_id)
            recent_claims = self._recent_claims(project_id)
            recent_visualizations = self._recent_visualizations(project_id)
            recent_goals = self._recent_goals(project_id)
            project_blocks.append(
                {
                    "id": str(project.project_id),
                    "label": project.name,
                    "status": project.status.value,
                    "note_ids_in_batch": [str(n.note_id) for n in project_notes],
                    "active_or_staged_questions": [
                        _compact_question(item) for item in active_or_staged
                    ],
                    "recent_sessions": [_compact_session(item) for item in recent_sessions],
                    "recent_datasets": [_compact_dataset(item) for item in recent_datasets],
                    "recent_notes": [_compact_note(item) for item in recent_notes],
                    "recent_analyses": [_compact_analysis(item) for item in recent_analyses],
                    "recent_claims": [_compact_claim(item) for item in recent_claims],
                    "recent_visualizations": [
                        _compact_visualization(item) for item in recent_visualizations
                    ],
                    "recent_goals": [_compact_goal(item) for item in recent_goals],
                    "known_aliases": _known_aliases(
                        project=project,
                        questions=active_or_staged,
                        superseded_questions=superseded,
                        sessions=recent_sessions,
                        datasets=recent_datasets,
                        analyses=recent_analyses,
                        claims=recent_claims,
                        visualizations=recent_visualizations,
                        goals=recent_goals,
                    ),
                }
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
            "source_artifacts": [_source_artifact_packet(item) for item in batch_notes],
            "projects": project_blocks,
            "truncated_note_count": truncated_note_count,
        }
        packet["context_summary"] = _graph_batch_context_summary(packet)
        return packet

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
            except ValidationError:
                raise
            except Exception as exc:
                raise ValidationError("Source image file could not be read.") from exc
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
        bundle_notes = [
            item
            for item in self.notes.list_notes(project_id=note.project_id)
            if item.metadata.get("capture_bundle_id") == bundle_id
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
    ) -> dict[str, Any]:
        try:
            project = self.projects.get_project(note.project_id)
        except NotFoundError as exc:
            raise ValidationError(
                "Graph context cannot be built because the note project does not exist."
            ) from exc
        questions, superseded_questions = self._question_context(note.project_id)
        recent_notes = self._recent_notes_excluding(note.project_id, {note.note_id})
        recent_sessions = self._recent_sessions(note.project_id)
        recent_datasets = self._recent_datasets(note.project_id)
        recent_analyses = self._recent_analyses(note.project_id)
        recent_claims = self._recent_claims(note.project_id)
        recent_visualizations = self._recent_visualizations(note.project_id)
        recent_goals = self._recent_goals(note.project_id)
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
            "active_or_staged_questions": [_compact_question(item) for item in questions],
            "recent_sessions": [_compact_session(item) for item in recent_sessions],
            "recent_datasets": [_compact_dataset(item) for item in recent_datasets],
            "recent_notes": [_compact_note(item) for item in recent_notes],
            "recent_analyses": [_compact_analysis(item) for item in recent_analyses],
            "recent_claims": [_compact_claim(item) for item in recent_claims],
            "recent_visualizations": [
                _compact_visualization(item) for item in recent_visualizations
            ],
            "recent_goals": [_compact_goal(item) for item in recent_goals],
            "known_aliases": _known_aliases(
                project=project,
                questions=questions,
                superseded_questions=superseded_questions,
                sessions=recent_sessions,
                datasets=recent_datasets,
                analyses=recent_analyses,
                claims=recent_claims,
                visualizations=recent_visualizations,
                goals=recent_goals,
            ),
            "unresolved_recent_captures": [
                _compact_note(item)
                for item in recent_notes
                if item.raw_asset is not None
                and item.metadata.get("capture_source") == "mobile_capture"
                and item.status == NoteStatus.STAGED
            ],
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

    def _question_context(self, project_id: UUID) -> tuple[list[Question], list[Question]]:
        active_questions, _ = self.questions.repository.query_questions(
            project_id=project_id,
            status=QuestionStatus.ACTIVE.value,
            limit=_QUESTION_CONTEXT_LIMIT,
            offset=0,
            updated_first=True,
        )
        staged_questions, _ = self.questions.repository.query_questions(
            project_id=project_id,
            status=QuestionStatus.STAGED.value,
            limit=_QUESTION_CONTEXT_LIMIT,
            offset=0,
            updated_first=True,
        )
        questions = sorted(
            [*active_questions, *staged_questions],
            key=lambda question: (question.status.value, question.updated_at, question.question_id),
            reverse=True,
        )[:_QUESTION_CONTEXT_LIMIT]
        active_context_ids = {question.question_id for question in questions}
        superseded_candidates, _ = self.questions.repository.query_questions(
            project_id=project_id,
            status=QuestionStatus.SUPERSEDED.value,
            superseded_by_question_ids=active_context_ids,
            limit=_QUESTION_CONTEXT_LIMIT * 2,
            offset=0,
            updated_first=True,
        )
        return questions, superseded_candidates

    def _recent_notes_excluding(
        self,
        project_id: UUID,
        excluded_note_ids: set[UUID],
    ) -> list[Note]:
        recent_notes, _ = self.notes.repository.query_notes(
            project_id=project_id,
            limit=_RECENT_CONTEXT_LIMIT + len(excluded_note_ids),
            offset=0,
            recent_first=True,
        )
        return [
            note
            for note in recent_notes
            if note.note_id not in excluded_note_ids
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
        if artifact.get("raw_content_truncated") or artifact.get(
            "transcript_text_truncated"
        ):
            note_id = artifact.get("note_id") or "unknown"
            warnings.append(
                f"source note {note_id} exceeded the bounded provider-context limit"
            )
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
            "recent_analyses": len(context_packet.get("recent_analyses") or []),
            "recent_claims": len(context_packet.get("recent_claims") or []),
            "recent_visualizations": len(context_packet.get("recent_visualizations") or []),
            "recent_goals": len(context_packet.get("recent_goals") or []),
            "known_aliases": len(context_packet.get("known_aliases") or []),
            "unresolved_recent_captures": len(
                context_packet.get("unresolved_recent_captures") or []
            ),
        },
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
        if artifact.get("raw_content_truncated") or artifact.get(
            "transcript_text_truncated"
        ):
            note_id = artifact.get("note_id") or "unknown"
            warnings.append(
                f"source note {note_id} exceeded the bounded provider-context limit"
            )
    if not source_artifacts:
        warnings.append("no source artifacts were included")
    truncated = int(packet.get("truncated_note_count") or 0)
    if truncated:
        warnings.append(f"batch truncated; {truncated} note(s) omitted")
    projects = packet.get("projects") or []
    batch_notes = [item for item in packet.get("batch_notes") or [] if isinstance(item, dict)]
    meeting_note_count = sum(1 for item in batch_notes if item.get("is_meeting"))
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
            "recent_sessions": sum(len(p.get("recent_sessions") or []) for p in projects),
            "recent_datasets": sum(len(p.get("recent_datasets") or []) for p in projects),
            "recent_analyses": sum(len(p.get("recent_analyses") or []) for p in projects),
            "recent_claims": sum(len(p.get("recent_claims") or []) for p in projects),
            "recent_visualizations": sum(
                len(p.get("recent_visualizations") or []) for p in projects
            ),
            "recent_goals": sum(len(p.get("recent_goals") or []) for p in projects),
            "known_aliases": sum(len(p.get("known_aliases") or []) for p in projects),
        },
        "source_artifact_counts": source_artifact_counts,
        "truncated_note_count": truncated,
        "warnings": warnings,
    }


def _compact_actor(actor: AuthContext | None) -> dict[str, Any] | None:
    if actor is None:
        return None
    return {"id": str(actor.user_id), "role": actor.role.value}


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
    timestamps = [note.created_at for note in batch_notes]
    return {"since": min(timestamps).isoformat(), "until": max(timestamps).isoformat()}


def _capture_placement(note: Note, sessions: list[Session]) -> dict[str, Any]:
    """Where a capture lands in the day.

    Pre-computes the most recent session window (if any) that contains the
    note's capture time, plus the bundle it belongs to, so terse
    identifier-only captures can be placed -- or surfaced as unplaceable --
    rather than guessed.
    """

    candidates = [
        session
        for session in sessions
        if session.started_at <= note.created_at
        and (session.ended_at is None or note.created_at <= session.ended_at)
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
    if note.transcribed_text:
        transcript_text, transcript_truncated = _bounded_source_text(
            note.transcribed_text
        )
        payload["transcript_id"] = f"transcript:{note.note_id}"
        payload["transcript_text"] = transcript_text
        payload["transcript_text_char_count"] = len(note.transcribed_text)
        payload["transcript_text_truncated"] = transcript_truncated
        payload["transcript_text_limit_chars"] = SOURCE_ARTIFACT_TEXT_LIMIT_CHARS
        payload["transcript_is_derived"] = True
    if note.raw_content:
        raw_content_text, raw_content_truncated = _bounded_source_text(
            note.raw_content
        )
        # Keep the historical preview for older provider adapters while giving
        # the model the full bounded text and an explicit completeness signal.
        payload["raw_content_preview"] = raw_content_text[
            :_SOURCE_ARTIFACT_PREVIEW_CHARS
        ]
        payload["raw_content_text"] = raw_content_text
        payload["raw_content_char_count"] = len(note.raw_content)
        payload["raw_content_truncated"] = raw_content_truncated
        payload["raw_content_limit_chars"] = SOURCE_ARTIFACT_TEXT_LIMIT_CHARS
    return payload


def _bounded_source_text(value: str) -> tuple[str, bool]:
    return (
        value[:SOURCE_ARTIFACT_TEXT_LIMIT_CHARS],
        len(value) > SOURCE_ARTIFACT_TEXT_LIMIT_CHARS,
    )


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


def _compact_claim(claim: Claim) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(claim.claim_id),
        "label": claim.statement[:180],
        "status": claim.status.value,
        "confidence": claim.confidence,
        "supported_by_dataset_ids": [str(item) for item in claim.supported_by_dataset_ids],
        "supported_by_analysis_ids": [str(item) for item in claim.supported_by_analysis_ids],
        "created_at": claim.created_at.isoformat(),
    }
    if claim.terminal_reason:
        payload["terminal_reason"] = claim.terminal_reason
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
    return aliases


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
    raise ValidationError("Unsupported entity type.")
