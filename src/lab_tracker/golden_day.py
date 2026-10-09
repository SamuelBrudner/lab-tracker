"""Golden-day fixture: one realistic lab day, its expected batch draft, and a scorer.

One package-resident definition is shared by the pipeline test
(``tests/test_golden_day_pipeline.py``), the opt-in evaluation runner
(``scripts/eval-drafts.py``) and ``lab-tracker seed-demo --with-review`` so
all three agree on what a good batch draft looks like for this day.

The day has twelve questions (two near-duplicates of the dose-response
question and one that a refactor superseded), a closed bench session and an
open imaging session, a dead-end exploration node, a paper goal, and fifteen
staged captures: bench and imaging notes, a figure, a git commit, a meeting
note, an identifier-only capture that can only be resolved by asking, and a
grounded follow-up and an unassigned dose comparison. Three known dose captures
declare their researcher-selected question; the unassigned capture requires a
choice among the active near-duplicates. ``golden_day_expected_patch`` is the
draft a careful reviewer would want back; ``score_golden_day`` measures any
READY change set against it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from lab_tracker.auth import AuthContext
from lab_tracker.graph_drafting import GraphDraftingError
from lab_tracker.models import (
    EntityRef,
    EntityType,
    ExplorationNodeType,
    GoalType,
    GraphChangeOp,
    GraphChangeSet,
    GraphDraftSemanticType,
    Note,
    QuestionStatus,
    QuestionType,
    SessionStatus,
    SessionType,
    utc_now,
)
from lab_tracker.services.shared import MEETING_NOTE_TYPE, NOTE_TYPE_METADATA_KEY

if TYPE_CHECKING:
    from lab_tracker.api import LabTrackerAPI

GOLDEN_DAY_PROVIDER: Final = "golden-day"
GOLDEN_DAY_MODEL: Final = "scripted-v1"
GOLDEN_DAY_FIXTURE_VERSION: Final = "golden-day-v2-explicit-routing"
GOLDEN_DAY_SCORER_VERSION: Final = "golden-day-score-v5"
GOLDEN_DAY_CAPTURE_PREFIX: Final = "golden-day-v2-"
GOLDEN_DAY_IDENTIFIER_CAPTURE: Final = "M7-0925-03"
GOLDEN_DAY_CLARIFICATION: Final = "Which session and animal does capture 'M7-0925-03' refer to?"
GOLDEN_DAY_MEETING_QUESTION: Final = (
    "Does a partial agonist saturate the calcium response at a lower dose?"
)

BENCH_SESSION: Final = "bench_morning"
IMAGING_SESSION: Final = "imaging_afternoon"
SUPERSEDED_QUESTION_SLUG: Final = "old_timing"
REPLACEMENT_QUESTION_SLUG: Final = "timing_latency"
MEETING_CAPTURE_SLUG: Final = "meeting"
IDENTIFIER_CAPTURE_SLUG: Final = "identifier"
AMBIGUOUS_CAPTURE_SLUG: Final = "unassigned_dose"
DOSE_QUESTION_SLUGS: Final = ("dose_response", "dose_response_near_dup", "dose_response_near_dup2")

_LINK_CONFIDENCE: Final = 0.9
_SUGGEST_CONFIDENCE: Final = 0.7
_CLARIFY_CONFIDENCE: Final = 0.4


@dataclass(frozen=True)
class GoldenDayQuestion:
    slug: str
    text: str
    question_type: QuestionType


GOLDEN_DAY_QUESTIONS: Final[tuple[GoldenDayQuestion, ...]] = (
    GoldenDayQuestion(
        "dose_response",
        "Does the calcium response in cortical slices scale with agonist dose?",
        QuestionType.HYPOTHESIS_DRIVEN,
    ),
    GoldenDayQuestion(
        "dose_response_near_dup",
        "Is the cortical slice calcium response dose-dependent for the agonist?",
        QuestionType.DESCRIPTIVE,
    ),
    GoldenDayQuestion(
        "dose_response_near_dup2",
        "How does agonist dose change calcium response amplitude in cortical slices?",
        QuestionType.DESCRIPTIVE,
    ),
    GoldenDayQuestion(
        SUPERSEDED_QUESTION_SLUG,
        "When does the response peak after stimulation?",
        QuestionType.DESCRIPTIVE,
    ),
    GoldenDayQuestion(
        "pooling_bias",
        "Does pooling traces across animals mask per-animal dose effects?",
        QuestionType.HYPOTHESIS_DRIVEN,
    ),
    GoldenDayQuestion(
        "imaging_drift",
        "How much focal drift accumulates over a 30-minute imaging run?",
        QuestionType.DESCRIPTIVE,
    ),
    GoldenDayQuestion(
        "bleaching",
        "Does photobleaching bias late-trial response amplitudes?",
        QuestionType.HYPOTHESIS_DRIVEN,
    ),
    GoldenDayQuestion(
        "slice_health",
        "Which slice-health markers predict a usable recording?",
        QuestionType.DESCRIPTIVE,
    ),
    GoldenDayQuestion(
        "pipette_resistance",
        "Does pipette resistance drift explain within-session baseline shifts?",
        QuestionType.HYPOTHESIS_DRIVEN,
    ),
    GoldenDayQuestion(
        "bath_temperature",
        "Does bath temperature change the calcium response latency?",
        QuestionType.HYPOTHESIS_DRIVEN,
    ),
    GoldenDayQuestion(
        "analysis_pipeline",
        "Is the normalization step of the analysis pipeline stable across sessions?",
        QuestionType.METHOD_DEV,
    ),
    GoldenDayQuestion(
        "figure_reproducibility",
        "Can the dose-response figure be regenerated from the committed dataset?",
        QuestionType.METHOD_DEV,
    ),
)
GOLDEN_DAY_REPLACEMENT_QUESTION: Final = GoldenDayQuestion(
    REPLACEMENT_QUESTION_SLUG,
    "At what latency after stimulation does the calcium response peak, per animal?",
    QuestionType.DESCRIPTIVE,
)


@dataclass(frozen=True)
class GoldenDayCapture:
    """One staged capture; ``link_session`` names the session the draft should link it to."""

    slug: str
    raw_content: str
    link_session: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)
    declared_question_slug: str | None = None


GOLDEN_DAY_CAPTURES: Final[tuple[GoldenDayCapture, ...]] = (
    GoldenDayCapture(
        "bench_dose_1",
        "Bench: 1 uM agonist on slice 3 gave a clear calcium rise.",
        BENCH_SESSION,
        declared_question_slug="dose_response",
    ),
    GoldenDayCapture(
        "bench_dose_2",
        "Bench: 10 uM agonist on slice 3, larger rise than at 1 uM.",
        BENCH_SESSION,
        declared_question_slug="dose_response",
    ),
    GoldenDayCapture(
        "bench_pipette",
        "Pipette resistance drifted from 4 to 6 MOhm over the morning; baseline shifted with it.",
        BENCH_SESSION,
    ),
    GoldenDayCapture(
        "bench_temperature",
        "Bath at 32 C today; response latency looked shorter than at 30 C.",
        BENCH_SESSION,
    ),
    GoldenDayCapture(
        "bench_slice_health",
        "Slice 2 looked pale and gave no response; slice 3 was healthy.",
        BENCH_SESSION,
    ),
    GoldenDayCapture(
        "bench_timing",
        "Peak arrived roughly 400 ms after stimulation on slice 3, animal M7.",
        BENCH_SESSION,
    ),
    GoldenDayCapture(
        "imaging_drift",
        "Imaging: focal plane drifted about 2 um over 30 min; refocused twice.",
        IMAGING_SESSION,
    ),
    GoldenDayCapture(
        "imaging_bleaching",
        "Late trials were dimmer; suspect photobleaching over the run.",
        IMAGING_SESSION,
    ),
    GoldenDayCapture(
        "imaging_pooling",
        "Pooling the M7 and M8 traces flattens the dose effect; per animal it is clear.",
        IMAGING_SESSION,
    ),
    GoldenDayCapture(
        "figure",
        "Regenerated the dose-response figure from today's traces.",
        declared_question_slug="dose_response",
        metadata={
            "evidence_source_provider": "matplotlib",
            "evidence_capture_kind": "figure",
            "evidence_title": "Dose-response, slice 3, animal M7",
            "evidence_content_hash": "sha256:goldenday-fig-1",
            "evidence_source_uri": "file:///figures/dose_response.png",
        },
    ),
    GoldenDayCapture(
        "commit",
        "Committed the normalization fix to the analysis pipeline.",
        metadata={
            "evidence_source_provider": "git",
            "evidence_source_external_id": "0123abcd",
            "evidence_source_uri": "https://example.invalid/lab/analysis/commit/0123abcd",
        },
    ),
    GoldenDayCapture(
        MEETING_CAPTURE_SLUG,
        "Lab meeting: agreed to test whether a partial agonist saturates at a lower dose.",
        metadata={NOTE_TYPE_METADATA_KEY: MEETING_NOTE_TYPE},
    ),
    GoldenDayCapture(IDENTIFIER_CAPTURE_SLUG, GOLDEN_DAY_IDENTIFIER_CAPTURE),
    GoldenDayCapture(
        "followup",
        "Thought: check whether focal drift tracks bath temperature.",
    ),
    GoldenDayCapture(
        AMBIGUOUS_CAPTURE_SLUG,
        "Unassigned dose comparison: higher agonist dose gave a larger cortical-slice calcium "
        "rise. No question was selected for this capture; the three active dose-response "
        "questions remain indistinguishable for this observation.",
    ),
)

# Capture slug -> question slugs a good draft links it to. The dose-response
# near-duplicates are never targets: the canonical question is. The timing
# capture links to the replacement question, never the superseded one.
GOLDEN_DAY_EXPECTED_LINKS: Final[dict[str, frozenset[str]]] = {
    "bench_dose_1": frozenset({"dose_response"}),
    "bench_dose_2": frozenset({"dose_response"}),
    "bench_pipette": frozenset({"pipette_resistance"}),
    "bench_temperature": frozenset({"bath_temperature"}),
    "bench_slice_health": frozenset({"slice_health"}),
    "bench_timing": frozenset({REPLACEMENT_QUESTION_SLUG}),
    "imaging_drift": frozenset({"imaging_drift"}),
    "imaging_bleaching": frozenset({"bleaching"}),
    "imaging_pooling": frozenset({"pooling_bias"}),
    "figure": frozenset({"figure_reproducibility", "dose_response"}),
    "commit": frozenset({"analysis_pipeline"}),
}


@dataclass(frozen=True)
class GoldenDayGraph:
    """Identifiers of the seeded day. ``questions`` holds the twelve created slugs."""

    project_id: UUID
    questions: dict[str, UUID]
    superseded_question_id: UUID
    replacement_question_id: UUID
    sessions: dict[str, UUID]
    goal_id: UUID
    dead_end_node_id: UUID

    def question_id(self, slug: str) -> UUID:
        if slug == REPLACEMENT_QUESTION_SLUG:
            return self.replacement_question_id
        return self.questions[slug]


def question_capture_id(slug: str) -> str:
    return f"{GOLDEN_DAY_CAPTURE_PREFIX}q-{slug}"


def note_capture_id(slug: str) -> str:
    return f"{GOLDEN_DAY_CAPTURE_PREFIX}n-{slug}"


def capture_slug(note: Note) -> str:
    """Recover the capture slug from a golden-day note's client capture id."""

    prefix = f"{GOLDEN_DAY_CAPTURE_PREFIX}n-"
    if note.client_capture_id is None or not note.client_capture_id.startswith(prefix):
        raise ValueError(f"Note {note.note_id} is not a golden-day capture.")
    return note.client_capture_id.removeprefix(prefix)


