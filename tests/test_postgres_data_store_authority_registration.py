"""Real-PostgreSQL concurrency coverage for authority-bound store registration."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID

import pytest
from api_helpers import TEST_STORE_AUTHORITY_GRANT_ID
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from lab_tracker.db_models import DataStoreModel
from lab_tracker.sqlalchemy_repository_parts.data_stores import (
    SQLAlchemyDataStoreRepository,
)

pytestmark = pytest.mark.postgres

_CONFLICT_BODY = {
    "error": {
        "code": "conflict",
        "message": (
            "A data store with this name already exists in the selected scope."
        ),
        "issues": None,
    }
}


def test_postgres_concurrent_same_scope_name_creates_one_bound_row(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force both transactions to reach the unique-key insert together."""

    project_response = postgres_client.post(
        "/projects",
        json={"name": "Concurrent authority-bound store"},
        headers=postgres_admin_auth_headers,
    )
    assert project_response.status_code == 201, project_response.text
    project_id = project_response.json()["data"]["project_id"]

    original_insert = SQLAlchemyDataStoreRepository.insert
    before_insert = Barrier(2)

    def synchronized_insert(self, entity):  # noqa: ANN001, ANN202
        before_insert.wait(timeout=10)
        return original_insert(self, entity)

    monkeypatch.setattr(
        SQLAlchemyDataStoreRepository,
        "insert",
        synchronized_insert,
    )
    payload = {
        "project_id": project_id,
        "name": "concurrent-approved-store",
        "kind": "http",
        "root": "https://files.example.test/approved",
        "authority_grant_id": TEST_STORE_AUTHORITY_GRANT_ID,
    }

    def create_store(_index: int):
        return postgres_client.post(
            "/data-stores",
            json=payload,
            headers=postgres_admin_auth_headers,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(create_store, range(2)))

    assert sorted(response.status_code for response in responses) == [201, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json() == _CONFLICT_BODY
    winner = next(response for response in responses if response.status_code == 201)
    winner_data = winner.json()["data"]
    assert winner_data["authority_grant_id"] == TEST_STORE_AUTHORITY_GRANT_ID
    assert winner_data["authority_grant_fingerprint"].startswith("sag-v1-sha256:")

    with postgres_client.app.state.db_session_factory() as session:
        rows = list(
            session.scalars(
                select(DataStoreModel).where(
                    DataStoreModel.project_id == UUID(project_id),
                    DataStoreModel.name == payload["name"],
                )
            )
        )
        count = session.scalar(
            select(func.count())
            .select_from(DataStoreModel)
            .where(
                DataStoreModel.project_id == UUID(project_id),
                DataStoreModel.name == payload["name"],
            )
        )
    assert count == 1
    assert len(rows) == 1
    assert str(rows[0].store_id) == winner_data["store_id"]
    assert rows[0].authority_grant_id == TEST_STORE_AUTHORITY_GRANT_ID
    assert rows[0].authority_grant_fingerprint == winner_data[
        "authority_grant_fingerprint"
    ]
