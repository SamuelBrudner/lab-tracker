from uuid import uuid4

import pytest
from pydantic import ValidationError

from lab_tracker.models import QuestionType
from lab_tracker.schemas import (
    AnalysisCreate,
    ClaimCreate,
    NoteCreate,
    NoteUpdate,
    ProjectUpdate,
    QuestionCreate,
    QuestionUpdate,
    VisualizationCreate,
)


def test_request_schemas_reject_blank_strings_before_services():
    project_id = uuid4()

    with pytest.raises(ValidationError, match="must not be empty"):
        ProjectUpdate(name="   ")
    with pytest.raises(ValidationError, match="must not be empty"):
        QuestionCreate(
            project_id=project_id,
            text=" ",
            question_type=QuestionType.DESCRIPTIVE,
        )
    with pytest.raises(ValidationError, match="must not be empty"):
        QuestionUpdate(text="\t")
    with pytest.raises(ValidationError, match="must not be empty"):
        VisualizationCreate(analysis_id=uuid4(), viz_type="line", file_path=" ")


def test_request_schemas_reject_duplicate_uuid_lists_before_services():
    duplicate_id = uuid4()

    with pytest.raises(ValidationError, match="Duplicate id in list"):
        QuestionCreate(
            project_id=uuid4(),
            text="How stable is the baseline?",
            question_type=QuestionType.DESCRIPTIVE,
            parent_question_ids=[duplicate_id, duplicate_id],
        )
    with pytest.raises(ValidationError, match="Duplicate id in list"):
        AnalysisCreate(
            project_id=uuid4(),
            dataset_ids=[duplicate_id, duplicate_id],
            method_hash="method-1",
            code_version="v1",
        )
    with pytest.raises(ValidationError, match="Duplicate id in list"):
        ClaimCreate(
            project_id=uuid4(),
            statement="Signal is stable",
            confidence=42.0,
            supported_by_dataset_ids=[duplicate_id, duplicate_id],
        )


def test_note_request_schema_normalizes_metadata_before_services():
    payload = NoteCreate(
        project_id=uuid4(),
        raw_content="Observation",
        metadata={
            " rig ": " np2 ",
            "approved": True,
            "count": 3,
        },
    )

    assert payload.metadata == {
        "rig": "np2",
        "approved": "True",
        "count": "3",
    }


def test_note_request_schema_rejects_empty_metadata_keys_before_services():
    with pytest.raises(ValidationError, match="metadata key must not be empty"):
        NoteUpdate(metadata={"   ": "camera"})
