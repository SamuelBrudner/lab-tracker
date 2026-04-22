from uuid import uuid4

import pytest

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, AuthService, Role
from lab_tracker.errors import AuthError, ValidationError
from lab_tracker.models import (
    DatasetCommitManifestInput,
    DatasetFile,
    DatasetStatus,
    ProjectReviewPolicy,
    QuestionLinkRole,
    QuestionStatus,
    QuestionType,
    SessionStatus,
    SessionType,
)


def _actor(role: Role = Role.ADMIN) -> AuthContext:
    return AuthContext(user_id=uuid4(), role=role)


def test_project_question_dataset_flow():
    api = LabTrackerAPI.in_memory()
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


def test_dataset_requires_primary_question():
    api = LabTrackerAPI.in_memory()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    with pytest.raises(ValidationError):
        api.create_dataset(
            project_id=project.project_id,
            primary_question_id=None,  # type: ignore[arg-type]
            actor=actor,
        )


def test_commit_hash_is_content_addressed():
    api = LabTrackerAPI.in_memory()
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


def test_dataset_commit_manifest_preserves_nwb_metadata_keys():
    api = LabTrackerAPI.in_memory()
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
    api = LabTrackerAPI.in_memory()
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
    api = LabTrackerAPI.in_memory()
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
    api = LabTrackerAPI.in_memory()
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


def test_dataset_commit_requires_file_attachment():
    api = LabTrackerAPI.in_memory()
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
    with pytest.raises(ValidationError, match="At least one file is required to commit a dataset."):
        api.update_dataset(dataset.dataset_id, status=DatasetStatus.COMMITTED, actor=actor)


def test_dataset_commit_ignores_legacy_review_policy_and_commits_directly():
    api = LabTrackerAPI.in_memory()
    actor = _actor(Role.EDITOR)
    project = api.create_project(
        "Neuro Project",
        review_policy=ProjectReviewPolicy.ALL,
        actor=actor,
    )
    question = api.create_question(
        project_id=project.project_id,
        text="Is the signal stable?",
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

    updated = api.update_dataset(dataset.dataset_id, status=DatasetStatus.COMMITTED, actor=actor)
    assert updated.status == DatasetStatus.COMMITTED
    assert api.list_dataset_reviews(dataset_id=dataset.dataset_id) == []


def test_committed_dataset_is_immutable():
    api = LabTrackerAPI.in_memory()
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
    archived = api.update_dataset(dataset.dataset_id, status=DatasetStatus.ARCHIVED, actor=actor)
    assert archived.status == DatasetStatus.ARCHIVED


def test_promote_operational_session_to_dataset():
    api = LabTrackerAPI.in_memory()
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
    api = LabTrackerAPI.in_memory()
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
    api = LabTrackerAPI.in_memory()
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
    api = LabTrackerAPI.in_memory()
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
    api = LabTrackerAPI.in_memory()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    session = api.create_session(
        project_id=project.project_id,
        session_type=SessionType.OPERATIONAL,
        actor=actor,
    )

    with pytest.raises(ValidationError):
        api.update_session(session.session_id, ended_at=api.get_session(session.session_id).started_at, actor=actor)


def test_archived_dataset_cannot_be_recommitted():
    api = LabTrackerAPI.in_memory()
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
    api.update_dataset(dataset.dataset_id, status=DatasetStatus.ARCHIVED, actor=actor)

    with pytest.raises(ValidationError):
        api.update_dataset(dataset.dataset_id, status=DatasetStatus.COMMITTED, actor=actor)


def test_session_link_code_roundtrip():
    api = LabTrackerAPI.in_memory()
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
    api = LabTrackerAPI.in_memory()
    viewer = _actor(Role.VIEWER)
    with pytest.raises(AuthError):
        api.create_project("Nope", actor=viewer)


def test_scientific_session_requires_question():
    api = LabTrackerAPI.in_memory()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    with pytest.raises(ValidationError):
        api.create_session(
            project_id=project.project_id,
            session_type=SessionType.SCIENTIFIC,
            actor=actor,
        )


def test_operational_session_disallows_primary_question():
    api = LabTrackerAPI.in_memory()
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
def test_update_note_accepts_extracted_entities():
    api = LabTrackerAPI.in_memory()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    note = api.create_note(
        project_id=project.project_id,
        raw_content="raw note",
        actor=actor,
    )

    updated = api.update_note(
        note.note_id,
        extracted_entities=[
            ("Neuron", 0.82, "ocr:model-1"),
            ("Hippocampus", 0.77, "ocr:model-1"),
        ],
        actor=actor,
    )

    assert [entity.label for entity in updated.extracted_entities] == [
        "Neuron",
        "Hippocampus",
    ]
    assert updated.extracted_entities[0].confidence == 0.82
    assert updated.extracted_entities[0].provenance == "ocr:model-1"
