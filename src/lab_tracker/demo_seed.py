"""Reusable local-development demo data seed."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from uuid import UUID

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import LOCAL_AUTH_USER_ID, AuthContext, Role
from lab_tracker.models import (
    AnalysisStatus,
    ClaimStatus,
    DatasetCommitManifestInput,
    DatasetFile,
    DatasetStatus,
    EntityRef,
    EntityType,
    ExternalArtifactReference,
    NoteStatus,
    QuestionStatus,
    QuestionType,
    SessionType,
)

DEMO_PROJECT_NAME = "Demo: Cortical Decision Dynamics"


@dataclass(frozen=True)
class DemoSeedResult:
    created: bool
    project_id: UUID
    project_name: str
    question_count: int
    dataset_count: int
    note_count: int
    analysis_count: int
    claim_count: int
    visualization_count: int

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["project_id"] = str(self.project_id)
        return payload


def seed_demo_data(
    api: LabTrackerAPI,
    *,
    actor: AuthContext | None = None,
    allow_duplicates: bool = False,
) -> DemoSeedResult:
    """Populate a small, inspectable demo project through the public API facade."""

    resolved_actor = actor or AuthContext(user_id=LOCAL_AUTH_USER_ID, role=Role.ADMIN)
    if not allow_duplicates:
        existing = _find_demo_project(api)
        if existing is not None:
            return _summarize_project(api, existing.project_id, created=False)

    project = api.create_project(
        DEMO_PROJECT_NAME,
        description=(
            "A compact demo project showing how questions, sessions, datasets, "
            "analyses, claims, and visualizations fit together."
        ),
        actor=resolved_actor,
    )
    question = api.create_question(
        project_id=project.project_id,
        text="How stable is decision-related cortical activity across sessions?",
        question_type=QuestionType.HYPOTHESIS_DRIVEN,
        hypothesis=(
            "Decision-aligned activity remains stable after within-session "
            "normalization."
        ),
        status=QuestionStatus.ACTIVE,
        actor=resolved_actor,
    )
    session = api.create_session(
        project_id=project.project_id,
        session_type=SessionType.OPERATIONAL,
        actor=resolved_actor,
    )
    note = api.create_note(
        project_id=project.project_id,
        raw_content=(
            "Demo seed note: session 2026-06-19 captured decision-aligned calcium "
            "activity for a two-choice task. Use this project to explore retained "
            "Lab Tracker graph relationships."
        ),
        targets=[
            EntityRef(entity_type=EntityType.QUESTION, entity_id=question.question_id),
            EntityRef(entity_type=EntityType.SESSION, entity_id=session.session_id),
        ],
        metadata={"seed": True, "source": "lab-tracker seed-demo"},
        client_capture_id="lab-tracker-demo-seed-v1",
        status=NoteStatus.COMMITTED,
        actor=resolved_actor,
    )
    dataset = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        commit_manifest=DatasetCommitManifestInput(
            files=[
                DatasetFile(
                    path="demo/raw/session-2026-06-19/traces.csv",
                    checksum="sha256:demo-traces-v1",
                    size_bytes=4096,
                )
            ],
            external_artifacts=[
                ExternalArtifactReference(
                    source_system="demo",
                    uri="demo://cortical-decision-dynamics/session-2026-06-19",
                    content_hash="sha256:demo-session-v1",
                    metadata={"animal": "demo-mouse-01", "task": "two-choice"},
                )
            ],
            metadata={"condition": "baseline", "seed": "lab-tracker"},
            note_ids=[note.note_id],
            source_session_id=session.session_id,
        ),
        status=DatasetStatus.COMMITTED,
        actor=resolved_actor,
    )
    analysis = api.create_analysis(
        project_id=project.project_id,
        dataset_ids=[dataset.dataset_id],
        method_hash="sha256:demo-normalization-method-v1",
        code_version="demo-analysis@v1",
        environment_hash="sha256:demo-env-v1",
        status=AnalysisStatus.COMMITTED,
        actor=resolved_actor,
    )
    claim = api.create_claim(
        project_id=project.project_id,
        statement=(
            "Decision-aligned activity is stable enough in the demo dataset to "
            "support a first-pass reproducibility check."
        ),
        confidence=82.0,
        status=ClaimStatus.SUPPORTED,
        supported_by_dataset_ids=[dataset.dataset_id],
        supported_by_analysis_ids=[analysis.analysis_id],
        answers_question_ids=[question.question_id],
        actor=resolved_actor,
    )
    api.create_visualization(
        analysis_id=analysis.analysis_id,
        viz_type="line",
        file_path="demo/figures/decision-aligned-activity.png",
        caption="Mean normalized activity aligned to the choice event.",
        related_claim_ids=[claim.claim_id],
        actor=resolved_actor,
    )
    return _summarize_project(api, project.project_id, created=True)


def _find_demo_project(api: LabTrackerAPI):
    for project in api.list_projects():
        if project.name == DEMO_PROJECT_NAME:
            return project
    return None


def _summarize_project(
    api: LabTrackerAPI,
    project_id: UUID,
    *,
    created: bool,
) -> DemoSeedResult:
    project = api.get_project(project_id)
    return DemoSeedResult(
        created=created,
        project_id=project.project_id,
        project_name=project.name,
        question_count=len(api.list_questions(project_id=project_id)),
        dataset_count=len(api.list_datasets(project_id=project_id)),
        note_count=len(api.list_notes(project_id=project_id)),
        analysis_count=len(api.list_analyses(project_id=project_id)),
        claim_count=len(api.list_claims(project_id=project_id)),
        visualization_count=len(api.list_visualizations(project_id=project_id)),
    )
