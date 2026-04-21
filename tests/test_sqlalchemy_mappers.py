from datetime import datetime, timezone
from uuid import UUID, uuid4

from lab_tracker.models import (
    Dataset,
    DatasetCommitManifest,
    EntityRef,
    EntityTagSuggestion,
    EntityType,
    ExtractedEntity,
    Note,
    NoteStatus,
    OutcomeStatus,
    Project,
    ProjectStatus,
    Question,
    QuestionLink,
    QuestionLinkRole,
    QuestionSource,
    QuestionStatus,
    QuestionType,
    TagSuggestionStatus,
)
from lab_tracker.sqlalchemy_mappers import (
    dataset_from_model,
    dataset_question_link_from_model,
    dataset_question_link_models,
    dataset_to_model,
    entity_ref_from_model,
    extracted_entity_from_model,
    note_extracted_entity_models,
    note_from_model,
    note_tag_suggestion_models,
    note_target_models,
    note_to_model,
    project_from_model,
    project_to_model,
    question_from_model,
    question_parent_models,
    question_to_model,
    tag_suggestion_from_model,
)


def _ts() -> datetime:
    return datetime(2026, 2, 7, tzinfo=timezone.utc)


def test_project_mapper_round_trip():
    project = Project(
        project_id=uuid4(),
        name="Neural Mapping",
        description="Parent persistence milestone",
        status=ProjectStatus.ARCHIVED,
        created_by="scientist@example.com",
        created_at=_ts(),
        updated_at=_ts(),
    )
    row = project_to_model(project)
    mapped = project_from_model(row)
    assert mapped == project


def test_question_mapper_round_trip_with_parent_links():
    parent_id = uuid4()
    question = Question(
        question_id=uuid4(),
        project_id=uuid4(),
        text="How stable is the baseline?",
        question_type=QuestionType.DESCRIPTIVE,
        hypothesis="Baseline should stay within 2 SD.",
        status=QuestionStatus.ACTIVE,
        parent_question_ids=[parent_id],
        created_from=QuestionSource.IMPORTED,
        source_provenance="ocr://note-1",
        created_by="operator-1",
        created_at=_ts(),
        updated_at=_ts(),
    )
    row = question_to_model(question)
    parent_rows = question_parent_models(question)
    parent_ids = [UUID(parent_row.parent_question_id) for parent_row in parent_rows]
    mapped = question_from_model(row, parent_question_ids=parent_ids)
    assert parent_ids == [parent_id]
    assert mapped == question


def test_dataset_mapper_round_trip_preserves_manifest_fields():
    primary_question_id = uuid4()
    secondary_question_id = uuid4()
    links = [
        QuestionLink(
            question_id=primary_question_id,
            role=QuestionLinkRole.PRIMARY,
            outcome_status=OutcomeStatus.SUPPORTS,
        ),
        QuestionLink(
            question_id=secondary_question_id,
            role=QuestionLinkRole.SECONDARY,
            outcome_status=OutcomeStatus.INCONCLUSIVE,
        ),
    ]
    dataset = Dataset(
        dataset_id=uuid4(),
        project_id=uuid4(),
        commit_hash="abc123",
        primary_question_id=primary_question_id,
        question_links=links,
        commit_manifest=DatasetCommitManifest(
            question_links=links,
            metadata={"run": "7"},
            bids_metadata={"Name": "Example"},
            nwb_metadata={"Session Description": "baseline"},
            note_ids=[uuid4()],
            extraction_provenance=["nlp-v1"],
            source_session_id=uuid4(),
        ),
        created_by="operator-1",
        created_at=_ts(),
        updated_at=_ts(),
    )
    row = dataset_to_model(dataset)
    link_rows = dataset_question_link_models(dataset)
    mapped_links = [dataset_question_link_from_model(item) for item in link_rows]
    mapped = dataset_from_model(row, question_links=mapped_links)
    assert mapped.question_links == links
    assert mapped.commit_manifest.question_links == links
    assert mapped.commit_manifest.files == []
    assert mapped.commit_manifest.metadata == {"run": "7"}
    assert mapped.commit_manifest.bids_metadata == {"Name": "Example"}
    assert mapped.commit_manifest.nwb_metadata == {"Session Description": "baseline"}
    assert mapped.commit_manifest.note_ids == dataset.commit_manifest.note_ids
    assert mapped.commit_manifest.extraction_provenance == ["nlp-v1"]
    assert mapped.commit_manifest.source_session_id == dataset.commit_manifest.source_session_id


def test_note_mapper_round_trip_for_supported_fields():
    extracted = [ExtractedEntity(label="hippocampus", confidence=0.9, provenance="ocr")]
    targets = [EntityRef(entity_type=EntityType.QUESTION, entity_id=uuid4())]
    suggestions = [
        EntityTagSuggestion(
            suggestion_id=uuid4(),
            entity_label="hippocampus",
            vocabulary="UBERON",
            term_id="0002421",
            term_label="Hippocampus",
            confidence=0.92,
            provenance="nlp-v1",
            status=TagSuggestionStatus.ACCEPTED,
            reviewed_by="operator-2",
            reviewed_at=_ts(),
        )
    ]
    note = Note(
        note_id=uuid4(),
        project_id=uuid4(),
        raw_content="notes.md",
        transcribed_text="signal is stable",
        extracted_entities=extracted,
        tag_suggestions=suggestions,
        targets=targets,
        metadata={"device": "np2"},
        status=NoteStatus.COMMITTED,
        created_by="operator-1",
        created_at=_ts(),
        updated_at=_ts(),
    )
    row = note_to_model(note)
    mapped = note_from_model(
        row,
        extracted_entities=[
            extracted_entity_from_model(item) for item in note_extracted_entity_models(note)
        ],
        tag_suggestions=[
            tag_suggestion_from_model(item) for item in note_tag_suggestion_models(note)
        ],
        targets=[entity_ref_from_model(item) for item in note_target_models(note)],
    )
    assert mapped.raw_asset is None
    assert mapped.metadata == {"device": "np2"}
    assert mapped.extracted_entities == extracted
    assert mapped.tag_suggestions == suggestions
    assert mapped.targets == targets