def golden_day_question_texts() -> dict[str, str]:
    """Every question text the day contains, keyed by slug (replacement included)."""

    texts = {question.slug: question.text for question in GOLDEN_DAY_QUESTIONS}
    texts[REPLACEMENT_QUESTION_SLUG] = GOLDEN_DAY_REPLACEMENT_QUESTION.text
    return texts


def seed_golden_day_graph(
    api: LabTrackerAPI,
    *,
    project_id: UUID,
    actor: AuthContext,
) -> GoldenDayGraph:
    """Create the day's questions, refactor, morning session, goal and dead end."""

    questions: dict[str, UUID] = {}
    for question in GOLDEN_DAY_QUESTIONS:
        created = api.create_question(
            project_id=project_id,
            text=question.text,
            question_type=question.question_type,
            status=QuestionStatus.ACTIVE,
            client_capture_id=question_capture_id(question.slug),
            actor=actor,
        )
        questions[question.slug] = created.question_id
    refactor = api.refactor_question(
        questions[SUPERSEDED_QUESTION_SLUG],
        replacement_text=GOLDEN_DAY_REPLACEMENT_QUESTION.text,
        replacement_question_type=GOLDEN_DAY_REPLACEMENT_QUESTION.question_type,
        replacement_status=QuestionStatus.ACTIVE,
        reason="golden day supersede: per-animal latency replaces the vague timing question",
        actor=actor,
    )
    bench_session = api.create_session(
        project_id,
        SessionType.OPERATIONAL,
        actor=actor,
    )
    goal = api.create_goal(
        project_id,
        goal_type=GoalType.PAPER,
        title="Golden day paper",
        summary="Dose-response of cortical calcium signals, per animal.",
        actor=actor,
    )
    dead_end = api.create_exploration_node(
        project_id,
        node_type=ExplorationNodeType.DEAD_END,
        title="Pooling across animals hid the effect",
        target=EntityRef(entity_type=EntityType.QUESTION, entity_id=questions["dose_response"]),
        hypothesis="Pooling traces across animals would sharpen the dose-response curve.",
        failure_mode="Pooled traces averaged out an effect that is clear per animal.",
        lesson="Normalize and compare within animal before pooling.",
        actor=actor,
    )
    return GoldenDayGraph(
        project_id=project_id,
        questions=questions,
        superseded_question_id=questions[SUPERSEDED_QUESTION_SLUG],
        replacement_question_id=refactor.replacement_question.question_id,
        sessions={BENCH_SESSION: bench_session.session_id},
        goal_id=goal.goal_id,
        dead_end_node_id=dead_end.node_id,
    )


