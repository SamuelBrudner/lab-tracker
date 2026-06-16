from uuid import uuid4

import pytest
from api_helpers import repository_backed_api

from lab_tracker.auth import AuthContext, AuthService, Role
from lab_tracker.errors import AuthError, ValidationError
from lab_tracker.models import (
    AnalysisStatus,
    ClaimStatus,
    DatasetCommitManifestInput,
    DatasetFile,
    DatasetStatus,
    EntityOrigin,
    EntityRef,
    EntityType,
    ExternalArtifactReference,
    GraphChangeSet,
    GraphChangeSetStatus,
    QuestionLinkRole,
    QuestionStatus,
    QuestionType,
    SessionStatus,
    SessionType,
)


def _actor(role: Role = Role.ADMIN) -> AuthContext:
    return AuthContext(user_id=uuid4(), role=role)


def _external_artifact(
    uri: str = "s3://lab-bucket/acquisitions/run-001/manifest.json",
    content_hash: str = "sha256:manifest001",
) -> ExternalArtifactReference:
    return ExternalArtifactReference(
        source_system="s3",
        uri=uri,
        content_hash=content_hash,
        metadata={"file_count": 12, "total_size_bytes": 1024},
    )


def test_project_question_dataset_flow():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="What is the baseline distribution?",
        question_type=QuestionType.DESCRIPTIVE,
        actor=actor,
    )
    dataset = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        actor=actor,
    )
    assert dataset.primary_question_id == question.question_id
    assert any(link.role == QuestionLinkRole.PRIMARY for link in dataset.question_links)
    assert dataset.commit_hash
    assert dataset.commit_manifest.question_links == dataset.question_links


def test_direct_claim_and_visualization_writes_record_creator_and_user_origin():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Origin Project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="What should be attributed directly?",
        question_type=QuestionType.DESCRIPTIVE,
        actor=actor,
    )
    dataset = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        actor=actor,
    )
    analysis = api.create_analysis(
        project_id=project.project_id,
        dataset_ids=[dataset.dataset_id],
        method_hash="method-v1",
        code_version="code-v1",
        actor=actor,
    )

    claim = api.create_claim(
        project_id=project.project_id,
        statement="Direct claims carry their creator.",
        confidence=90,
        actor=actor,
    )
    visualization = api.create_visualization(
        analysis_id=analysis.analysis_id,
        viz_type="line",
        file_path="/tmp/figure.png",
        related_claim_ids=[claim.claim_id],
        actor=actor,
    )

    assert claim.created_by == str(actor.user_id)
    assert claim.origin == EntityOrigin.USER
    assert claim.change_set_id is None
    assert visualization.created_by == str(actor.user_id)
    assert visualization.origin == EntityOrigin.USER
    assert visualization.change_set_id is None


def test_dataset_requires_primary_question():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    with pytest.raises(ValidationError):
        api.create_dataset(
            project_id=project.project_id,
            primary_question_id=None,  # type: ignore[arg-type]
            actor=actor,
        )


def test_commit_hash_is_content_addressed():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="What is the baseline distribution?",
        question_type=QuestionType.DESCRIPTIVE,
        actor=actor,
    )
    manifest = DatasetCommitManifestInput(
        files=[DatasetFile(path="data.csv", checksum="abc123")],
        metadata={"run": "1"},
    )
    dataset = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        commit_manifest=manifest,
        actor=actor,
    )
    original_hash = dataset.commit_hash
    dataset_clone = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        commit_manifest=manifest,
        actor=actor,
    )
    assert dataset_clone.commit_hash == original_hash
    updated_manifest = DatasetCommitManifestInput(
        files=[
            DatasetFile(path="data.csv", checksum="abc123"),
            DatasetFile(path="meta.json", checksum="def456"),
        ],
        metadata={"run": "1"},
    )
    updated = api.update_dataset(
        dataset.dataset_id,
        commit_manifest=updated_manifest,
        actor=actor,
    )
    assert updated.commit_hash != original_hash


