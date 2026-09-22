"""Delete paths refuse to strand records that reference the deleted entity.

Every guard here is driven by ``lab_tracker.reference_registry``; these tests
pin the observable contract for each referrer family (FK cascades, SET NULL
pointers and FK-less GUID/JSON references alike).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from api_helpers import repository_backed_api
from sqlalchemy import update

from lab_tracker.auth import AuthContext, Role
from lab_tracker.db_models import DatasetModel
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    ClaimRelation,
    ClaimStatus,
    DatasetCommitManifestInput,
    DatasetFile,
    DatasetStatus,
    EntityRef,
    EntityType,
    ExplorationNodeStatus,
    ExplorationNodeType,
    ProvenanceLinkStatus,
    QuestionStatus,
    QuestionType,
    SessionType,
)


def _actor() -> AuthContext:
    return AuthContext(user_id=uuid4(), role=Role.ADMIN)


class _Context:
    def __init__(self) -> None:
        self.api = repository_backed_api()
        self.actor = _actor()
        self.project = self.api.create_project("Deletion guards", actor=self.actor)
        self.question = self.api.create_question(
            project_id=self.project.project_id,
            text="Which records must survive deletion?",
            question_type=QuestionType.DESCRIPTIVE,
            status=QuestionStatus.ACTIVE,
            actor=self.actor,
        )

    @property
    def project_id(self) -> UUID:
        return self.project.project_id

    def question_(self, text: str = "Another question") -> UUID:
        return self.api.create_question(
            project_id=self.project_id,
            text=text,
            question_type=QuestionType.DESCRIPTIVE,
            status=QuestionStatus.ACTIVE,
            actor=self.actor,
        ).question_id

    def dataset(
        self,
        *,
        status: DatasetStatus = DatasetStatus.STAGED,
        note_ids: list[UUID] | None = None,
        source_session_id: UUID | None = None,
    ) -> UUID:
        return self.api.create_dataset(
            project_id=self.project_id,
            primary_question_id=self.question.question_id,
            status=status,
            commit_manifest=DatasetCommitManifestInput(
                files=[DatasetFile(path="data.csv", checksum="abc123")],
                note_ids=note_ids or [],
                source_session_id=source_session_id,
            ),
            actor=self.actor,
        ).dataset_id

    def session(self, session_type: SessionType = SessionType.OPERATIONAL) -> UUID:
        return self.api.create_session(
            project_id=self.project_id,
            session_type=session_type,
            actor=self.actor,
        ).session_id

    def note(self, *targets: tuple[EntityType, UUID]) -> UUID:
        return self.api.create_note(
            project_id=self.project_id,
            raw_content="Observation that points at other records.",
            targets=[
                EntityRef(entity_type=entity_type, entity_id=entity_id)
                for entity_type, entity_id in targets
            ],
            actor=self.actor,
        ).note_id

    def analysis(self, dataset_id: UUID) -> UUID:
        return self.api.create_analysis(
            project_id=self.project_id,
            dataset_ids=[dataset_id],
            method_hash="method-1",
            code_version="v1",
            actor=self.actor,
        ).analysis_id

    def claim(self, **kwargs) -> UUID:
        return self.api.create_claim(
            project_id=self.project_id,
            statement=kwargs.pop("statement", "A claim under test."),
            confidence=kwargs.pop("confidence", 0.5),
            actor=self.actor,
            **kwargs,
        ).claim_id

    def visualization(self, analysis_id: UUID, **kwargs) -> UUID:
        return self.api.create_visualization(
            analysis_id=analysis_id,
            viz_type="line",
            file_path="/tmp/figure.png",
            actor=self.actor,
            **kwargs,
        ).viz_id

    def decision_node(self, target: EntityRef | None = None, **kwargs):
        return self.api.create_exploration_node(
            project_id=self.project_id,
            node_type=ExplorationNodeType.DECISION,
            title=kwargs.pop("title", "Decision"),
            target=target
            or EntityRef(entity_type=EntityType.QUESTION, entity_id=self.question.question_id),
            choice="Use the mixed model path",
            alternatives_considered=["Bootstrap"],
            rationale="The model represents the acquisition structure.",
            actor=self.actor,
            **kwargs,
        )

    def pivot_node(self, **invalidates):
        return self.api.create_exploration_node(
            project_id=self.project_id,
            node_type=ExplorationNodeType.PIVOT,
            title="Pivot",
            target=EntityRef(entity_type=EntityType.QUESTION, entity_id=self.question.question_id),
            trigger="A later run changed the interpretation.",
            rationale="Recording why the path changed.",
            actor=self.actor,
            **invalidates,
        )


# --- M55: note targets (FK-less GUIDs) must not dangle -----------------------


@pytest.mark.parametrize(
    ("entity_type", "label"),
    [
        (EntityType.DATASET, "Dataset"),
        (EntityType.SESSION, "Session"),
        (EntityType.ANALYSIS, "Analysis"),
        (EntityType.CLAIM, "Claim"),
        (EntityType.VISUALIZATION, "Visualization"),
        (EntityType.NOTE, "Note"),
    ],
)
def test_delete_refuses_while_notes_target_the_entity(
    entity_type: EntityType,
    label: str,
) -> None:
    ctx = _Context()
    api, actor = ctx.api, ctx.actor
    dataset_id = ctx.dataset()
    entity_ids = {
        EntityType.DATASET: lambda: dataset_id,
        EntityType.SESSION: ctx.session,
        EntityType.ANALYSIS: lambda: ctx.analysis(dataset_id),
        EntityType.CLAIM: ctx.claim,
        EntityType.VISUALIZATION: lambda: ctx.visualization(ctx.analysis(dataset_id)),
        EntityType.NOTE: ctx.note,
    }
    entity_id = entity_ids[entity_type]()
    note_id = ctx.note((entity_type, entity_id))
    delete = {
        EntityType.DATASET: api.delete_dataset,
        EntityType.SESSION: api.delete_session,
        EntityType.ANALYSIS: api.delete_analysis,
        EntityType.CLAIM: api.delete_claim,
        EntityType.VISUALIZATION: api.delete_visualization,
        EntityType.NOTE: api.delete_note,
    }[entity_type]

    with pytest.raises(
        ValidationError,
        match=f"^{label} cannot be deleted while notes target it\\.$",
    ):
        delete(entity_id, actor=actor)

    note = api.get_note(note_id)
    assert [target.entity_id for target in note.targets] == [entity_id]

    api.update_note(note_id, targets=[], actor=actor)
    delete(entity_id, actor=actor)


def test_delete_analysis_refuses_while_notes_target_its_visualizations() -> None:
    ctx = _Context()
    analysis_id = ctx.analysis(ctx.dataset())
    viz_id = ctx.visualization(analysis_id)
    ctx.note((EntityType.VISUALIZATION, viz_id))

    with pytest.raises(
        ValidationError,
        match="^Analysis cannot be deleted while notes target its visualizations\\.$",
    ):
        ctx.api.delete_analysis(analysis_id, actor=ctx.actor)
    assert ctx.api.get_visualization(viz_id).viz_id == viz_id


def test_blocked_message_names_every_referrer() -> None:
    ctx = _Context()
    dataset_id = ctx.dataset()
    ctx.claim(supported_by_dataset_ids=[dataset_id])
    ctx.note((EntityType.DATASET, dataset_id))

    with pytest.raises(ValidationError) as excinfo:
        ctx.api.delete_dataset(dataset_id, actor=ctx.actor)

    assert str(excinfo.value) == (
        "Dataset cannot be deleted while claims reference it; notes target it."
    )


# --- M68: session deletion ----------------------------------------------------


def test_delete_session_refuses_while_staged_dataset_manifest_references_it() -> None:
    ctx = _Context()
    session_id = ctx.session()
    dataset_id = ctx.dataset(source_session_id=session_id)

    with pytest.raises(
        ValidationError,
        match="^Session cannot be deleted while staged datasets reference it\\.$",
    ):
        ctx.api.delete_session(session_id, actor=ctx.actor)

    committed = ctx.api.update_dataset(
        dataset_id,
        status=DatasetStatus.COMMITTED,
        actor=ctx.actor,
    )
    assert committed.commit_manifest.source_session_id == session_id


def test_delete_session_refuses_while_acquisition_collections_exist() -> None:
    ctx = _Context()
    session_id = ctx.session()
    ctx.api.capture_collection_snapshot(
        session_id=session_id,
        collection_key="trials",
        client_capture_id="capture-1",
        observed_at=datetime(2026, 7, 24, 12, tzinfo=timezone.utc),
        complete=True,
        schema_version=1,
        members=[{"path": "trial/data.bin", "checksum": "a" * 64, "size_bytes": 1}],
        actor=ctx.actor,
    )

    with pytest.raises(
        ValidationError,
        match="^Session cannot be deleted while acquisition collections capture it\\.$",
    ):
        ctx.api.delete_session(session_id, actor=ctx.actor)
    collections, total = ctx.api.list_acquisition_collections(
        session_id=session_id,
        limit=10,
        offset=0,
        actor=ctx.actor,
    )
    assert total == 1 and len(collections) == 1


# --- M63: note deletion -------------------------------------------------------


@pytest.mark.parametrize("status", [DatasetStatus.STAGED, DatasetStatus.COMMITTED])
def test_delete_note_refuses_while_dataset_manifests_cite_it(status: DatasetStatus) -> None:
    ctx = _Context()
    note_id = ctx.note()
    dataset_id = ctx.dataset(status=status, note_ids=[note_id])

    with pytest.raises(
        ValidationError,
        match="^Note cannot be deleted while dataset manifests cite it\\.$",
    ):
        ctx.api.delete_note(note_id, actor=ctx.actor)
    assert ctx.api.get_dataset(dataset_id).commit_manifest.note_ids == [note_id]
    assert ctx.api.get_note(note_id).note_id == note_id


def test_delete_note_refuses_while_another_projects_dataset_manifest_cites_it() -> None:
    # Writes now reject cross-project manifest note_ids, but manifests committed
    # before that check can still cite another project's note; the guard must
    # see citations from any project's (immutable) manifest.
    ctx = _Context()
    note_id = ctx.note()
    other_project = ctx.api.create_project("Citing project", actor=ctx.actor)
    other_question = ctx.api.create_question(
        project_id=other_project.project_id,
        text="Which note does this manifest cite?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=ctx.actor,
    )
    dataset_id = ctx.api.create_dataset(
        project_id=other_project.project_id,
        primary_question_id=other_question.question_id,
        status=DatasetStatus.COMMITTED,
        commit_manifest=DatasetCommitManifestInput(
            files=[DatasetFile(path="data.csv", checksum="abc123")],
        ),
        actor=ctx.actor,
    ).dataset_id
    _engine, session = ctx.api._test_resources
    session.execute(
        update(DatasetModel)
        .where(DatasetModel.dataset_id == dataset_id)
        .values(manifest_note_ids=[str(note_id)])
    )
    session.commit()

    with pytest.raises(
        ValidationError,
        match="^Note cannot be deleted while dataset manifests cite it\\.$",
    ):
        ctx.api.delete_note(note_id, actor=ctx.actor)
    assert ctx.api.get_dataset(dataset_id).commit_manifest.note_ids == [note_id]


def test_delete_note_refuses_while_exploration_nodes_cite_it_as_evidence() -> None:
    ctx = _Context()
    note_id = ctx.note()
    ctx.decision_node(evidence_refs=[EntityRef(entity_type=EntityType.NOTE, entity_id=note_id)])

    with pytest.raises(
        ValidationError,
        match="^Note cannot be deleted while exploration nodes cite it as evidence\\.$",
    ):
        ctx.api.delete_note(note_id, actor=ctx.actor)


# --- M67: question refactors and supersession ---------------------------------


def test_delete_question_refuses_refactor_replacement_and_source() -> None:
    ctx = _Context()
    result = ctx.api.refactor_question(
        ctx.question.question_id,
        replacement_text="Which contrast is testable this week?",
        replacement_question_type=QuestionType.HYPOTHESIS_DRIVEN,
        replacement_status=QuestionStatus.ACTIVE,
        reason="Make the question testable.",
        actor=ctx.actor,
    )
    source_id = result.source_question.question_id
    replacement_id = result.replacement_question.question_id

    for question_id in (replacement_id, source_id):
        with pytest.raises(ValidationError, match="question refactors record it") as excinfo:
            ctx.api.delete_question(question_id, actor=ctx.actor)
        assert "supersession links" in str(excinfo.value)

    assert len(ctx.api.list_question_refactors(source_id)) == 1
    source = ctx.api.get_question(source_id)
    assert source.status == QuestionStatus.SUPERSEDED
    assert source.superseded_by_question_id == replacement_id


def test_delete_question_refuses_while_child_questions_list_it_as_parent() -> None:
    ctx = _Context()
    parent_id = ctx.question_("Parent question")
    ctx.api.create_question(
        project_id=ctx.project_id,
        text="Child question",
        question_type=QuestionType.DESCRIPTIVE,
        parent_question_ids=[parent_id],
        actor=ctx.actor,
    )

    with pytest.raises(
        ValidationError,
        match="^Question cannot be deleted while child questions list it as a parent\\.$",
    ):
        ctx.api.delete_question(parent_id, actor=ctx.actor)


def test_delete_question_refuses_while_exploration_nodes_target_it() -> None:
    ctx = _Context()
    question_id = ctx.question_("Explored question")
    ctx.decision_node(EntityRef(entity_type=EntityType.QUESTION, entity_id=question_id))

    with pytest.raises(
        ValidationError,
        match="^Question cannot be deleted while exploration nodes target it\\.$",
    ):
        ctx.api.delete_question(question_id, actor=ctx.actor)


# --- M51: claims --------------------------------------------------------------


@pytest.mark.parametrize("status", [ClaimStatus.SUPPORTED, ClaimStatus.REJECTED])
def test_delete_claim_refuses_non_proposed_claims(status: ClaimStatus) -> None:
    ctx = _Context()
    dataset_id = ctx.dataset(status=DatasetStatus.COMMITTED)
    claim_id = ctx.claim(
        status=status,
        supported_by_dataset_ids=[dataset_id],
        terminal_reason="Rejected after review." if status == ClaimStatus.REJECTED else None,
    )

    with pytest.raises(ValidationError, match="^Only proposed claims can be deleted"):
        ctx.api.delete_claim(claim_id, actor=ctx.actor)
    assert ctx.api.get_claim(claim_id).supported_by_dataset_ids == [dataset_id]


def test_delete_claim_refuses_incoming_edges_but_owns_outgoing_edges() -> None:
    ctx = _Context()
    target_id = ctx.claim(statement="Target claim")
    source_id = ctx.claim(statement="Source claim")
    ctx.api.create_claim_edge(
        source_id,
        target_claim_id=target_id,
        relation=ClaimRelation.EXTENDS,
        actor=ctx.actor,
    )

    with pytest.raises(
        ValidationError,
        match="^Claim cannot be deleted while claim edges point to it\\.$",
    ):
        ctx.api.delete_claim(target_id, actor=ctx.actor)

    ctx.api.delete_claim(source_id, actor=ctx.actor)
    assert ctx.api.list_claim_edges(project_id=ctx.project_id) == []
    ctx.api.delete_claim(target_id, actor=ctx.actor)


def test_delete_claim_refuses_visualization_and_exploration_referrers() -> None:
    ctx = _Context()
    claim_id = ctx.claim()
    analysis_id = ctx.analysis(ctx.dataset())
    ctx.visualization(analysis_id, related_claim_ids=[claim_id])
    ctx.decision_node(EntityRef(entity_type=EntityType.CLAIM, entity_id=claim_id))
    pivot = ctx.pivot_node(invalidates_claim_id=claim_id)
    ctx.api.update_exploration_node(
        pivot.node_id,
        status=ExplorationNodeStatus.COMMITTED,
        actor=ctx.actor,
    )

    with pytest.raises(ValidationError) as excinfo:
        ctx.api.delete_claim(claim_id, actor=ctx.actor)

    assert str(excinfo.value) == (
        "Claim cannot be deleted while visualizations reference it; "
        "exploration nodes target it; exploration pivots invalidate it."
    )
    assert ctx.api.get_exploration_node(pivot.node_id).invalidates_claim_id == claim_id


# --- M57: exploration nodes ---------------------------------------------------


def test_delete_exploration_node_refuses_committed_pivot_invalidation_and_children() -> None:
    ctx = _Context()
    decision = ctx.decision_node(title="Old path")
    pivot = ctx.pivot_node(
        invalidates_node_id=decision.node_id,
        parent_node_ids=[decision.node_id],
    )
    ctx.api.update_exploration_node(
        pivot.node_id,
        status=ExplorationNodeStatus.COMMITTED,
        actor=ctx.actor,
    )

    with pytest.raises(ValidationError) as excinfo:
        ctx.api.delete_exploration_node(decision.node_id, actor=ctx.actor)

    assert str(excinfo.value) == (
        "Exploration node cannot be deleted while exploration nodes depend on it; "
        "exploration pivots invalidate it."
    )
    reloaded = ctx.api.get_exploration_node(pivot.node_id)
    assert reloaded.status == ExplorationNodeStatus.COMMITTED
    assert reloaded.invalidates_node_id == decision.node_id
    assert reloaded.parent_node_ids == [decision.node_id]


def test_delete_exploration_node_removes_leaf_and_its_own_parent_edges() -> None:
    ctx = _Context()
    root = ctx.decision_node(title="Root")
    leaf = ctx.decision_node(title="Leaf", parent_node_ids=[root.node_id])

    deleted = ctx.api.delete_exploration_node(leaf.node_id, actor=ctx.actor)

    assert deleted.node_id == leaf.node_id
    ctx.api.delete_exploration_node(root.node_id, actor=ctx.actor)
    assert ctx.api.list_exploration_nodes(project_id=ctx.project_id) == []


# --- CLEANUP referrers ----------------------------------------------------------


def _duplicate_notes_with_proposed_link(ctx: _Context) -> tuple[UUID, UUID, UUID]:
    notes = [
        ctx.api.create_note(
            project_id=ctx.project_id,
            raw_content=f"Duplicate capture {index}",
            metadata={"evidence_content_hash": "sha256:" + "d" * 64},
            actor=ctx.actor,
        ).note_id
        for index in range(2)
    ]
    assert ctx.api.provenance_links.propose_links_from_content_hash(
        ctx.project_id,
        actor=ctx.actor,
    ) == 1
    (link,) = ctx.api.list_provenance_links(project_id=ctx.project_id)
    return notes[0], notes[1], link.link_id


def test_delete_note_removes_its_unaccepted_provenance_proposals() -> None:
    ctx = _Context()
    _antecedent_id, derived_id, _link_id = _duplicate_notes_with_proposed_link(ctx)

    ctx.api.delete_note(derived_id, actor=ctx.actor)

    assert ctx.api.list_provenance_links(project_id=ctx.project_id) == []


def test_delete_note_refuses_while_accepted_provenance_links_reference_it() -> None:
    ctx = _Context()
    antecedent_id, derived_id, link_id = _duplicate_notes_with_proposed_link(ctx)
    ctx.api.update_provenance_link_status(
        link_id,
        ProvenanceLinkStatus.ACCEPTED,
        actor=ctx.actor,
    )

    for note_id in (antecedent_id, derived_id):
        with pytest.raises(
            ValidationError,
            match="^Note cannot be deleted while accepted provenance links reference it\\.$",
        ):
            ctx.api.delete_note(note_id, actor=ctx.actor)
    assert [link.link_id for link in ctx.api.list_provenance_links(project_id=ctx.project_id)] == [
        link_id
    ]

