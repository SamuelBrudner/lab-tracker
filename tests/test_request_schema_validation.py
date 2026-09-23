import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from lab_tracker import schemas
from lab_tracker.models import (
    ClaimInput,
    DatasetCommitManifestInput,
    DatasetFile,
    EntityRef,
    ExternalArtifactReference,
    QuestionLink,
    QuestionType,
    VisualizationInput,
)
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


# --- M39: nested request objects forbid unknown keys like their parents -------

_ARTIFACT = {"source_system": "s3", "uri": "s3://lab/run-1/manifest.json", "content_hash": "h1"}


def _nested_extra_cases() -> list[tuple[str, dict[str, object], tuple[object, ...]]]:
    project_id = str(uuid4())
    question_id = str(uuid4())
    entity_ref = {"entity_type": "claim", "entity_id": str(uuid4())}
    return [
        (
            "NoteCreate",
            {
                "project_id": project_id,
                "raw_content": "x",
                "targets": [{**entity_ref, "role": "support"}],
            },
            ("targets", 0, "role"),
        ),
        ("NoteUpdate", {"targets": [{**entity_ref, "role": "x"}]}, ("targets", 0, "role")),
        (
            "DatasetCreate",
            {
                "project_id": project_id,
                "primary_question_id": question_id,
                "commit_manifest": {
                    "files": [{"path": "a.nwb", "checksum": "c1", "sizeBytes": 5}],
                },
            },
            ("commit_manifest", "files", 0, "sizeBytes"),
        ),
        (
            "DatasetCreate",
            {
                "project_id": project_id,
                "primary_question_id": question_id,
                "commit_manifest": {"files": [], "noteIds": []},
            },
            ("commit_manifest", "noteIds"),
        ),
        (
            "DatasetUpdate",
            {"commit_manifest": {"external_artifacts": [{**_ARTIFACT, "hash": "x"}]}},
            ("commit_manifest", "external_artifacts", 0, "hash"),
        ),
        (
            "DatasetUpdate",
            {"question_links": [{"question_id": question_id, "role": "primary", "outcome": "x"}]},
            ("question_links", 0, "outcome"),
        ),
        (
            "SessionDatasetPromotionRequest",
            {
                "primary_question_id": question_id,
                "commit_manifest": {"bogus": 1},
            },
            ("commit_manifest", "bogus"),
        ),
        (
            "AnalysisCreate",
            {
                "project_id": project_id,
                "dataset_ids": [str(uuid4())],
                "method_hash": "m",
                "code_version": "v",
                "external_artifacts": [{**_ARTIFACT, "bogus": 1}],
            },
            ("external_artifacts", 0, "bogus"),
        ),
        (
            "ClaimCreate",
            {
                "project_id": project_id,
                "statement": "s",
                "confidence": 5,
                "external_citations": [{**_ARTIFACT, "bogus": 1}],
            },
            ("external_citations", 0, "bogus"),
        ),
        (
            "AnalysisCommitRequest",
            {"claims": [{"statement": "s", "confidence": 5, "falsifcation_criteria": "typo"}]},
            ("claims", 0, "falsifcation_criteria"),
        ),
        (
            "AnalysisCommitRequest",
            {"claims": [{"statement": "s", "confidence": 5, "external_citations": [
                {**_ARTIFACT, "bogus": 1}
            ]}]},
            ("claims", 0, "external_citations", 0, "bogus"),
        ),
        (
            "AnalysisCommitRequest",
            {"visualizations": [{"viz_type": "line", "file_path": "f.png", "title": "t"}]},
            ("visualizations", 0, "title"),
        ),
        (
            "ExplorationNodeUpdate",
            {"evidence_refs": [{**entity_ref, "note": "x"}]},
            ("evidence_refs", 0, "note"),
        ),
    ]