def test_external_artifacts_are_content_addressed_deterministically():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="What is the external run identity?",
        question_type=QuestionType.DESCRIPTIVE,
        actor=actor,
    )
    first_artifact = _external_artifact(
        uri="s3://lab-bucket/acquisitions/run-001/manifest.json",
        content_hash="sha256:manifest001",
    )
    second_artifact = _external_artifact(
        uri="datalad://lab/run-001?commit=abc123",
        content_hash="sha256:manifest002",
    )

    first = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        commit_manifest=DatasetCommitManifestInput(
            external_artifacts=[second_artifact, first_artifact]
        ),
        actor=actor,
    )
    reordered = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        commit_manifest=DatasetCommitManifestInput(
            external_artifacts=[first_artifact, second_artifact]
        ),
        actor=actor,
    )
    changed = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        commit_manifest=DatasetCommitManifestInput(
            external_artifacts=[
                first_artifact,
                _external_artifact(
                    uri=second_artifact.uri,
                    content_hash="sha256:changed",
                ),
            ]
        ),
        actor=actor,
    )

    assert reordered.commit_hash == first.commit_hash
    assert changed.commit_hash != first.commit_hash


def test_dataset_commit_manifest_preserves_nwb_metadata_keys():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="What is the baseline distribution?",
        question_type=QuestionType.DESCRIPTIVE,
        actor=actor,
    )
    manifest = DatasetCommitManifestInput(
        files=[DatasetFile(path="data.nwb", checksum="abc123")],
        nwb_metadata={"Identifier": "nwb-001", "Session Description": "baseline"},
    )
    dataset = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        commit_manifest=manifest,
        actor=actor,
    )
    assert dataset.commit_manifest.nwb_metadata == {
        "Identifier": "nwb-001",
        "Session Description": "baseline",
    }


def test_dataset_commit_manifest_preserves_bids_metadata_keys():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Is the signal stable?",
        question_type=QuestionType.DESCRIPTIVE,
        actor=actor,
    )
    manifest = DatasetCommitManifestInput(
        files=[DatasetFile(path="dataset_description.json", checksum="abc123")],
        bids_metadata={"Name": "Example Dataset"},
    )
    dataset = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        commit_manifest=manifest,
        actor=actor,
    )
    assert dataset.commit_manifest.bids_metadata == {"Name": "Example Dataset"}


def test_dataset_commit_manifest_does_not_split_prefixed_metadata():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Did the rig pass QA?",
        question_type=QuestionType.DESCRIPTIVE,
        actor=actor,
    )
    manifest = DatasetCommitManifestInput(
        files=[DatasetFile(path="rig.nwb", checksum="abc123")],
        metadata={
            "nwb.identifier": "rig-001",
            "nwb.session_description": "qa session",
            "nwb.session_start_time": "2024-01-02T00:00:00Z",
            "bids.Name": "Rig Dataset",
            "bids.BIDSVersion": "1.8.0",
            "run": "7",
        },
    )
    dataset = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        commit_manifest=manifest,
        actor=actor,
    )
    assert dataset.commit_manifest.metadata == {
        "nwb.identifier": "rig-001",
        "nwb.session_description": "qa session",
        "nwb.session_start_time": "2024-01-02T00:00:00Z",
        "bids.Name": "Rig Dataset",
        "bids.BIDSVersion": "1.8.0",
        "run": "7",
    }
    assert dataset.commit_manifest.nwb_metadata == {}
    assert dataset.commit_manifest.bids_metadata == {}


def test_dataset_commit_requires_active_question():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Is the signal stable?",
        question_type=QuestionType.DESCRIPTIVE,
        actor=actor,
    )
    manifest = DatasetCommitManifestInput(files=[DatasetFile(path="data.csv", checksum="abc123")])
    dataset = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        commit_manifest=manifest,
        actor=actor,
    )
    with pytest.raises(ValidationError):
        api.update_dataset(dataset.dataset_id, status=DatasetStatus.COMMITTED, actor=actor)
    api.update_question(question.question_id, status=QuestionStatus.ACTIVE, actor=actor)
    committed = api.update_dataset(dataset.dataset_id, status=DatasetStatus.COMMITTED, actor=actor)
    assert committed.status == DatasetStatus.COMMITTED


def test_dataset_commit_requires_evidence_source():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Is there any data to commit?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    dataset = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        actor=actor,
    )
    with pytest.raises(
        ValidationError,
        match="At least one file or external artifact is required to commit a dataset.",
    ):
        api.update_dataset(dataset.dataset_id, status=DatasetStatus.COMMITTED, actor=actor)