def stage_golden_day_captures(
    api: LabTrackerAPI,
    graph: GoldenDayGraph,
    *,
    actor: AuthContext,
) -> tuple[GoldenDayGraph, list[Note]]:
    """Stage the fifteen captures in order, closing the bench session midway.

    The bench captures land while the morning session is open; it is then
    closed and the imaging session opened so the afternoon captures fall in a
    different session window. Returns the graph extended with the imaging
    session id alongside the staged notes.
    """

    notes: list[Note] = []
    sessions = dict(graph.sessions)
    for capture in GOLDEN_DAY_CAPTURES:
        if capture.link_session == IMAGING_SESSION and IMAGING_SESSION not in sessions:
            api.update_session(
                sessions[BENCH_SESSION],
                status=SessionStatus.CLOSED,
                ended_at=utc_now(),
                actor=actor,
            )
            imaging_session = api.create_session(
                graph.project_id,
                SessionType.OPERATIONAL,
                actor=actor,
            )
            sessions[IMAGING_SESSION] = imaging_session.session_id
        metadata = dict(capture.metadata)
        if capture.declared_question_slug is not None:
            metadata["declared_question_id"] = str(
                graph.question_id(capture.declared_question_slug)
            )
            metadata["question_routing_basis"] = (
                "Researcher-selected question for this capture only; retain this selection "
                "rather than choosing another near-duplicate. "
                "Other captures have no implied selection."
            )
        note = api.create_note(
            project_id=graph.project_id,
            raw_content=capture.raw_content,
            metadata=metadata,
            client_capture_id=note_capture_id(capture.slug),
            actor=actor,
        )
        notes.append(note)
    if IMAGING_SESSION not in sessions:
        raise ValueError("Golden day captures never opened the imaging session.")
    return replace(graph, sessions=sessions), notes


