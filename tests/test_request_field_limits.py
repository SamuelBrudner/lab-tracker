"""Request string limits match the ORM VARCHAR lengths they are stored in.

SQLite ignores declared VARCHAR lengths, but Postgres rejects an over-long value
with a DataError that surfaced as HTTP 500. Every request field that lands in a
bounded column must therefore fail validation (422) before the write, on every
backend, and accept a value of exactly the column length.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from lab_tracker import db_models, schemas

# (schema, field, ORM model, ORM column, minimal valid payload without the field)
_BOUNDED_FIELDS: list[tuple[str, str, str, str, dict[str, Any]]] = []


def _case(
    schema: str,
    field: str,
    orm_model: str,
    column: str,
    base: dict[str, Any] | None = None,
) -> None:
    _BOUNDED_FIELDS.append((schema, field, orm_model, column, base or {}))


_PROJECT_ID = str(uuid4())
_ANALYSIS_BASE = {"project_id": _PROJECT_ID, "dataset_ids": [str(uuid4())]}
_BUNDLE_ANALYSIS_BASE = {"kind": "create"}
_VIZ_BASE = {"analysis_id": str(uuid4())}
_GOAL_BASE = {"goal_type": "other"}

_case("ProjectCreate", "name", "ProjectModel", "name")
_case("ProjectCreate", "description", "ProjectModel", "description", {"name": "p"})
_case("ProjectUpdate", "name", "ProjectModel", "name")
_case("ProjectUpdate", "description", "ProjectModel", "description")
_case("ProjectGroupCreate", "name", "ProjectGroupModel", "name")
_case("ProjectGroupCreate", "description", "ProjectGroupModel", "description", {"name": "g"})
_case("ProjectGroupUpdate", "name", "ProjectGroupModel", "name")
_case("ProjectGroupUpdate", "description", "ProjectGroupModel", "description")
_case(
    "ExperimentCreate",
    "name",
    "ExperimentModel",
    "name",
    {"project_id": _PROJECT_ID, "primary_question_id": str(uuid4())},
)
_case("ExperimentUpdate", "name", "ExperimentModel", "name")
for _field in ("method_hash", "code_version", "environment_hash"):
    _case(
        "AnalysisCreate",
        _field,
        "AnalysisModel",
        _field,
        {"method_hash": "m", "code_version": "v", **_ANALYSIS_BASE},
    )
    _case(
        "EvidenceBundleCreateAnalysis",
        _field,
        "AnalysisModel",
        _field,
        {"method_hash": "m", "code_version": "v", **_BUNDLE_ANALYSIS_BASE},
    )
_case("AnalysisUpdate", "environment_hash", "AnalysisModel", "environment_hash")
_case("AnalysisCommitRequest", "environment_hash", "AnalysisModel", "environment_hash")
for _field in ("viz_type", "file_path"):
    _case(
        "VisualizationCreate",
        _field,
        "VisualizationModel",
        _field,
        {"viz_type": "line", "file_path": "f.png", **_VIZ_BASE},
    )
    _case("VisualizationUpdate", _field, "VisualizationModel", _field)
    _case(
        "EvidenceBundleCreateVisualization",
        _field,
        "VisualizationModel",
        _field,
        {"kind": "create", "viz_type": "line", "file_path": "f.png"},
    )
    _case(
        "VisualizationInputRequest",
        _field,
        "VisualizationModel",
        _field,
        {"viz_type": "line", "file_path": "f.png"},
    )
for _field in ("file_path", "checksum"):
    _case(
        "AcquisitionOutputCreate",
        _field,
        "AcquisitionOutputModel",
        _field,
        {"file_path": "raw/a.nwb", "checksum": "c1"},
    )
_case("GoalCreate", "title", "GoalModel", "title", _GOAL_BASE)
_case("GoalCreate", "external_ref", "GoalModel", "external_ref", {"title": "t", **_GOAL_BASE})
_case("GoalUpdate", "title", "GoalModel", "title")
_case("GoalUpdate", "external_ref", "GoalModel", "external_ref")
_case("AuthRegisterRequest", "username", "UserModel", "username", {"password": "pw"})
_case("AuthInvitationCreate", "email", "InvitationModel", "email")


def _column_length(orm_model: str, column: str) -> int:
    length = getattr(db_models, orm_model).__table__.columns[column].type.length
    assert isinstance(length, int)
    return length


@pytest.mark.parametrize(
    ("schema_name", "field", "orm_model", "column", "base"),
    _BOUNDED_FIELDS,
    ids=[f"{schema}.{field}" for schema, field, *_ in _BOUNDED_FIELDS],
)
def test_request_string_limits_match_orm_column_lengths(
    schema_name: str,
    field: str,
    orm_model: str,
    column: str,
    base: dict[str, Any],
) -> None:
    schema_type: type[BaseModel] = getattr(schemas, schema_name)
    limit = _column_length(orm_model, column)

    accepted = schema_type.model_validate({**base, field: "x" * limit})
    assert getattr(accepted, field) == "x" * limit

    with pytest.raises(ValidationError) as excinfo:
        schema_type.model_validate({**base, field: "x" * (limit + 1)})
    assert [error["type"] for error in excinfo.value.errors()][0] == "string_too_long"


def test_http_over_long_values_are_422_and_boundary_values_are_stored(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    name_limit = _column_length("ProjectModel", "name")
    description_limit = _column_length("ProjectModel", "description")

    too_long = client.post(
        "/projects",
        json={"name": "n" * (name_limit + 1)},
        headers=admin_auth_headers,
    )
    assert too_long.status_code == 422
    assert [issue["field"] for issue in too_long.json()["error"]["issues"]] == ["name"]

    boundary = client.post(
        "/projects",
        json={"name": "n" * name_limit, "description": "d" * description_limit},
        headers=admin_auth_headers,
    )
    assert boundary.status_code == 201
    assert boundary.json()["data"]["name"] == "n" * name_limit

    title_limit = _column_length("GoalModel", "title")
    goal = client.post(
        "/goals",
        json={"goal_type": "other", "title": "t" * (title_limit + 1)},
        headers=admin_auth_headers,
    )
    assert goal.status_code == 422
    assert [issue["field"] for issue in goal.json()["error"]["issues"]] == ["title"]