def test_dataset_commit_accepts_external_artifact_without_file_attachment():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Can large external runs be committed by reference?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )

    dataset = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        status=DatasetStatus.COMMITTED,
        commit_manifest=DatasetCommitManifestInput(external_artifacts=[_external_artifact()]),
        actor=actor,
    )

    assert dataset.status == DatasetStatus.COMMITTED
    assert dataset.commit_manifest.files == []
    assert dataset.commit_manifest.external_artifacts == [_external_artifact()]


def test_analysis_accepts_external_run_reference_and_rejects_duplicates():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Analysis Run Project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Can analysis runs be referenced externally?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    dataset = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        status=DatasetStatus.COMMITTED,
        commit_manifest=DatasetCommitManifestInput(
            files=[DatasetFile(path="data.csv", checksum="sha256:data")]
        ),
        actor=actor,
    )
    run = ExternalArtifactReference(
        source_system="mlflow",
        uri="mlflow://experiments/fly/runs/run-001",
        content_hash="sha256:run001",
        metadata={"run_name": "gain-fit"},
    )

    analysis = api.create_analysis(
        project_id=project.project_id,
        dataset_ids=[dataset.dataset_id],
        method_hash="method-1",
        code_version="git:abc123",
        external_artifacts=[run],
        actor=actor,
    )

    assert analysis.external_artifacts == [run]
    with pytest.raises(ValidationError, match="Duplicate external artifact reference"):
        api.update_analysis(
            analysis.analysis_id,
            external_artifacts=[run, run],
            actor=actor,
        )


def test_dataset_manifest_rejects_duplicate_external_artifact_reference():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Can duplicate external references be rejected?",
        question_type=QuestionType.DESCRIPTIVE,
        actor=actor,
    )
    artifact = _external_artifact()

    with pytest.raises(ValidationError, match="Duplicate external artifact reference"):
        api.create_dataset(
            project_id=project.project_id,
            primary_question_id=question.question_id,
            commit_manifest=DatasetCommitManifestInput(external_artifacts=[artifact, artifact]),
            actor=actor,
        )


@pytest.mark.parametrize(
    ("source_status", "replacement_status"),
    [
        (QuestionStatus.STAGED, QuestionStatus.STAGED),
        (QuestionStatus.ACTIVE, QuestionStatus.STAGED),
        (QuestionStatus.ACTIVE, QuestionStatus.ACTIVE),
    ],
)
def test_question_refactor_supports_staged_and_active_replacements(
    source_status: QuestionStatus,
    replacement_status: QuestionStatus,
):
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Question Refactor", actor=actor)
    source = api.create_question(
        project_id=project.project_id,
        text="How should the broad question be framed?",
        question_type=QuestionType.DESCRIPTIVE,
        status=source_status,
        actor=actor,
    )

    result = api.refactor_question(
        source.question_id,
        replacement_text="Which experimental contrast is testable this week?",
        replacement_question_type=QuestionType.HYPOTHESIS_DRIVEN,
        replacement_status=replacement_status,
        reason="Make the question testable before project launch.",
        actor=actor,
    )

    assert result.source_question.status == QuestionStatus.SUPERSEDED
    assert (
        result.source_question.superseded_by_question_id == result.replacement_question.question_id
    )
    assert result.replacement_question.status == replacement_status
    assert result.replacement_question.supersedes_question_id == source.question_id
    assert result.refactor.source_snapshot["status"] == source_status.value
    assert result.refactor.replacement_snapshot["status"] == replacement_status.value
    assert (
        api.list_question_refactors(source.question_id)[0].refactor_id
        == result.refactor.refactor_id
    )
    assert (
        api.list_question_refactors(result.replacement_question.question_id)[0].refactor_id
        == result.refactor.refactor_id
    )