def seed_golden_day(
    api: LabTrackerAPI,
    *,
    project_id: UUID,
    actor: AuthContext,
) -> tuple[GoldenDayGraph, list[Note]]:
    """Seed the graph and stage the captures in one call."""

    graph = seed_golden_day_graph(api, project_id=project_id, actor=actor)
    return stage_golden_day_captures(api, graph, actor=actor)


def _notes_by_slug(notes: Sequence[Note]) -> dict[str, Note]:
    return {capture_slug(note): note for note in notes}


def _question_target(question_id: UUID) -> dict[str, str]:
    return {"entity_type": EntityType.QUESTION.value, "entity_id": str(question_id)}


def _session_target(session_id: UUID) -> dict[str, str]:
    return {"entity_type": EntityType.SESSION.value, "entity_id": str(session_id)}


def _operation(
    *,
    client_ref: str | None,
    op: GraphChangeOp,
    entity_type: EntityType,
    semantic_type: GraphDraftSemanticType,
    target_entity_id: UUID | None,
    payload: dict[str, Any],
    rationale: str,
    confidence: float,
    source_note_id: UUID,
) -> dict[str, Any]:
    return {
        "client_ref": client_ref,
        "op": op.value,
        "entity_type": entity_type.value,
        "semantic_type": semantic_type.value,
        "target_entity_id": str(target_entity_id) if target_entity_id is not None else None,
        "payload_json": json.dumps(payload),
        "rationale": rationale,
        "confidence": confidence,
        "source_refs": [{"source_note_ids": [str(source_note_id)]}],
    }


