"""Real PostgreSQL race coverage for atomic evidence-bundle commands."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError

from lab_tracker.db_models import (
    AnalysisDatasetModel,
    AnalysisModel,
    ClaimAnalysisModel,
    ClaimDatasetModel,
    ClaimModel,
    ClaimQuestionModel,
    DatasetModel,
    DatasetQuestionLinkModel,
    EntityVersionModel,
    EvidenceBundleModel,
    NoteModel,
    NoteTargetModel,
    VisualizationClaimModel,
    VisualizationModel,
)
from lab_tracker.sqlalchemy_repository_parts.evidence_bundles import (
    EVIDENCE_BUNDLE_KEY_CONSTRAINT,
    SQLAlchemyEvidenceBundleRepository,
)

pytestmark = pytest.mark.postgres

_MUTATED_MODELS = (
    DatasetModel,
    DatasetQuestionLinkModel,
    AnalysisModel,
    AnalysisDatasetModel,
    ClaimModel,
    ClaimDatasetModel,
    ClaimAnalysisModel,
    ClaimQuestionModel,
    VisualizationModel,
    VisualizationClaimModel,
    NoteModel,
    NoteTargetModel,
    EntityVersionModel,
    EvidenceBundleModel,
)

_EVIDENCE_BUNDLE_REVISION = "0055_evidence_bundles"
_PRINCIPAL_CAPTURE_KEY_REVISION = "0054_project_capture_key_principal_scope"


def _alembic_config() -> Config:
    return Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))


def _single_script_head(config: Config) -> str:
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1
    return heads[0]


def _current_revision(database_url: str) -> str:
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
    finally:
        engine.dispose()
    assert isinstance(revision, str)
    return revision


def _identity_constraint_snapshot(database_url: str) -> dict[str, object]:
    engine = create_engine(database_url, future=True)
    try:
        inspector = inspect(engine)
        return {
            "user_primary_key": tuple(inspector.get_pk_constraint("users")["constrained_columns"]),
            "user_unique_constraints": {
                constraint["name"]: tuple(constraint["column_names"])
                for constraint in inspector.get_unique_constraints("users")
            },
            "project_primary_key": tuple(
                inspector.get_pk_constraint("projects")["constrained_columns"]
            ),
            "project_unique_constraints": {
                constraint["name"]: tuple(constraint["column_names"])
                for constraint in inspector.get_unique_constraints("projects")
            },
            "project_check_constraints": {
                constraint["name"] for constraint in inspector.get_check_constraints("projects")
            },
            "project_foreign_keys": {
                (
                    tuple(constraint["constrained_columns"]),
                    constraint["referred_table"],
                    tuple(constraint["referred_columns"]),
                )
                for constraint in inspector.get_foreign_keys("projects")
            },
        }
    finally:
        engine.dispose()


def _seed_project_and_question(
    client: TestClient,
    headers: dict[str, str],
) -> tuple[str, str]:
    project_response = client.post(
        "/projects",
        json={"name": "Concurrent evidence bundles"},
        headers=headers,
    )
    assert project_response.status_code == 201, project_response.text
    project_id = project_response.json()["data"]["project_id"]
    question_response = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Does the whole bundle survive a key race?",
            "question_type": "descriptive",
        },
        headers=headers,
    )
    assert question_response.status_code == 201, question_response.text
    return project_id, question_response.json()["data"]["question_id"]


def _bundle_payload(
    project_id: str,
    question_id: str,
    *,
    evidence_label: str,
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "primary_question_id": question_id,
        "dataset": {"kind": "create"},
        "analysis": {
            "kind": "create",
            "method_hash": "race-method-v1",
            "code_version": "race-code-v1",
        },
        "claim": {
            "kind": "create",
            "statement": f"The {evidence_label} bundle is atomic.",
            "confidence": 80,
        },
        "visualization": {
            "kind": "create",
            "viz_type": "line",
            "file_path": f"figures/{evidence_label}.png",
        },
        "source_note": {
            "kind": "create",
            "raw_content": f"Source material for {evidence_label}",
        },
        "dry_run": False,
        "idempotency_key": "postgres-evidence-bundle-race",
    }


def _synchronize_initial_key_misses(monkeypatch) -> None:  # noqa: ANN001
    original = SQLAlchemyEvidenceBundleRepository.get_by_key
    barrier = Barrier(2)
    counter_lock = Lock()
    initial_calls = 0

    def synchronized_get_by_key(
        self,
        *,
        project_id,
        created_by,
        idempotency_key,
    ):
        nonlocal initial_calls
        result = original(
            self,
            project_id=project_id,
            created_by=created_by,
            idempotency_key=idempotency_key,
        )
        with counter_lock:
            initial_calls += 1
            call_number = initial_calls
        if call_number <= 2:
            barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(
        SQLAlchemyEvidenceBundleRepository,
        "get_by_key",
        synchronized_get_by_key,
    )


def _row_counts(client: TestClient) -> dict[type, int]:
    with client.app.state.db_session_factory() as session:
        return {
            model: int(session.scalar(select(func.count()).select_from(model)) or 0)
            for model in _MUTATED_MODELS
        }


def _assert_one_committed_bundle(
    before: dict[type, int],
    after: dict[type, int],
) -> None:
    expected_increments = {
        DatasetModel: 1,
        DatasetQuestionLinkModel: 1,
        AnalysisModel: 1,
        AnalysisDatasetModel: 1,
        ClaimModel: 1,
        ClaimDatasetModel: 1,
        ClaimAnalysisModel: 1,
        ClaimQuestionModel: 1,
        VisualizationModel: 1,
        VisualizationClaimModel: 1,
        NoteModel: 1,
        NoteTargetModel: 1,
        EntityVersionModel: 1,
        EvidenceBundleModel: 1,
    }
    assert {model: after[model] - before[model] for model in _MUTATED_MODELS} == expected_increments


def test_0055_postgres_migration_cycle_preserves_existing_principals_and_projects(
    migrated_postgres_database_url: str,
) -> None:
    config = _alembic_config()
    current_head_revision = _single_script_head(config)
    user_id = str(uuid4())
    project_id = str(uuid4())
    bundle_id = str(uuid4())
    actor_id = user_id
    username = f"migration-user-{uuid4().hex[:8]}"
    capture_key = f"migration-project-{uuid4().hex}"
    engine = create_engine(migrated_postgres_database_url, future=True)

    try:
        command.downgrade(config, _PRINCIPAL_CAPTURE_KEY_REVISION)
        assert _current_revision(migrated_postgres_database_url) == (
            _PRINCIPAL_CAPTURE_KEY_REVISION
        )

        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users "
                    "(user_id, username, password_hash, role, created_at) VALUES "
                    "(:user_id, :username, 'unused-password-hash', 'scientist', "
                    "CURRENT_TIMESTAMP)"
                ),
                {"user_id": user_id, "username": username},
            )
            connection.execute(
                text(
                    "INSERT INTO projects "
                    "(project_id, name, description, status, client_capture_id, "
                    "created_by, created_by_user_id, created_at, updated_at) VALUES "
                    "(:project_id, 'Existing migration project', '', 'active', "
                    ":capture_key, :actor_id, :user_id, CURRENT_TIMESTAMP, "
                    "CURRENT_TIMESTAMP)"
                ),
                {
                    "project_id": project_id,
                    "capture_key": capture_key,
                    "actor_id": actor_id,
                    "user_id": user_id,
                },
            )

        baseline_constraints = _identity_constraint_snapshot(migrated_postgres_database_url)
        assert baseline_constraints["user_primary_key"] == ("user_id",)
        assert baseline_constraints["user_unique_constraints"] == {
            "uq_users_username": ("username",)
        }
        assert baseline_constraints["project_primary_key"] == ("project_id",)
        assert baseline_constraints["project_unique_constraints"] == {
            "uq_projects_creator_client_capture": (
                "created_by",
                "client_capture_id",
            )
        }
        assert (
            "ck_projects_client_capture_creator"
            in baseline_constraints["project_check_constraints"]
        )
        assert (
            ("created_by_user_id",),
            "users",
            ("user_id",),
        ) in baseline_constraints["project_foreign_keys"]

        command.upgrade(config, _EVIDENCE_BUNDLE_REVISION)
        assert _current_revision(migrated_postgres_database_url) == (_EVIDENCE_BUNDLE_REVISION)
        assert _identity_constraint_snapshot(migrated_postgres_database_url) == baseline_constraints

        inspector = inspect(engine)
        assert "evidence_bundles" in inspector.get_table_names()
        assert inspector.get_pk_constraint("evidence_bundles")["constrained_columns"] == [
            "bundle_id"
        ]
        evidence_unique_constraints = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("evidence_bundles")
        }
        assert evidence_unique_constraints[EVIDENCE_BUNDLE_KEY_CONSTRAINT] == (
            "project_id",
            "created_by",
            "idempotency_key",
        )
        evidence_project_fk = next(
            constraint
            for constraint in inspector.get_foreign_keys("evidence_bundles")
            if constraint["constrained_columns"] == ["project_id"]
        )
        assert evidence_project_fk["referred_table"] == "projects"
        assert evidence_project_fk["referred_columns"] == ["project_id"]
        assert evidence_project_fk["options"]["ondelete"] == "CASCADE"

        evidence_record_parameters = {
            "bundle_id": bundle_id,
            "project_id": project_id,
            "created_by": actor_id,
            "idempotency_key": "preserved-migration-key",
            "request_fingerprint": "a" * 64,
            "result": '{"status": "created"}',
        }
        insert_evidence_record = text(
            "INSERT INTO evidence_bundles "
            "(bundle_id, project_id, created_by, idempotency_key, "
            "request_fingerprint, result, created_at) VALUES "
            "(:bundle_id, :project_id, :created_by, :idempotency_key, "
            ":request_fingerprint, CAST(:result AS JSON), CURRENT_TIMESTAMP)"
        )
        with engine.begin() as connection:
            connection.execute(insert_evidence_record, evidence_record_parameters)

        duplicate_parameters = dict(evidence_record_parameters)
        duplicate_parameters["bundle_id"] = str(uuid4())
        with pytest.raises(IntegrityError) as duplicate_error, engine.begin() as connection:
            connection.execute(insert_evidence_record, duplicate_parameters)
        assert duplicate_error.value.orig.diag.constraint_name == (EVIDENCE_BUNDLE_KEY_CONSTRAINT)

        with engine.connect() as connection:
            preserved = (
                connection.execute(
                    text(
                        "SELECT users.username, projects.client_capture_id, "
                        "projects.created_by, projects.created_by_user_id "
                        "FROM projects JOIN users "
                        "ON projects.created_by_user_id = users.user_id "
                        "WHERE projects.project_id = :project_id"
                    ),
                    {"project_id": project_id},
                )
                .mappings()
                .one()
            )
            assert dict(preserved) == {
                "username": username,
                "client_capture_id": capture_key,
                "created_by": actor_id,
                "created_by_user_id": user_id,
            }
            assert (
                connection.scalar(
                    text("SELECT COUNT(*) FROM evidence_bundles WHERE bundle_id = :bundle_id"),
                    {"bundle_id": bundle_id},
                )
                == 1
            )

        command.downgrade(config, _PRINCIPAL_CAPTURE_KEY_REVISION)
        assert _current_revision(migrated_postgres_database_url) == (
            _PRINCIPAL_CAPTURE_KEY_REVISION
        )
        assert "evidence_bundles" not in inspect(engine).get_table_names()
        assert _identity_constraint_snapshot(migrated_postgres_database_url) == baseline_constraints
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT COUNT(*) FROM users WHERE user_id = :user_id"),
                    {"user_id": user_id},
                )
                == 1
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT COUNT(*) FROM projects "
                        "WHERE project_id = :project_id "
                        "AND created_by_user_id = :user_id"
                    ),
                    {"project_id": project_id, "user_id": user_id},
                )
                == 1
            )
    finally:
        # Always drop any partially-created 0055 state before restoring the
        # disposable PostgreSQL fixture to a clean head, even after an assertion.
        command.downgrade(config, _PRINCIPAL_CAPTURE_KEY_REVISION)
        command.upgrade(config, "head")
        assert _current_revision(migrated_postgres_database_url) == (
            current_head_revision
        )
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT COUNT(*) FROM evidence_bundles")) == 0
        engine.dispose()


def test_postgres_identical_bundle_race_reuses_winner_and_rolls_back_loser(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch,
) -> None:
    project_id, question_id = _seed_project_and_question(
        postgres_client,
        postgres_admin_auth_headers,
    )
    before = _row_counts(postgres_client)
    payload = _bundle_payload(project_id, question_id, evidence_label="identical")
    _synchronize_initial_key_misses(monkeypatch)

    def create_bundle(_index: int):
        return postgres_client.post(
            "/evidence-bundles",
            json=payload,
            headers=postgres_admin_auth_headers,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(create_bundle, range(2)))

    assert sorted(response.status_code for response in responses) == [200, 201]
    component_ids = [response.json()["data"]["component_ids"] for response in responses]
    assert component_ids[0] == component_ids[1]
    assert all(component_ids[0].values())
    _assert_one_committed_bundle(before, _row_counts(postgres_client))


def test_postgres_conflicting_bundle_race_returns_409_and_rolls_back_loser(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch,
) -> None:
    project_id, question_id = _seed_project_and_question(
        postgres_client,
        postgres_admin_auth_headers,
    )
    before = _row_counts(postgres_client)
    payloads = [
        _bundle_payload(project_id, question_id, evidence_label=label)
        for label in ("first", "second")
    ]
    _synchronize_initial_key_misses(monkeypatch)

    def create_bundle(payload: dict[str, Any]):
        return postgres_client.post(
            "/evidence-bundles",
            json=payload,
            headers=postgres_admin_auth_headers,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(create_bundle, payloads))

    assert sorted(response.status_code for response in responses) == [201, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["error"]["code"] == "conflict"
    _assert_one_committed_bundle(before, _row_counts(postgres_client))