def test_question_refactor_moves_only_selected_children_and_notes():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Question Refactor Moves", actor=actor)
    parent = api.create_question(
        project_id=project.project_id,
        text="What is the project frame?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    source = api.create_question(
        project_id=project.project_id,
        text="Does the original leaf question make sense?",
        question_type=QuestionType.HYPOTHESIS_DRIVEN,
        status=QuestionStatus.ACTIVE,
        parent_question_ids=[parent.question_id],
        actor=actor,
    )
    moved_child = api.create_question(
        project_id=project.project_id,
        text="Which child should move?",
        question_type=QuestionType.METHOD_DEV,
        parent_question_ids=[source.question_id],
        actor=actor,
    )
    retained_child = api.create_question(
        project_id=project.project_id,
        text="Which child remains historical?",
        question_type=QuestionType.METHOD_DEV,
        parent_question_ids=[source.question_id],
        actor=actor,
    )
    moved_note = api.create_note(
        project_id=project.project_id,
        raw_content="Move this note to the refined question.",
        targets=[EntityRef(entity_type=EntityType.QUESTION, entity_id=source.question_id)],
        actor=actor,
    )
    retained_note = api.create_note(
        project_id=project.project_id,
        raw_content="Keep this note on the historical wording.",
        targets=[EntityRef(entity_type=EntityType.QUESTION, entity_id=source.question_id)],
        actor=actor,
    )

    result = api.refactor_question(
        source.question_id,
        replacement_text="Does the refined leaf question support a direct assay?",
        replacement_question_type=QuestionType.HYPOTHESIS_DRIVEN,
        replacement_status=QuestionStatus.ACTIVE,
        reason="Separate current testable framing from historical wording.",
        child_question_ids_to_reparent=[moved_child.question_id],
        note_ids_to_retarget=[moved_note.note_id],
        actor=actor,
    )

    replacement_id = result.replacement_question.question_id
    assert result.replacement_question.parent_question_ids == [parent.question_id]
    assert api.get_question(source.question_id).superseded_by_question_id == replacement_id
    assert api.get_question(moved_child.question_id).parent_question_ids == [replacement_id]
    assert api.get_question(retained_child.question_id).parent_question_ids == [source.question_id]
    assert api.get_note(moved_note.note_id).targets == [
        EntityRef(entity_type=EntityType.QUESTION, entity_id=replacement_id)
    ]
    assert api.get_note(retained_note.note_id).targets == [
        EntityRef(entity_type=EntityType.QUESTION, entity_id=source.question_id)
    ]
    assert result.refactor.relationship_changes == {
        "child_question_ids_reparented": [str(moved_child.question_id)],
        "note_ids_retargeted": [str(moved_note.note_id)],
        "dataset_session_analysis_claim_links_moved": False,
    }