def golden_day_expected_patch(graph: GoldenDayGraph, notes: Sequence[Note]) -> dict[str, Any]:
    """The batch patch a careful drafter would return for this day.

    Question links come first, then session links that carry the question
    targets forward (an update replaces a note's target list, so the later
    operation must include what the earlier one set), then two grounded new
    questions and two specific clarification requests.
    """

    by_slug = _notes_by_slug(notes)
    operations: list[dict[str, Any]] = []
    for capture in GOLDEN_DAY_CAPTURES:
        links = GOLDEN_DAY_EXPECTED_LINKS.get(capture.slug)
        if links is None:
            continue
        note = by_slug[capture.slug]
        question_targets = [
            _question_target(graph.question_id(question_slug)) for question_slug in sorted(links)
        ]
        operations.append(
            _operation(
                client_ref=None,
                op=GraphChangeOp.UPDATE,
                entity_type=EntityType.NOTE,
                semantic_type=GraphDraftSemanticType.LINK_NOTE_TO_QUESTION,
                target_entity_id=note.note_id,
                payload={"targets": question_targets},
                rationale=f"The {capture.slug.replace('_', ' ')} capture speaks to this question.",
                confidence=_LINK_CONFIDENCE,
                source_note_id=note.note_id,
            )
        )
    for capture in GOLDEN_DAY_CAPTURES:
        if capture.link_session is None:
            continue
        note = by_slug[capture.slug]
        question_targets = [
            _question_target(graph.question_id(question_slug))
            for question_slug in sorted(GOLDEN_DAY_EXPECTED_LINKS.get(capture.slug, ()))
        ]
        operations.append(
            _operation(
                client_ref=None,
                op=GraphChangeOp.UPDATE,
                entity_type=EntityType.NOTE,
                semantic_type=GraphDraftSemanticType.LINK_NOTE_TO_SESSION,
                target_entity_id=note.note_id,
                payload={
                    "targets": [
                        *question_targets,
                        _session_target(graph.sessions[capture.link_session]),
                    ]
                },
                rationale=f"Captured during the {capture.link_session.replace('_', ' ')} session.",
                confidence=_LINK_CONFIDENCE,
                source_note_id=note.note_id,
            )
        )
    meeting_note = by_slug[MEETING_CAPTURE_SLUG]
    operations.append(
        _operation(
            client_ref="meeting_followup",
            op=GraphChangeOp.CREATE,
            entity_type=EntityType.QUESTION,
            semantic_type=GraphDraftSemanticType.SUGGEST_NEW_QUESTION,
            target_entity_id=None,
            payload={
                "project_id": str(graph.project_id),
                "text": GOLDEN_DAY_MEETING_QUESTION,
                "question_type": QuestionType.HYPOTHESIS_DRIVEN.value,
                "status": QuestionStatus.STAGED.value,
            },
            rationale=(
                "The meeting agreed to test partial-agonist saturation; no question covers it."
            ),
            confidence=_SUGGEST_CONFIDENCE,
            source_note_id=meeting_note.note_id,
        )
    )
    followup_note = by_slug["followup"]
    operations.append(
        _operation(
            client_ref="temperature_drift_followup",
            op=GraphChangeOp.CREATE,
            entity_type=EntityType.QUESTION,
            semantic_type=GraphDraftSemanticType.SUGGEST_FOLLOWUP,
            target_entity_id=None,
            payload={
                "project_id": str(graph.project_id),
                "text": "Does focal drift track bath temperature?",
                "question_type": QuestionType.HYPOTHESIS_DRIVEN.value,
                "status": QuestionStatus.STAGED.value,
            },
            rationale="The thought explicitly proposes a temperature/drift check.",
            confidence=_SUGGEST_CONFIDENCE,
            source_note_id=followup_note.note_id,
        )
    )
    for note, reference in (
        (meeting_note, "meeting_followup"),
        (followup_note, "temperature_drift_followup"),
    ):
        operations.append(
            _operation(
                client_ref=None,
                op=GraphChangeOp.UPDATE,
                entity_type=EntityType.NOTE,
                semantic_type=GraphDraftSemanticType.UPDATE_ENTITY,
                target_entity_id=note.note_id,
                payload={
                    "targets": [{"entity_type": "question", "entity_id": {"$ref": reference}}]
                },
                rationale="Link the proposal to the capture that explicitly raised it.",
                confidence=_LINK_CONFIDENCE,
                source_note_id=note.note_id,
            )
        )
    identifier_note = by_slug[IDENTIFIER_CAPTURE_SLUG]
    operations.append(
        _operation(
            client_ref=None,
            op=GraphChangeOp.UPDATE,
            entity_type=EntityType.NOTE,
            semantic_type=GraphDraftSemanticType.REQUEST_CLARIFICATION,
            target_entity_id=identifier_note.note_id,
            payload={"metadata": {"needs_clarification": GOLDEN_DAY_CLARIFICATION}},
            rationale="An identifier alone cannot be placed without asking.",
            confidence=_CLARIFY_CONFIDENCE,
            source_note_id=identifier_note.note_id,
        )
    )
    ambiguity_request = (
        "Which question should receive the Unassigned dose comparison capture: "
        + ", ".join(str(graph.question_id(slug)) for slug in DOSE_QUESTION_SLUGS)
        + "?"
    )
    ambiguous_note = by_slug[AMBIGUOUS_CAPTURE_SLUG]
    operations.append(
        _operation(
            client_ref=None,
            op=GraphChangeOp.UPDATE,
            entity_type=EntityType.NOTE,
            semantic_type=GraphDraftSemanticType.REQUEST_CLARIFICATION,
            target_entity_id=ambiguous_note.note_id,
            payload={"metadata": {"needs_clarification": ambiguity_request}},
            rationale="This capture has no declared routing anchor among three near-duplicates.",
            confidence=_CLARIFY_CONFIDENCE,
            source_note_id=ambiguous_note.note_id,
        )
    )
    return {
        "summary": (
            "Linked the bench and imaging captures to their questions and sessions, "
            "proposed two grounded questions, and asked about an identifier "
            "and unassigned dose capture."
        ),
        "uncertain_fields": [IDENTIFIER_CAPTURE_SLUG, AMBIGUOUS_CAPTURE_SLUG],
        "clarification_requests": [GOLDEN_DAY_CLARIFICATION, ambiguity_request],
        "operations": operations,
    }


