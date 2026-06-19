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
    AnalysisDatasetModel,
    AnalysisModel,
    ClaimAnalysisModel,
    ClaimDatasetModel,
    ClaimModel,
    ClaimQuestionModel,
    DatasetModel,
    GraphDraftBatchSettingsModel,
    NoteModel,
    NoteTargetModel,
    ProjectModel,
    QuestionModel,
    SessionModel,
    VisualizationClaimModel,
    VisualizationModel,
)

pytestmark = pytest.mark.postgres


def _create_representative_project_tree(
    client: TestClient,
) -> dict[str, str]:
    project_id = str(uuid4())
    question_id = str(uuid4())
    dataset_id = str(uuid4())
    note_id = str(uuid4())
    session_id = str(uuid4())
    analysis_id = str(uuid4())
    claim_id = str(uuid4())
    viz_id = str(uuid4())

    with client.app.state.db_session_factory() as session:
        session.add(
            ProjectModel(
                project_id=project_id,
                name="Postgres cascade project",
                description="",
                status="active",
            )
        )
        session.flush()
        session.add(
            QuestionModel(
                question_id=question_id,
                project_id=project_id,
                text="Does Postgres preserve cascade semantics?",
                question_type="descriptive",
                status="active",
            )
        )
        session.flush()
        session.add(
            DatasetModel(
                dataset_id=dataset_id,
                project_id=project_id,
                commit_hash="sha256:pg",
                primary_question_id=question_id,
                manifest_files=[{"path": "data/postgres.csv", "checksum": "sha256:pg"}],
                manifest_metadata={"backend": "postgres"},
                status="committed",
            )
        )
        session.add(
            NoteModel(
                note_id=note_id,
                project_id=project_id,
                raw_content="Postgres JSON/boolean/FK representative note.",
                note_metadata={"backend": "postgres"},
                status="staged",
            )
        )
        session.add(
            SessionModel(
                session_id=session_id,
                project_id=project_id,
                session_type="scientific",
                status="active",
                primary_question_id=question_id,
            )
        )
        session.flush()
        session.add(
            NoteTargetModel(
                note_id=note_id,
                entity_type="dataset",
                entity_id=dataset_id,
            )
        )
        session.add(
            AnalysisModel(
                analysis_id=analysis_id,
                project_id=project_id,
                method_hash="postgres-method",
                code_version="postgres-v1",
                external_artifacts=[
                    {
                        "source_system": "s3",
                        "uri": "s3://lab-tracker/postgres-run",
                        "content_hash": "sha256:postgres-run",
                        "metadata": {"backend": "postgres"},
                    }
                ],
                status="committed",
            )
        )
        session.flush()
        session.add(AnalysisDatasetModel(analysis_id=analysis_id, dataset_id=dataset_id))
        session.add(
            ClaimModel(
                claim_id=claim_id,
                project_id=project_id,
                statement="The Postgres cascade path remains coherent.",
                confidence=0.9,
                status="supported",
            )
        )
        session.flush()
        session.add(ClaimDatasetModel(claim_id=claim_id, dataset_id=dataset_id))
        session.add(ClaimAnalysisModel(claim_id=claim_id, analysis_id=analysis_id))
        session.add(ClaimQuestionModel(claim_id=claim_id, question_id=question_id))
        session.add(
            VisualizationModel(
                viz_id=viz_id,
                analysis_id=analysis_id,
                viz_type="line",
                file_path="figures/postgres.png",
            )
        )
        session.flush()
        session.add(VisualizationClaimModel(viz_id=viz_id, claim_id=claim_id))
        session.commit()

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
    ids = _create_representative_project_tree(postgres_client)

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
    ids = _create_representative_project_tree(postgres_client)

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
    ids = _create_representative_project_tree(postgres_client)
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
