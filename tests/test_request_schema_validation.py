from uuid import uuid4

import pytest
from pydantic import ValidationError

from lab_tracker import schemas
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


def test_auth_user_update_requires_one_optional_non_null_field() -> None:
    with pytest.raises(
        ValidationError,
        match="At least one of password or role must be provided",
    ):
        schemas.AuthUserUpdate.model_validate({})

    auth_update_schema = schemas.AuthUserUpdate.model_json_schema()
    assert auth_update_schema["minProperties"] == 1
    assert "required" not in auth_update_schema


_NON_NULL_PATCH_FIELDS = {
    "AuthUserUpdate": ("password", "role"),
    "ProjectUpdate": ("name", "description", "status"),
    "ProjectGroupUpdate": ("name", "description", "kind", "group_read_all"),
    "SupervisionEdgeUpdate": (
        "supervisor_user_id",
        "supervisee_user_id",
        "started_at",
    ),
    "QuestionUpdate": ("text", "question_type", "status", "parent_question_ids"),
    "DatasetUpdate": ("commit_manifest", "commit_hash", "status", "question_links"),
    "NoteUpdate": ("targets", "metadata", "status"),
    "GraphDraftOperationUpdate": ("payload", "status"),
    "GraphDraftBatchSettingsUpdate": (
        "enabled",
        "cadence_minutes",
        "run_at_local_time",
        "timezone_name",
        "user_id",
    ),
    "SessionUpdate": ("status",),
    "AnalysisUpdate": ("status", "external_artifacts"),
    "ClaimUpdate": (
        "statement",
        "confidence",
        "status",
        "supported_by_dataset_ids",
        "supported_by_analysis_ids",
        "answers_question_ids",
        "external_citations",
    ),
    "ExplorationNodeUpdate": (
        "title",
        "status",
        "alternatives_considered",
        "evidence_refs",
        "parent_node_ids",
        "also_depends_on_node_ids",
    ),
    "GoalUpdate": ("goal_type", "title", "summary", "status", "attributes", "links"),
    "GoalLinkUpdate": ("relation", "link_status"),
    "VisualizationUpdate": ("viz_type", "file_path", "related_claim_ids"),
}

_NULLABLE_PATCH_FIELDS = {
    "ProjectUpdate": ("group_id",),
    "SupervisionEdgeUpdate": ("ended_at",),
    "QuestionUpdate": ("hypothesis", "terminal_reason"),
    "DatasetUpdate": ("terminal_reason",),
    "NoteUpdate": ("transcribed_text",),
    "GraphDraftOperationUpdate": ("review_note",),
    "SessionUpdate": ("ended_at",),
    "AnalysisUpdate": ("environment_hash", "terminal_reason"),
    "ClaimUpdate": (
        "terminal_reason",
        "falsification_criteria",
        "verification_plan",
        "refuting_outcome",
    ),
    "ExplorationNodeUpdate": (
        "choice",
        "rationale",
        "hypothesis",
        "failure_mode",
        "lesson",
        "tooling_context",
        "trigger",
        "invalidates_node_id",
        "invalidates_claim_id",
    ),
    "GoalUpdate": ("target_date", "external_ref"),
    "GoalLinkUpdate": ("slot",),
    "VisualizationUpdate": ("caption",),
}


@pytest.mark.parametrize(
    ("schema_name", "field_name"),
    [
        (schema_name, field_name)
        for schema_name, field_names in _NON_NULL_PATCH_FIELDS.items()
        for field_name in field_names
    ],
)
def test_optional_non_null_patch_fields_reject_explicit_null_and_hide_it_in_openapi(
    schema_name: str,
    field_name: str,
) -> None:
    schema_type = getattr(schemas, schema_name)

    with pytest.raises(ValidationError, match=rf"{field_name} must not be null"):
        schema_type.model_validate({field_name: None})

    field_schema = schema_type.model_json_schema()["properties"][field_name]
    assert not _json_schema_allows_null(field_schema)


@pytest.mark.parametrize(
    ("schema_name", "field_name"),
    [
        (schema_name, field_name)
        for schema_name, field_names in _NULLABLE_PATCH_FIELDS.items()
        for field_name in field_names
    ],
)
def test_nullable_patch_fields_preserve_explicit_null_presence(
    schema_name: str,
    field_name: str,
) -> None:
    schema_type = getattr(schemas, schema_name)

    payload = schema_type.model_validate({field_name: None})

    assert field_name in payload.model_fields_set
    assert getattr(payload, field_name) is None
    assert _json_schema_allows_null(
        schema_type.model_json_schema()["properties"][field_name]
    )


@pytest.mark.parametrize(
    "schema_name",
    sorted(
        (set(_NON_NULL_PATCH_FIELDS) | set(_NULLABLE_PATCH_FIELDS))
        - {"AuthUserUpdate"}
    ),
)
def test_optional_patch_models_keep_omission_out_of_model_fields_set(
    schema_name: str,
) -> None:
    payload = getattr(schemas, schema_name).model_validate({})

    assert payload.model_fields_set == set()


@pytest.mark.parametrize(
    ("schema_name", "field_name"),
    (
        ("GroupMembershipUpdate", "role"),
        ("ProjectMembershipUpdate", "role"),
        ("ProvenanceLinkStatusUpdate", "status"),
    ),
)
def test_required_patch_fields_reject_omission_and_null(
    schema_name: str,
    field_name: str,
) -> None:
    schema_type = getattr(schemas, schema_name)

    with pytest.raises(ValidationError):
        schema_type.model_validate({})
    with pytest.raises(ValidationError):
        schema_type.model_validate({field_name: None})


def _json_schema_allows_null(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("type") == "null":
        return True
    return any(
        _json_schema_allows_null(item)
        for key in ("anyOf", "oneOf")
        for item in value.get(key, [])
    )