def expected_link_pairs(graph: GoldenDayGraph, notes: Sequence[Note]) -> set[tuple[UUID, UUID]]:
    """Every (note id, question id) pair a good draft links."""

    by_slug = _notes_by_slug(notes)
    return {
        (by_slug[capture_slug].note_id, graph.question_id(question_slug))
        for capture_slug, question_slugs in GOLDEN_DAY_EXPECTED_LINKS.items()
        for question_slug in question_slugs
    }


class ScriptedGoldenDayDraftClient:
    """A ``GraphDraftClient`` that returns one fixed batch patch and never calls a provider."""

    provider = GOLDEN_DAY_PROVIDER
    model = GOLDEN_DAY_MODEL

    def __init__(self, patch: dict[str, Any]) -> None:
        self.patch = patch
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def draft_from_batch(
        self,
        *,
        batch_context: dict[str, Any],
        user_hint: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"batch_context": batch_context, "user_hint": user_hint})
        return self.patch

    def draft_from_note(
        self,
        *,
        graph_context: dict[str, Any] | None = None,
        user_hint: str | None = None,
        draft_mode: str = "graph_context",
        project_context: dict[str, Any] | None = None,
        source_artifacts: list[dict[str, Any]] | None = None,
        image_bytes: bytes | None = None,
        image_content_type: str | None = None,
        extra_images: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        raise GraphDraftingError("Golden day client only drafts batches.")

    def draft_from_analysis_evidence(
        self,
        *,
        evidence_text: str,
        project_context: dict[str, Any],
    ) -> dict[str, Any]:
        raise GraphDraftingError("Golden day client only drafts batches.")

    def transcribe_audio(
        self,
        *,
        audio_bytes: bytes,
        filename: str,
        content_type: str,
        prompt: str | None = None,
    ) -> dict[str, Any]:
        raise GraphDraftingError("Golden day client does not transcribe audio.")

    def close(self) -> None:
        self.closed = True


@dataclass(frozen=True)
class GoldenDayScore:
    provider: str
    model: str
    prompt_version: str
    link_precision: float
    link_recall: float
    duplicate_create_rate: float
    clarification_rate: float
    clarification_recall: float
    ambiguity_link_rate: float
    proposal_precision: float
    proposal_recall: float
    operation_count: int


def _normalize_question_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip().rstrip("?.!")


def _predicted_link_pairs(change_set: GraphChangeSet) -> set[tuple[UUID, UUID | str]]:
    by_note: dict[UUID, set[tuple[UUID, UUID | str]]] = {}
    for operation in change_set.operations:
        if operation.entity_type != EntityType.NOTE or operation.op != GraphChangeOp.UPDATE:
            continue
        if operation.target_entity_id is None:
            continue
        targets = operation.payload.get("targets")
        if not isinstance(targets, list):
            continue
        # targets replaces the note's full target list regardless of the semantic
        # label. A combined session/question update must count, and a later
        # session-only update must remove a question link it overwrites.
        pairs: set[tuple[UUID, UUID | str]] = set()
        for target in targets:
            if not isinstance(target, dict):
                continue
            if target.get("entity_type") != EntityType.QUESTION.value:
                continue
            raw_id = target.get("entity_id")
            try:
                question_id: UUID | str = UUID(str(raw_id))
            except ValueError:
                # Same-patch references are graded separately for grounding and
                # source placement; they are not existing-question predictions.
                question_id = "unresolved:" + json.dumps(raw_id, sort_keys=True, default=str)
            pairs.add((operation.target_entity_id, question_id))
        by_note[operation.target_entity_id] = pairs
    return set().union(*by_note.values())


def golden_day_link_diagnostics(
    change_set: GraphChangeSet, graph: GoldenDayGraph, notes: Sequence[Note]
) -> dict[str, Any]:
    """Explain link mismatches using stable synthetic capture/question slugs."""

    expected = expected_link_pairs(graph, notes)
    predicted = {pair for pair in _predicted_link_pairs(change_set) if isinstance(pair[1], UUID)}
    note_slugs = {note.note_id: capture_slug(note) for note in notes}
    question_slugs: dict[UUID | str, str] = {
        identifier: slug for slug, identifier in graph.questions.items()
    }
    question_slugs[graph.replacement_question_id] = REPLACEMENT_QUESTION_SLUG

    def describe(pairs: set[Any]) -> list[dict[str, str]]:
        return sorted(
            [
                {
                    "capture": note_slugs.get(note_id, "unknown_capture"),
                    "question": question_slugs.get(question_id, "unmatched_question"),
                }
                for note_id, question_id in pairs
            ],
            key=lambda item: (item["capture"], item["question"]),
        )

    return {
        "expected_count": len(expected),
        "predicted_count": len(predicted),
        "missing_links": describe(expected - predicted),
        "unexpected_links": describe(predicted - expected),
        "proposals": _proposal_diagnostics(change_set, notes),
        "clarifications": _clarification_diagnostics(change_set, graph, notes),
    }


def _is_duplicate_question_text(candidate: str, existing_texts: Sequence[str]) -> bool:
    normalized = _normalize_question_text(candidate)
    if not normalized:
        return False
    return any(
        normalized in _normalize_question_text(text) or _normalize_question_text(text) in normalized
        for text in existing_texts
    )


# This is a deliberately bounded synthetic rubric, not a general semantic judge.
# Required concepts accept common paraphrases; novel scientific qualifiers fail
# closed rather than treating a keyword match as evidence for an invented claim.
_PROPOSAL_CONCEPTS: Final = {
    MEETING_CAPTURE_SLUG: (r"partial", r"agonist", r"saturat\w*|plateau", r"dose|concentration"),
    "followup": (r"focal|focus", r"drift", r"bath", r"temperature"),
}
_PROPOSAL_WORDS: Final = (
    "a an the at in of to from for per and or does do is are can could will would how what whether "
    "with on by during between than compared relative lower low smaller less that this as be "
    "condition conditions intended comparison comparator reference lab meeting "
    "test check across runs sessions specified unspecified"
)
_PROPOSAL_SCIENTIFIC_WORDS: Final = {
    MEETING_CAPTURE_SLUG: (
        "dose doses concentration concentrations partial agonist agonists saturate saturates "
        "saturation saturating saturated calcium response responses cortical slice slices reach "
        "reaches maximum maximal plateau amplitude"
    ),
    "followup": (
        "focal focus plane drift bath temperature temperatures imaging track tracks tracking "
        "correlate correlates correlated correlation covary covaries covariance "
        "associate associates "
        "associated association relationship relation depend depends dependence vary varies "
        "variation change changes affect affects influence influences "
        "predict predicts predictability"
    ),
}
_PROPOSAL_SUPPORTING_CAPTURES: Final = {
    MEETING_CAPTURE_SLUG: (MEETING_CAPTURE_SLUG, "bench_dose_1", "bench_dose_2", "figure"),
    "followup": ("followup", "imaging_drift", "bench_temperature"),
}


def _proposal_diagnostics(change_set: GraphChangeSet, notes: Sequence[Note]) -> dict[str, Any]:
    by_slug = _notes_by_slug(notes)
    creates = [
        operation
        for operation in change_set.operations
        if operation.op == GraphChangeOp.CREATE and operation.entity_type == EntityType.QUESTION
    ]
    new_links = {pair for pair in _predicted_link_pairs(change_set) if isinstance(pair[1], str)}
    grounded: dict[str, str] = {}
    seen: set[str] = set()
    descriptions = []
    valid_creates = 0
    for operation in creates:
        text = str(operation.payload.get("text") or "").casefold().strip()
        words = set(re.findall(r"[a-z]+", text))
        sources = {
            str(identifier)
            for ref in operation.source_refs
            for identifier in ref.get("source_note_ids", [])
        }
        slug = next(
            (
                slug
                for slug, concepts in _PROPOSAL_CONCEPTS.items()
                if str(by_slug[slug].note_id) in sources
                and sources
                <= {
                    str(by_slug[capture].note_id) for capture in _PROPOSAL_SUPPORTING_CAPTURES[slug]
                }
                and text.endswith("?")
                and not re.search(r"\d", text)
                and words <= set((_PROPOSAL_WORDS + " " + _PROPOSAL_SCIENTIFIC_WORDS[slug]).split())
                and all(re.search(r"\b(?:" + concept + r")\b", text) for concept in concepts)
            ),
            None,
        )
        supported = slug is not None and slug not in seen
        if supported and slug is not None:
            seen.add(slug)
            valid_creates += 1
            if operation.client_ref is not None:
                key = "unresolved:" + json.dumps({"$ref": operation.client_ref}, sort_keys=True)
                grounded[key] = slug
        descriptions.append(
            {"client_ref": operation.client_ref, "capture": slug, "supported": supported}
        )
    valid_links = {
        pair
        for pair in new_links
        if pair[1] in grounded and pair[0] == by_slug[grounded[str(pair[1])]].note_id
    }
    linked_slugs = {grounded[str(reference)] for _, reference in valid_links}
    denominator = len(creates) + len(new_links)
    return {
        "precision": (valid_creates + len(valid_links)) / denominator if denominator else 0.0,
        "recall": len(linked_slugs) / len(_PROPOSAL_CONCEPTS),
        "created_count": len(creates),
        "new_link_count": len(new_links),
        "unsupported_create_count": len(creates) - valid_creates,
        "unsupported_link_count": len(new_links) - len(valid_links),
        "missing_proposals": sorted(set(_PROPOSAL_CONCEPTS) - linked_slugs),
        "created_questions": descriptions,
    }


def _clarification_diagnostics(
    change_set: GraphChangeSet, graph: GoldenDayGraph, notes: Sequence[Note]
) -> dict[str, Any]:
    by_slug = _notes_by_slug(notes)

    def requests(slug: str) -> list[str]:
        note = by_slug[slug]
        marker = (
            r"\bunassigned\b.*\b(dose|comparison)\b"
            if slug == AMBIGUOUS_CAPTURE_SLUG
            else re.escape(GOLDEN_DAY_IDENTIFIER_CAPTURE.casefold())
        )
        texts = [
            str(operation.payload.get("metadata", {}).get("needs_clarification") or "")
            for operation in change_set.operations
            if operation.entity_type == EntityType.NOTE
            and operation.op == GraphChangeOp.UPDATE
            and operation.target_entity_id == note.note_id
        ]
        texts.extend(
            text
            for text in change_set.clarification_requests
            if re.search(marker, text.casefold()) or str(note.note_id) in text
        )
        return texts

    identifier_ok = any(
        re.search(r"\b(which|what|confirm|clarify)\b", text.casefold())
        and re.search(
            r"animal|specimen|sample|recording|dataset|identif|refer|represent|mean",
            text.casefold(),
        )
        and "?" in text
        for text in requests(IDENTIFIER_CAPTURE_SLUG)
    )
    question_texts = golden_day_question_texts()
    ambiguity_ok = any(
        re.search(r"\b(which|choose|select|confirm|clarify)\b", text.casefold())
        and "question" in text.casefold()
        and "?" in text
        and all(
            str(graph.question_id(slug)) in text
            or _normalize_question_text(question_texts[slug]) in _normalize_question_text(text)
            for slug in DOSE_QUESTION_SLUGS
        )
        for text in requests(AMBIGUOUS_CAPTURE_SLUG)
    )
    ambiguous_links = {
        pair
        for pair in _predicted_link_pairs(change_set)
        if pair[0] == by_slug[AMBIGUOUS_CAPTURE_SLUG].note_id
    }
    return {
        "identifier_specific": identifier_ok,
        "dose_choice_specific": bool(ambiguity_ok),
        "recall": (int(identifier_ok) + int(bool(ambiguity_ok))) / 2,
        "ambiguity_link_rate": float(bool(ambiguous_links)),
    }


def score_golden_day(
    change_set: GraphChangeSet,
    graph: GoldenDayGraph,
    notes: Sequence[Note],
) -> GoldenDayScore:
    """Compare one READY change set for the day against the expected draft."""

    expected = expected_link_pairs(graph, notes)
    if not expected:
        raise ValueError("The golden day expects at least one note link.")
    predicted = {pair for pair in _predicted_link_pairs(change_set) if isinstance(pair[1], UUID)}
    matched = len(predicted & expected)
    existing_texts = list(golden_day_question_texts().values())
    create_texts = [
        str(operation.payload.get("text") or "")
        for operation in change_set.operations
        if operation.op == GraphChangeOp.CREATE and operation.entity_type == EntityType.QUESTION
    ]
    duplicates = sum(
        1 for text in create_texts if _is_duplicate_question_text(text, existing_texts)
    )
    clarifications = sum(
        1
        for operation in change_set.operations
        if operation.semantic_type == GraphDraftSemanticType.REQUEST_CLARIFICATION
    )
    proposals = _proposal_diagnostics(change_set, notes)
    clarification_details = _clarification_diagnostics(change_set, graph, notes)
    return GoldenDayScore(
        provider=change_set.provider,
        model=change_set.model,
        prompt_version=change_set.prompt_version,
        link_precision=matched / len(predicted) if predicted else 0.0,
        link_recall=matched / len(expected),
        duplicate_create_rate=duplicates / len(create_texts) if create_texts else 0.0,
        clarification_rate=clarifications / len(notes),
        clarification_recall=clarification_details["recall"],
        ambiguity_link_rate=clarification_details["ambiguity_link_rate"],
        proposal_precision=proposals["precision"],
        proposal_recall=proposals["recall"],
        operation_count=len(change_set.operations),
    )