def test_question_refactor_validates_source_status_relationships_and_cycles():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Question Refactor Validation", actor=actor)
    other_project = api.create_project("Other Refactor Validation", actor=actor)
    source = api.create_question(
        project_id=project.project_id,
        text="Should this question be refactored?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    child = api.create_question(
        project_id=project.project_id,
        text="Direct child",
        question_type=QuestionType.METHOD_DEV,
        parent_question_ids=[source.question_id],
        actor=actor,
    )
    grandchild = api.create_question(
        project_id=project.project_id,
        text="Grandchild",
        question_type=QuestionType.METHOD_DEV,
        parent_question_ids=[child.question_id],
        actor=actor,
    )
    other_question = api.create_question(
        project_id=other_project.project_id,
        text="Other project question",
        question_type=QuestionType.DESCRIPTIVE,
        actor=actor,
    )
    other_note = api.create_note(
        project_id=other_project.project_id,
        raw_content="Other project note",
        actor=actor,
    )

    with pytest.raises(ValidationError, match="reason"):
        api.refactor_question(
            source.question_id,
            replacement_text="Missing reason replacement",
            replacement_question_type=QuestionType.DESCRIPTIVE,
            replacement_status=QuestionStatus.STAGED,
            reason=" ",
            actor=actor,
        )
    with pytest.raises(ValidationError, match="source as a parent"):
        api.refactor_question(
            source.question_id,
            replacement_text="Source parent replacement",
            replacement_question_type=QuestionType.DESCRIPTIVE,
            replacement_status=QuestionStatus.STAGED,
            reason="Reject source-as-parent.",
            replacement_parent_question_ids=[source.question_id],
            actor=actor,
        )
    with pytest.raises(ValidationError, match="same project"):
        api.refactor_question(
            source.question_id,
            replacement_text="Cross-project parent replacement",
            replacement_question_type=QuestionType.DESCRIPTIVE,
            replacement_status=QuestionStatus.STAGED,
            reason="Reject cross-project parent.",
            replacement_parent_question_ids=[other_question.question_id],
            actor=actor,
        )
    with pytest.raises(ValidationError, match="same project"):
        api.refactor_question(
            source.question_id,
            replacement_text="Cross-project child replacement",
            replacement_question_type=QuestionType.DESCRIPTIVE,
            replacement_status=QuestionStatus.STAGED,
            reason="Reject cross-project child move.",
            child_question_ids_to_reparent=[other_question.question_id],
            actor=actor,
        )
    with pytest.raises(ValidationError, match="same project"):
        api.refactor_question(
            source.question_id,
            replacement_text="Cross-project note replacement",
            replacement_question_type=QuestionType.DESCRIPTIVE,
            replacement_status=QuestionStatus.STAGED,
            reason="Reject cross-project note move.",
            note_ids_to_retarget=[other_note.note_id],
            actor=actor,
        )
    with pytest.raises(ValidationError, match="acyclic"):
        api.refactor_question(
            source.question_id,
            replacement_text="Cyclic replacement",
            replacement_question_type=QuestionType.DESCRIPTIVE,
            replacement_status=QuestionStatus.STAGED,
            reason="Reject cyclic child move.",
            replacement_parent_question_ids=[grandchild.question_id],
            child_question_ids_to_reparent=[child.question_id],
            actor=actor,
        )

    answered = api.create_question(
        project_id=project.project_id,
        text="Answered question",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    api.update_question(answered.question_id, status=QuestionStatus.ANSWERED, actor=actor)
    abandoned = api.create_question(
        project_id=project.project_id,
        text="Abandoned question",
        question_type=QuestionType.DESCRIPTIVE,
        actor=actor,
    )
    api.update_question(
        abandoned.question_id,
        status=QuestionStatus.ABANDONED,
        terminal_reason="Question was ruled out before refactor.",
        actor=actor,
    )
    for invalid_source in (answered, abandoned):
        with pytest.raises(ValidationError, match="staged or active"):
            api.refactor_question(
                invalid_source.question_id,
                replacement_text="Invalid source replacement",
                replacement_question_type=QuestionType.DESCRIPTIVE,
                replacement_status=QuestionStatus.STAGED,
                reason="Reject terminal source.",
                actor=actor,
            )


def test_question_status_superseded_is_terminal_and_active_to_staged_remains_disallowed():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Question Status Transitions", actor=actor)
    active = api.create_question(
        project_id=project.project_id,
        text="Active question",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    with pytest.raises(ValidationError, match="active to staged"):
        api.update_question(active.question_id, status=QuestionStatus.STAGED, actor=actor)

    result = api.refactor_question(
        active.question_id,
        replacement_text="Replacement question",
        replacement_question_type=QuestionType.DESCRIPTIVE,
        replacement_status=QuestionStatus.ACTIVE,
        reason="Retire the old wording.",
        actor=actor,
    )
    with pytest.raises(ValidationError, match="superseded to active"):
        api.update_question(
            result.source_question.question_id,
            status=QuestionStatus.ACTIVE,
            actor=actor,
        )


def test_terminal_status_transitions_require_and_persist_reason():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Terminal transition lessons", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Does the effect survive the control?",
        question_type=QuestionType.HYPOTHESIS_DRIVEN,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    with pytest.raises(ValidationError, match="terminal_reason is required"):
        api.update_question(question.question_id, status=QuestionStatus.ABANDONED, actor=actor)
    abandoned = api.update_question(
        question.question_id,
        status=QuestionStatus.ABANDONED,
        terminal_reason="  Control erased the effect.  ",
        actor=actor,
    )
    assert abandoned.terminal_reason == "Control erased the effect."

    data_question = api.create_question(
        project_id=project.project_id,
        text="Dataset-bearing question",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    manifest = DatasetCommitManifestInput(files=[DatasetFile(path="data.csv", checksum="abc123")])
    dataset = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=data_question.question_id,
        commit_manifest=manifest,
        status=DatasetStatus.COMMITTED,
        actor=actor,
    )
    analysis = api.create_analysis(
        project_id=project.project_id,
        dataset_ids=[dataset.dataset_id],
        method_hash="method-1",
        code_version="v1",
        status=AnalysisStatus.COMMITTED,
        actor=actor,
    )
    with pytest.raises(ValidationError, match="terminal_reason is required"):
        api.update_analysis(analysis.analysis_id, status=AnalysisStatus.ARCHIVED, actor=actor)
    archived_analysis = api.update_analysis(
        analysis.analysis_id,
        status=AnalysisStatus.ARCHIVED,
        terminal_reason="Pipeline parameters were invalid.",
        actor=actor,
    )
    assert archived_analysis.terminal_reason == "Pipeline parameters were invalid."

    with pytest.raises(ValidationError, match="terminal_reason is required"):
        api.update_dataset(dataset.dataset_id, status=DatasetStatus.ARCHIVED, actor=actor)
    archived_dataset = api.update_dataset(
        dataset.dataset_id,
        status=DatasetStatus.ARCHIVED,
        terminal_reason="Raw acquisition was corrupted.",
        actor=actor,
    )
    assert archived_dataset.terminal_reason == "Raw acquisition was corrupted."

    claim = api.create_claim(
        project_id=project.project_id,
        statement="The control had no effect.",
        confidence=0.7,
        actor=actor,
    )
    with pytest.raises(ValidationError, match="terminal_reason is required"):
        api.update_claim(claim.claim_id, status=ClaimStatus.REJECTED, actor=actor)
    rejected = api.update_claim(
        claim.claim_id,
        status=ClaimStatus.REJECTED,
        terminal_reason="New control analysis refuted the interpretation.",
        actor=actor,
    )
    assert rejected.terminal_reason == "New control analysis refuted the interpretation."


def test_terminal_reason_is_rejected_for_non_terminal_statuses():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Terminal reason guard", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Is the field scoped to dead ends?",
        question_type=QuestionType.DESCRIPTIVE,
        actor=actor,
    )

    with pytest.raises(ValidationError, match="can only be set"):
        api.update_question(
            question.question_id,
            status=QuestionStatus.ACTIVE,
            terminal_reason="Not a dead end.",
            actor=actor,
        )


def test_terminal_status_creation_requires_reason():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Terminal creation lessons", actor=actor)

    with pytest.raises(ValidationError, match="terminal_reason is required"):
        api.create_question(
            project_id=project.project_id,
            text="Known dead end",
            question_type=QuestionType.DESCRIPTIVE,
            status=QuestionStatus.ABANDONED,
            actor=actor,
        )
    created = api.create_question(
        project_id=project.project_id,
        text="Known dead end with lesson",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ABANDONED,
        terminal_reason="Prior data already invalidated it.",
        actor=actor,
    )
    assert created.terminal_reason == "Prior data already invalidated it."


def test_session_creation_starts_active_without_end_time():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)

    session = api.create_session(
        project_id=project.project_id,
        session_type=SessionType.OPERATIONAL,
        actor=actor,
    )

    assert session.status == SessionStatus.ACTIVE
    assert session.ended_at is None


