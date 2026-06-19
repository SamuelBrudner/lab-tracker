from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from lab_tracker import dolt_mirror
from lab_tracker.db_models import (
    AnalysisModel,
    ClaimModel,
    DatasetModel,
    GraphDraftBatchSettingsModel,
    NoteModel,
    ProjectModel,
    QuestionModel,
    SessionModel,
    VisualizationModel,
)

pytestmark = pytest.mark.postgres


def _create_representative_project_tree(
    client: TestClient,
    headers: dict[str, str],
) -> dict[str, str]:
    project_response = client.post(
        "/projects",
        json={"name": "Postgres cascade project"},
        headers=headers,
    )
    assert project_response.status_code == 201
    project_id = project_response.json()["data"]["project_id"]

    question_response = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Does Postgres preserve cascade semantics?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=headers,
    )
    assert question_response.status_code == 201
    question_id = question_response.json()["data"]["question_id"]

    dataset_response = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": question_id,
            "status": "committed",
            "commit_manifest": {
                "files": [{"path": "data/postgres.csv", "checksum": "sha256:pg"}],
                "metadata": {"backend": "postgres"},
            },
        },
        headers=headers,
    )
    assert dataset_response.status_code == 201
    dataset_id = dataset_response.json()["data"]["dataset_id"]

    note_response = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": "Postgres JSON/boolean/FK representative note.",
            "metadata": {"backend": "postgres"},
            "targets": [{"entity_type": "dataset", "entity_id": dataset_id}],
        },
        headers=headers,
    )
    assert note_response.status_code == 201
    note_id = note_response.json()["data"]["note_id"]

    session_response = client.post(
        "/sessions",
        json={
            "project_id": project_id,
            "session_type": "scientific",
            "primary_question_id": question_id,
        },
        headers=headers,
    )
    assert session_response.status_code == 201
    session_id = session_response.json()["data"]["session_id"]

    analysis_response = client.post(
        "/analyses",
        json={
            "project_id": project_id,
            "dataset_ids": [dataset_id],
            "method_hash": "postgres-method",
            "code_version": "postgres-v1",
            "status": "committed",
            "external_artifacts": [
                {
                    "source_system": "s3",
                    "uri": "s3://lab-tracker/postgres-run",
                    "metadata": {"backend": "postgres"},
                }
            ],
        },
        headers=headers,
    )
    assert analysis_response.status_code == 201
    analysis_id = analysis_response.json()["data"]["analysis_id"]

    claim_response = client.post(
        "/claims",
        json={
            "project_id": project_id,
            "statement": "The Postgres cascade path remains coherent.",
            "confidence": 0.9,
            "status": "supported",
            "supported_by_dataset_ids": [dataset_id],
            "supported_by_analysis_ids": [analysis_id],
            "answers_question_ids": [question_id],
        },
        headers=headers,
    )
    assert claim_response.status_code == 201
    claim_id = claim_response.json()["data"]["claim_id"]

    visualization_response = client.post(
        "/visualizations",
        json={
            "analysis_id": analysis_id,
            "viz_type": "line",
            "file_path": "figures/postgres.png",
            "related_claim_ids": [claim_id],
        },
        headers=headers,
    )
    assert visualization_response.status_code == 201
    viz_id = visualization_response.json()["data"]["viz_id"]

    return {
        "project_id": project_id,
        "question_id": question_id,
        "dataset_id": dataset_id,
        "note_id": note_id,
        "session_id": session_id,
        "analysis_id": analysis_id,
        "claim_id": claim_id,
        "viz_id": viz_id,
    }


def test_postgres_project_delete_cascades_representative_entity_tree(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
) -> None:
    ids = _create_representative_project_tree(postgres_client, postgres_admin_auth_headers)

    response = postgres_client.delete(
        f"/projects/{ids['project_id']}",
        headers=postgres_admin_auth_headers,
    )

    assert response.status_code == 200
    with postgres_client.app.state.db_session_factory() as session:
        for model, key in [
            (ProjectModel, "project_id"),
            (QuestionModel, "question_id"),
            (DatasetModel, "dataset_id"),
            (NoteModel, "note_id"),
            (SessionModel, "session_id"),
            (AnalysisModel, "analysis_id"),
            (ClaimModel, "claim_id"),
            (VisualizationModel, "viz_id"),
        ]:
            assert session.get(model, ids[key]) is None


def test_postgres_foreign_keys_reject_orphan_rows(
    postgres_client: TestClient,
) -> None:
    with (
        postgres_client.app.state.db_session_factory() as session,
        pytest.raises(IntegrityError),
    ):
        session.execute(
            insert(NoteModel).values(
                note_id=str(uuid4()),
                project_id=str(uuid4()),
                raw_content="orphan note",
                status="staged",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
        )
        session.commit()


def test_postgres_dolt_export_reads_repeatable_snapshot(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    ids = _create_representative_project_tree(postgres_client, postgres_admin_auth_headers)

    exports = dolt_mirror.export_tables(tmp_path / "postgres-export")

    by_name = {table.name: table for table in exports}
    assert by_name["projects"].row_count == 1
    assert by_name["questions"].row_count == 1
    assert by_name["datasets"].row_count == 1
    assert by_name["notes"].row_count == 1
    assert by_name["visualizations"].row_count == 1
    assert "users" not in by_name
    with by_name["projects"].csv_path.open(encoding="utf-8") as handle:
        assert ids["project_id"] in handle.read()


def test_postgres_json_and_boolean_round_trip(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
) -> None:
    ids = _create_representative_project_tree(postgres_client, postgres_admin_auth_headers)
    settings_response = postgres_client.patch(
        f"/projects/{ids['project_id']}/graph-draft-batch-settings",
        json={"enabled": True},
        headers=postgres_admin_auth_headers,
    )
    assert settings_response.status_code == 200

    with postgres_client.app.state.db_session_factory() as session:
        note = session.get(NoteModel, ids["note_id"])
        dataset = session.get(DatasetModel, ids["dataset_id"])
        settings = session.scalar(
            select(GraphDraftBatchSettingsModel).where(
                GraphDraftBatchSettingsModel.project_id == ids["project_id"]
            )
        )
        settings_count = session.scalar(select(func.count()).select_from(ProjectModel))

    assert note.note_metadata == {"backend": "postgres"}
    assert dataset.manifest_metadata == {"backend": "postgres"}
    assert settings.enabled is True
    assert settings_count == 1
