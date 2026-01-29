from uuid import uuid4

import pytest

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, AuthService, Role
from lab_tracker.errors import AuthError, ValidationError
from lab_tracker.models import (
    DatasetCommitManifestInput,
    DatasetFile,
    DatasetStatus,
    QuestionLinkRole,
    QuestionSource,
    QuestionStatus,
    QuestionType,
    SessionType,
    TagSuggestionStatus,
)


def _actor(role: Role = Role.ADMIN) -> AuthContext:
    return AuthContext(user_id=uuid4(), role=role)


def test_project_question_dataset_flow():
    api = LabTrackerAPI()
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
    api = LabTrackerAPI()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    with pytest.raises(ValidationError):
        api.create_dataset(
            project_id=project.project_id,
            primary_question_id=None,  # type: ignore[arg-type]
            actor=actor,
        )


def test_commit_hash_is_content_addressed():
    api = LabTrackerAPI()
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


def test_dataset_commit_requires_active_question():
    api = LabTrackerAPI()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Is the signal stable?",
        question_type=QuestionType.DESCRIPTIVE,
        actor=actor,
    )
    dataset = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        actor=actor,
    )
    with pytest.raises(ValidationError):
        api.update_dataset(dataset.dataset_id, status=DatasetStatus.COMMITTED, actor=actor)
    api.update_question(question.question_id, status=QuestionStatus.ACTIVE, actor=actor)
    committed = api.update_dataset(dataset.dataset_id, status=DatasetStatus.COMMITTED, actor=actor)
    assert committed.status == DatasetStatus.COMMITTED


def test_committed_dataset_is_immutable():
    api = LabTrackerAPI()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Does activity drift?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    manifest = DatasetCommitManifestInput(
        files=[DatasetFile(path="data.csv", checksum="abc123")]
    )
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
    api = LabTrackerAPI()
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
    manifest = DatasetCommitManifestInput(
        files=[DatasetFile(path="rig.log", checksum="qa123")]
    )
    dataset = api.promote_operational_session(
        session.session_id,
        primary_question_id=question.question_id,
        commit_manifest=manifest,
        actor=actor,
    )
    assert dataset.commit_manifest.source_session_id == session.session_id
    assert dataset.status == DatasetStatus.COMMITTED
    assert dataset.project_id == project.project_id


def test_auth_service_register_and_authenticate():
    service = AuthService()
    user = service.register_user("sam", "secret", Role.ADMIN)
    authenticated = service.authenticate("sam", "secret")
    assert authenticated.user_id == user.user_id


def test_role_required_for_writes():
    api = LabTrackerAPI()
    viewer = _actor(Role.VIEWER)
    with pytest.raises(AuthError):
        api.create_project("Nope", actor=viewer)


def test_scientific_session_requires_question():
    api = LabTrackerAPI()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    with pytest.raises(ValidationError):
        api.create_session(
            project_id=project.project_id,
            session_type=SessionType.SCIENTIFIC,
            actor=actor,
        )


def test_extract_questions_from_note_stages_questions():
    api = LabTrackerAPI()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    note = api.create_note(
        project_id=project.project_id,
        raw_content=(
            "Q: Does PV inhibition broaden tuning\n"
            "- Can we see layer-specific effects?\n"
            "Question: What is the baseline distribution\n"
            "Notes: check controls"
        ),
        actor=actor,
    )

    questions = api.extract_questions_from_note(note.note_id, actor=actor)

    assert {question.text for question in questions} == {
        "Does PV inhibition broaden tuning",
        "Can we see layer-specific effects?",
        "What is the baseline distribution",
    }
    assert all(question.status == QuestionStatus.STAGED for question in questions)
    assert all(question.created_from == QuestionSource.API for question in questions)
    assert all(question.created_by and str(note.note_id) in question.created_by for question in questions)
    assert api.extract_questions_from_note(note.note_id, actor=actor) == []


def test_suggest_entity_tags_creates_suggestions_and_dedupes():
    api = LabTrackerAPI()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    note = api.create_note(
        project_id=project.project_id,
        raw_content="Neuron note",
        extracted_entities=[("Neuron", 0.8, "ocr:model-1")],
        actor=actor,
    )

    suggestions = api.suggest_entity_tags(note.note_id, actor=actor)

    assert suggestions
    assert all(suggestion.entity_label == "Neuron" for suggestion in suggestions)
    assert all(suggestion.status == TagSuggestionStatus.STAGED for suggestion in suggestions)
    assert api.suggest_entity_tags(note.note_id, actor=actor) == []


def test_review_entity_tag_suggestion_updates_status():
    api = LabTrackerAPI()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    note = api.create_note(
        project_id=project.project_id,
        raw_content="Neuron note",
        extracted_entities=[("Neuron", 0.8, "ocr:model-1")],
        actor=actor,
    )

    suggestion = api.suggest_entity_tags(note.note_id, actor=actor)[0]
    reviewed = api.review_entity_tag_suggestion(
        note.note_id,
        suggestion.suggestion_id,
        status=TagSuggestionStatus.ACCEPTED,
        reviewed_by="reviewer",
        actor=actor,
    )

    assert reviewed.status == TagSuggestionStatus.ACCEPTED
    assert reviewed.reviewed_by == "reviewer"
    assert reviewed.reviewed_at is not None