def test_committed_dataset_is_immutable():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Does activity drift?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    manifest = DatasetCommitManifestInput(files=[DatasetFile(path="data.csv", checksum="abc123")])
    dataset = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        commit_manifest=manifest,
        actor=actor,
    )
    api.update_dataset(dataset.dataset_id, status=DatasetStatus.COMMITTED, actor=actor)
    with pytest.raises(ValidationError):
        api.update_dataset(
            dataset.dataset_id,
            commit_manifest=DatasetCommitManifestInput(
                files=[DatasetFile(path="data.csv", checksum="abc123")],
                metadata={"extra": "1"},
            ),
            actor=actor,
        )
    with pytest.raises(ValidationError):
        api.update_dataset(
            dataset.dataset_id,
            question_links=dataset.question_links,
            actor=actor,
        )
    with pytest.raises(ValidationError):
        api.update_dataset(dataset.dataset_id, commit_hash="deadbeef", actor=actor)
    archived = api.update_dataset(
        dataset.dataset_id,
        status=DatasetStatus.ARCHIVED,
        terminal_reason="Archive immutable dataset for transition coverage.",
        actor=actor,
    )
    assert archived.status == DatasetStatus.ARCHIVED


def test_delete_question_rejects_primary_dataset_and_session_references():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Question delete guards", actor=actor)
    dataset_question = api.create_question(
        project_id=project.project_id,
        text="Primary dataset question",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    api.create_dataset(
        project_id=project.project_id,
        primary_question_id=dataset_question.question_id,
        actor=actor,
    )

    with pytest.raises(ValidationError, match="datasets use"):
        api.delete_question(dataset_question.question_id, actor=actor)

    session_question = api.create_question(
        project_id=project.project_id,
        text="Primary session question",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    api.create_session(
        project_id=project.project_id,
        session_type=SessionType.SCIENTIFIC,
        primary_question_id=session_question.question_id,
        actor=actor,
    )

    with pytest.raises(ValidationError, match="sessions use"):
        api.delete_question(session_question.question_id, actor=actor)


def test_delete_question_rejects_secondary_dataset_links():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Question secondary delete guards", actor=actor)
    primary_question = api.create_question(
        project_id=project.project_id,
        text="Primary committed dataset question",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    secondary_question = api.create_question(
        project_id=project.project_id,
        text="Secondary committed dataset question",
        question_type=QuestionType.DESCRIPTIVE,
        actor=actor,
    )
    api.create_dataset(
        project_id=project.project_id,
        primary_question_id=primary_question.question_id,
        secondary_question_ids=[secondary_question.question_id],
        status=DatasetStatus.COMMITTED,
        commit_manifest=DatasetCommitManifestInput(
            files=[DatasetFile(path="data.csv", checksum="abc123")]
        ),
        actor=actor,
    )

    with pytest.raises(ValidationError, match="datasets link"):
        api.delete_question(secondary_question.question_id, actor=actor)


def test_delete_question_rejects_claim_answer_links():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Question claim delete guards", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Question answered by a claim",
        question_type=QuestionType.DESCRIPTIVE,
        actor=actor,
    )
    api.create_claim(
        project_id=project.project_id,
        statement="This claim answers the question.",
        confidence=0.8,
        answers_question_ids=[question.question_id],
        actor=actor,
    )

    with pytest.raises(ValidationError, match="claims answer"):
        api.delete_question(question.question_id, actor=actor)


def test_delete_note_rejects_graph_draft_references():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Note delete graph guard", actor=actor)
    note = api.create_note(
        project_id=project.project_id,
        raw_content="Raw observation for graph review.",
        actor=actor,
    )
    api.graph_drafts._save_graph_change_set(  # noqa: SLF001
        GraphChangeSet(
            change_set_id=uuid4(),
            project_id=project.project_id,
            source_note_id=note.note_id,
            source_note_ids=[note.note_id],
            provider="test",
            model="test-model",
            prompt_version="test-prompt",
            status=GraphChangeSetStatus.COMMITTED,
        )
    )

    with pytest.raises(ValidationError, match="graph drafts reference"):
        api.delete_note(note.note_id, actor=actor)


def test_delete_dataset_rejects_committed_and_referenced_datasets():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Dataset delete guards", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Which datasets should remain durable?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    committed = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        status=DatasetStatus.COMMITTED,
        commit_manifest=DatasetCommitManifestInput(
            files=[DatasetFile(path="data.csv", checksum="abc123")]
        ),
        actor=actor,
    )
    with pytest.raises(ValidationError, match="Only staged"):
        api.delete_dataset(committed.dataset_id, actor=actor)

    analysis_dataset = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        actor=actor,
    )
    api.create_analysis(
        project_id=project.project_id,
        dataset_ids=[analysis_dataset.dataset_id],
        method_hash="method-1",
        code_version="v1",
        actor=actor,
    )
    with pytest.raises(ValidationError, match="analyses reference"):
        api.delete_dataset(analysis_dataset.dataset_id, actor=actor)

    claim_dataset = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        actor=actor,
    )
    api.create_claim(
        project_id=project.project_id,
        statement="This dataset is cited by a claim.",
        confidence=0.7,
        supported_by_dataset_ids=[claim_dataset.dataset_id],
        actor=actor,
    )
    with pytest.raises(ValidationError, match="claims reference"):
        api.delete_dataset(claim_dataset.dataset_id, actor=actor)