@pytest.mark.parametrize(
    ("schema_name", "payload", "extra_loc"),
    _nested_extra_cases(),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_nested_request_objects_reject_unknown_keys(
    schema_name: str,
    payload: dict[str, object],
    extra_loc: tuple[object, ...],
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        getattr(schemas, schema_name).model_validate(payload)

    # PATCH fields typed ``X | SkipJsonSchema[None]`` are unions, so pydantic
    # also reports the rejected ``none`` branch and tags locs with the branch;
    # only the extra-key error carries the nested path.
    forbidden = [
        tuple(part for part in error["loc"] if not (isinstance(part, str) and "[" in part))
        for error in excinfo.value.errors()
        if error["type"] == "extra_forbidden"
    ]
    assert forbidden == [extra_loc]
    assert {error["type"] for error in excinfo.value.errors()} <= {
        "extra_forbidden",
        "none_required",
    }


def test_nested_request_objects_still_validate_into_domain_models() -> None:
    question_id = uuid4()
    update = schemas.DatasetUpdate.model_validate(
        {
            "commit_manifest": {
                "files": [{"path": "a.nwb", "checksum": "c1"}],
                "external_artifacts": [_ARTIFACT],
            },
            "question_links": [{"question_id": str(question_id), "role": "primary"}],
        }
    )
    assert type(update.commit_manifest) is DatasetCommitManifestInput
    assert type(update.commit_manifest.files[0]) is DatasetFile
    assert update.commit_manifest.files[0].model_fields_set == {"path", "checksum"}
    assert type(update.commit_manifest.external_artifacts[0]) is ExternalArtifactReference
    assert update.question_links == [QuestionLink(question_id=question_id, role="primary")]

    commit = schemas.AnalysisCommitRequest.model_validate(
        {
            "claims": [{"statement": "s", "confidence": 5, "falsification_criteria": "c"}],
            "visualizations": [{"viz_type": "line", "file_path": "f.png"}],
        }
    )
    assert type(commit.claims[0]) is ClaimInput
    assert commit.claims[0].falsification_criteria == "c"
    assert type(commit.visualizations[0]) is VisualizationInput

    # Python callers may keep passing domain instances.
    target = EntityRef(entity_type="claim", entity_id=uuid4())
    note = NoteCreate(project_id=uuid4(), raw_content="x", targets=[target])
    assert note.targets == [target]
    assert type(note.targets[0]) is EntityRef


def test_nested_request_objects_advertise_closed_json_schemas() -> None:
    definitions = schemas.DatasetCreate.model_json_schema()["$defs"]

    closed = {
        name
        for name, definition in definitions.items()
        if definition.get("additionalProperties") is False
    }
    assert {"DatasetCommitManifestInputRequest", "DatasetFileRequest"} <= closed
    assert "ExternalArtifactReferenceRequest" in closed


def test_http_note_target_with_misspelled_key_is_rejected(
    client,
    admin_auth_headers,
) -> None:
    project = client.post("/projects", json={"name": "Nested keys"}, headers=admin_auth_headers)
    project_id = project.json()["data"]["project_id"]

    response = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": "Pointing at the project",
            "targets": [
                {"entity_type": "project", "entity_id": project_id, "entityRole": "x"}
            ],
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == 422
    issues = response.json()["error"]["issues"]
    assert [issue["field"] for issue in issues] == ["targets.0.entityRole"]


def test_http_upload_target_with_unknown_key_is_rejected(
    client,
    admin_auth_headers,
) -> None:
    project = client.post("/projects", json={"name": "Upload keys"}, headers=admin_auth_headers)
    project_id = project.json()["data"]["project_id"]

    response = client.post(
        "/notes/upload-file",
        data={
            "project_id": project_id,
            "targets": json.dumps(
                [{"entity_type": "project", "entity_id": project_id, "role": "x"}]
            ),
        },
        files={"file": ("photo.png", b"not-really-a-png", "image/png")},
        headers=admin_auth_headers,
    )

    assert response.status_code == 422
    assert response.json()["error"]["message"] == "targets contains invalid entity refs."