def test_promote_operational_session_to_dataset():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Did the rig pass QA?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    session = api.create_session(
        project_id=project.project_id,
        session_type=SessionType.OPERATIONAL,
        actor=actor,
    )
    manifest = DatasetCommitManifestInput(files=[DatasetFile(path="rig.log", checksum="qa123")])
    dataset = api.promote_operational_session_to_dataset(
        session.session_id,
        primary_question_id=question.question_id,
        commit_manifest=manifest,
        actor=actor,
    )
    assert dataset.commit_manifest.source_session_id == session.session_id
    assert dataset.status == DatasetStatus.COMMITTED
    assert dataset.project_id == project.project_id


def test_promote_operational_session_to_scientific():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Is this now a scientific run?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    session = api.create_session(
        project_id=project.project_id,
        session_type=SessionType.OPERATIONAL,
        actor=actor,
    )

    promoted = api.promote_operational_session(
        session.session_id,
        primary_question_id=question.question_id,
        actor=actor,
    )

    assert promoted.session_id == session.session_id
    assert promoted.session_type == SessionType.SCIENTIFIC
    assert promoted.primary_question_id == question.question_id


def test_closed_operational_session_cannot_be_promoted():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Can this session still be promoted?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    session = api.create_session(
        project_id=project.project_id,
        session_type=SessionType.OPERATIONAL,
        actor=actor,
    )
    api.update_session(session.session_id, status=SessionStatus.CLOSED, actor=actor)

    with pytest.raises(ValidationError):
        api.promote_operational_session(
            session.session_id,
            primary_question_id=question.question_id,
            actor=actor,
        )

    with pytest.raises(ValidationError):
        api.promote_operational_session_to_dataset(
            session.session_id,
            primary_question_id=question.question_id,
            commit_manifest=DatasetCommitManifestInput(
                files=[DatasetFile(path="rig.log", checksum="qa123")]
            ),
            actor=actor,
        )


def test_session_cannot_be_reopened_after_closing():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    session = api.create_session(
        project_id=project.project_id,
        session_type=SessionType.OPERATIONAL,
        actor=actor,
    )
    api.update_session(session.session_id, status=SessionStatus.CLOSED, actor=actor)

    with pytest.raises(ValidationError):
        api.update_session(session.session_id, status=SessionStatus.ACTIVE, actor=actor)


def test_active_session_cannot_set_end_time_without_closing():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    session = api.create_session(
        project_id=project.project_id,
        session_type=SessionType.OPERATIONAL,
        actor=actor,
    )

    with pytest.raises(ValidationError):
        api.update_session(
            session.session_id,
            ended_at=api.get_session(session.session_id).started_at,
            actor=actor,
        )


def test_archived_dataset_cannot_be_recommitted():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Can an archived dataset be recommitted?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    manifest = DatasetCommitManifestInput(files=[DatasetFile(path="data.csv", checksum="abc123")])
    dataset = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        commit_manifest=manifest,
        actor=actor,
    )
    api.update_dataset(dataset.dataset_id, status=DatasetStatus.COMMITTED, actor=actor)
    api.update_dataset(
        dataset.dataset_id,
        status=DatasetStatus.ARCHIVED,
        terminal_reason="Archive before recommit regression check.",
        actor=actor,
    )

    with pytest.raises(ValidationError):
        api.update_dataset(dataset.dataset_id, status=DatasetStatus.COMMITTED, actor=actor)


def test_session_link_code_roundtrip():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    session = api.create_session(
        project_id=project.project_id,
        session_type=SessionType.OPERATIONAL,
        actor=actor,
    )
    assert session.link_code
    assert session.link_code.isupper()
    assert len(session.link_code) == 26
    resolved = api.get_session_by_link_code(session.link_code)
    assert resolved.session_id == session.session_id
    with pytest.raises(ValidationError):
        api.get_session_by_link_code("not-a-link-code")


def test_auth_service_register_and_authenticate():
    service = AuthService()
    user = service.register_user("sam", "secret", Role.ADMIN)
    authenticated = service.authenticate("sam", "secret")
    assert authenticated.user_id == user.user_id


def test_role_required_for_writes():
    api = repository_backed_api()
    viewer = _actor(Role.VIEWER)
    with pytest.raises(AuthError):
        api.create_project("Nope", actor=viewer)


def test_scientific_session_requires_question():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    with pytest.raises(ValidationError):
        api.create_session(
            project_id=project.project_id,
            session_type=SessionType.SCIENTIFIC,
            actor=actor,
        )


def test_operational_session_disallows_primary_question():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Is this a maintenance session?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    with pytest.raises(ValidationError):
        api.create_session(
            project_id=project.project_id,
            session_type=SessionType.OPERATIONAL,
            primary_question_id=question.question_id,
            actor=actor,
        )
