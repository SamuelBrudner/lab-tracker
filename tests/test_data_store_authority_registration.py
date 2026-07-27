from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session as OrmSession
from starlette.testclient import TestClient

from lab_tracker.db_models import DataStoreModel
from lab_tracker.repository import DataStoreForeignKeyRaceError
from lab_tracker.sqlalchemy_repository_parts.data_stores import (
    SQLAlchemyDataStoreRepository,
)
from lab_tracker.store_authority_registry import (
    STORE_AUTHORITY_CONFIG_SCHEMA,
    StoreAuthorityRegistry,
)

_CONFLICT_BODY = {
    "error": {
        "code": "conflict",
        "message": ("A data store with this name already exists in the selected scope."),
        "issues": None,
    }
}
_CONTEXT_CONFLICT_BODY = {
    "error": {
        "code": "conflict",
        "message": ("Data store registration context changed before it could be saved."),
        "issues": None,
    }
}
_INTERNAL_ERROR_BODY = {
    "error": {
        "code": "internal_server_error",
        "message": "Internal server error.",
        "issues": None,
    }
}


def _install_registry(client: TestClient, *grants: dict[str, object]) -> None:
    registry = StoreAuthorityRegistry.from_json(
        json.dumps(
            {
                "schema": STORE_AUTHORITY_CONFIG_SCHEMA,
                "grants": list(grants),
            },
            separators=(",", ":"),
        )
    )
    root_api = client.app.state.lab_tracker_api
    root_api._store_authority_registry = registry
    root_api.data_stores.store_authority_registry = registry


def _create_project(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str,
    group_id: str | None = None,
) -> str:
    payload: dict[str, str] = {"name": name}
    if group_id is not None:
        payload["group_id"] = group_id
    response = client.post("/projects", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["project_id"]


def _create_group(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str,
) -> str:
    response = client.post("/groups", json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["group_id"]


@pytest.mark.parametrize("scope_kind", ("project", "group"))
def test_exact_authority_binding_round_trips_server_derived_proof(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    scope_kind: str,
) -> None:
    if scope_kind == "project":
        scope_id = _create_project(
            client,
            admin_auth_headers,
            name="Bound project store",
        )
        scope = {"project_id": scope_id}
    else:
        scope_id = _create_group(
            client,
            admin_auth_headers,
            name="Bound group store",
        )
        scope = {"group_id": scope_id}
    grant_id = f"{scope_kind}-http"
    _install_registry(
        client,
        {
            "grant_id": grant_id,
            "scope": scope,
            "kind": "http",
            "root": "https://files.example.test/approved",
            "capabilities": ["bytes_by_path", "byte_range"],
        },
    )

    response = client.post(
        "/data-stores",
        json={
            **scope,
            "name": "bound-http",
            "kind": "http",
            "root": "https://files.example.test/approved/experiment",
            "authority_grant_id": grant_id,
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == 201, response.text
    created = response.json()["data"]
    assert created["authority_grant_id"] == grant_id
    assert created["authority_grant_fingerprint"].startswith("sag-v1-sha256:")
    fetched = client.get(
        f"/data-stores/{created['store_id']}",
        headers=admin_auth_headers,
    )
    assert fetched.status_code == 200
    assert fetched.json()["data"] == created


def test_sqlite_concurrent_same_scope_name_creates_one_bound_row_without_leakage(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The scoped-name constraint must resolve a real concurrent HTTP race."""

    project_id = _create_project(
        client,
        admin_auth_headers,
        name="Concurrent SQLite authority-bound store",
    )
    sensitive_root = "https://operator-secret.example.test/private"
    sensitive_grant_id = "sqlite-race-secret-grant"
    _install_registry(
        client,
        {
            "grant_id": sensitive_grant_id,
            "scope": {"project_id": project_id},
            "kind": "http",
            "root": sensitive_root,
            "capabilities": ["bytes_by_path", "byte_range"],
        },
    )

    original_reserve = SQLAlchemyDataStoreRepository.reserve_registration_write
    before_reserve = Barrier(2)

    def synchronized_reserve(self):  # noqa: ANN001, ANN202
        before_reserve.wait(timeout=10)
        return original_reserve(self)

    monkeypatch.setattr(
        SQLAlchemyDataStoreRepository,
        "reserve_registration_write",
        synchronized_reserve,
    )
    payload = {
        "project_id": project_id,
        "name": "concurrent-approved-store",
        "kind": "http",
        "root": sensitive_root,
        "authority_grant_id": sensitive_grant_id,
    }

    def create_store(_index: int):
        return client.post(
            "/data-stores",
            json=payload,
            headers=admin_auth_headers,
        )

    with (
        caplog.at_level(logging.WARNING, logger="lab_tracker.routes.errors"),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        responses = list(executor.map(create_store, range(2)))

    assert sorted(response.status_code for response in responses) == [201, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json() == _CONFLICT_BODY
    winner = next(response for response in responses if response.status_code == 201)
    winner_data = winner.json()["data"]
    fingerprint = winner_data["authority_grant_fingerprint"]
    assert winner_data["authority_grant_id"] == sensitive_grant_id
    assert fingerprint.startswith("sag-v1-sha256:")

    conflict_surface = f"{conflict.text}\n{caplog.text}"
    assert sensitive_root not in conflict_surface
    assert sensitive_grant_id not in conflict_surface
    assert fingerprint not in conflict_surface

    with client.app.state.db_session_factory() as session:
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
    assert rows[0].authority_grant_id == sensitive_grant_id
    assert rows[0].authority_grant_fingerprint == fingerprint


def test_foreign_key_race_port_error_returns_safe_conflict(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    project_id = _create_project(
        client,
        admin_auth_headers,
        name="Foreign-key registration race",
    )
    sensitive_root = "https://fk-race-secret.example.test/private"
    sensitive_grant_id = "fk-race-secret-grant"
    _install_registry(
        client,
        {
            "grant_id": sensitive_grant_id,
            "scope": {"project_id": project_id},
            "kind": "http",
            "root": sensitive_root,
            "capabilities": ["bytes_by_path", "byte_range"],
        },
    )
    fingerprints: list[str] = []

    def fail_with_foreign_key_race(self, entity):  # noqa: ANN001, ANN202
        fingerprint = entity.authority_grant_fingerprint
        assert fingerprint is not None
        fingerprints.append(fingerprint)
        raw_error = IntegrityError(
            "INSERT INTO data_stores (...) VALUES (...)",
            {
                "root": sensitive_root,
                "authority_grant_id": sensitive_grant_id,
                "authority_grant_fingerprint": fingerprint,
            },
            RuntimeError("foreign key constraint failed"),
        )
        try:
            raise raw_error
        except IntegrityError as exc:
            raise DataStoreForeignKeyRaceError from exc

    monkeypatch.setattr(
        SQLAlchemyDataStoreRepository,
        "insert",
        fail_with_foreign_key_race,
    )
    payload = {
        "project_id": project_id,
        "name": "foreign-key-race-store",
        "kind": "http",
        "root": sensitive_root,
        "authority_grant_id": sensitive_grant_id,
    }
    route_logger = logging.getLogger("lab_tracker.routes.errors")
    monkeypatch.setattr(route_logger, "disabled", False)
    route_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger=route_logger.name):
            response = client.post(
                "/data-stores",
                json=payload,
                headers=admin_auth_headers,
            )
    finally:
        route_logger.removeHandler(caplog.handler)

    assert response.status_code == 409
    assert response.json() == _CONTEXT_CONFLICT_BODY
    assert len(fingerprints) == 1
    fingerprint = fingerprints[0]
    assert fingerprint.startswith("sag-v1-sha256:")
    safe_surface = f"{response.text}\n{caplog.text}"
    assert sensitive_root not in safe_surface
    assert sensitive_grant_id not in safe_surface
    assert fingerprint not in safe_surface
    assert caplog.records
    assert all(record.exc_info is None for record in caplog.records)

    with client.app.state.db_session_factory() as session:
        count = session.scalar(
            select(func.count())
            .select_from(DataStoreModel)
            .where(
                DataStoreModel.project_id == UUID(project_id),
                DataStoreModel.name == payload["name"],
            )
        )
    assert count == 0


def test_unknown_insert_failure_returns_safe_internal_error(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    project_id = _create_project(
        client,
        admin_auth_headers,
        name="Unknown registration failure",
    )
    sensitive_root = "/approved/unknown-insert-secret"
    sensitive_credential = "unknown-insert-secret-credential"
    sensitive_grant_id = "unknown-insert-secret-grant"
    _install_registry(
        client,
        {
            "grant_id": sensitive_grant_id,
            "scope": {"project_id": project_id},
            "kind": "onedrive",
            "root": "/approved",
            "capabilities": ["bytes_by_path"],
            "remote": sensitive_credential,
            "credential_mode": "credential_ref",
        },
    )
    original_flush = OrmSession.flush
    fingerprints: list[str] = []

    def fail_data_store_flush(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        row = next(
            (candidate for candidate in self.new if isinstance(candidate, DataStoreModel)),
            None,
        )
        if row is None:
            return original_flush(self, *args, **kwargs)
        fingerprint = row.authority_grant_fingerprint
        assert fingerprint is not None
        fingerprints.append(fingerprint)
        raise OperationalError(
            "INSERT INTO data_stores (...) VALUES (...)",
            {
                "root": sensitive_root,
                "credential_ref": sensitive_credential,
                "authority_grant_id": sensitive_grant_id,
                "authority_grant_fingerprint": fingerprint,
            },
            RuntimeError("forced unknown data-store insert failure"),
        )

    monkeypatch.setattr(OrmSession, "flush", fail_data_store_flush)
    payload = {
        "project_id": project_id,
        "name": "unknown-failure-store",
        "kind": "onedrive",
        "root": sensitive_root,
        "credential_ref": sensitive_credential,
        "capabilities": ["bytes_by_path"],
        "authority_grant_id": sensitive_grant_id,
    }
    route_logger = logging.getLogger("lab_tracker.routes.errors")
    monkeypatch.setattr(route_logger, "disabled", False)
    route_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.ERROR, logger=route_logger.name):
            response = client.post(
                "/data-stores",
                json=payload,
                headers=admin_auth_headers,
            )
    finally:
        route_logger.removeHandler(caplog.handler)

    assert response.status_code == 500
    assert response.json() == _INTERNAL_ERROR_BODY
    assert len(fingerprints) == 1
    fingerprint = fingerprints[0]
    assert fingerprint.startswith("sag-v1-sha256:")
    safe_surface = f"{response.text}\n{caplog.text}"
    for secret in (
        sensitive_root,
        sensitive_credential,
        sensitive_grant_id,
        fingerprint,
    ):
        assert secret not in safe_surface
    assert caplog.records
    assert all(record.exc_info is None for record in caplog.records)

    with client.app.state.db_session_factory() as session:
        count = session.scalar(
            select(func.count())
            .select_from(DataStoreModel)
            .where(
                DataStoreModel.project_id == UUID(project_id),
                DataStoreModel.name == payload["name"],
            )
        )
    assert count == 0


@pytest.mark.parametrize(
    "case",
    (
        "missing",
        "unknown",
        "wrong_project_scope",
        "group_grant_for_project",
        "wrong_kind",
        "outside_boundary",
        "capability_expansion",
        "credential_mismatch",
    ),
)
def test_registration_authority_mismatches_share_one_opaque_403(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    case: str,
) -> None:
    group_id = (
        _create_group(
            client,
            admin_auth_headers,
            name="Applicable group authority",
        )
        if case == "group_grant_for_project"
        else None
    )
    project_id = _create_project(
        client,
        admin_auth_headers,
        name=f"Authority mismatch {case}",
        group_id=group_id,
    )
    grant_scope: dict[str, str] = {"project_id": project_id}
    grant: dict[str, object] = {
        "grant_id": "approved-grant",
        "scope": grant_scope,
        "kind": "http",
        "root": "https://files.example.test/approved",
        "capabilities": ["bytes_by_path", "byte_range"],
    }
    payload: dict[str, object] = {
        "project_id": project_id,
        "name": "candidate",
        "kind": "http",
        "root": "https://files.example.test/approved/experiment",
        "authority_grant_id": "approved-grant",
    }
    if case == "missing":
        payload.pop("authority_grant_id")
    elif case == "unknown":
        payload["authority_grant_id"] = "unknown-grant"
    elif case == "wrong_project_scope":
        grant["scope"] = {"project_id": str(uuid4())}
    elif case == "group_grant_for_project":
        assert group_id is not None
        grant["scope"] = {"group_id": group_id}
    elif case == "wrong_kind":
        grant.update(
            {
                "kind": "local_fs",
                "root": "/operator/approved",
                "capabilities": ["bytes_by_path"],
            }
        )
    elif case == "outside_boundary":
        payload["root"] = "https://files.example.test/disjoint"
    elif case == "capability_expansion":
        grant["capabilities"] = ["bytes_by_path"]
        payload["capabilities"] = ["bytes_by_path", "byte_range"]
    elif case == "credential_mismatch":
        grant.update(
            {
                "kind": "onedrive",
                "root": "/approved",
                "capabilities": ["bytes_by_path"],
                "remote": "approved-remote",
                "credential_mode": "credential_ref",
            }
        )
        payload.update(
            {
                "kind": "onedrive",
                "root": "/approved/experiment",
                "credential_ref": "other-remote",
                "capabilities": ["bytes_by_path"],
            }
        )
    _install_registry(client, grant)

    response = client.post(
        "/data-stores",
        json=payload,
        headers=admin_auth_headers,
    )

    assert response.status_code == 403
    assert response.json() == {
        "error": {
            "code": "store_authority_denied",
            "message": "Data store authority is unavailable.",
            "issues": None,
        }
    }
    listed = client.get(
        "/data-stores",
        params={"project_id": project_id},
        headers=admin_auth_headers,
    )
    assert listed.status_code == 200
    assert listed.json()["data"] == []


def test_caller_cannot_supply_a_grant_fingerprint(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _create_project(
        client,
        admin_auth_headers,
        name="Forged grant fingerprint",
    )

    response = client.post(
        "/data-stores",
        json={
            "project_id": project_id,
            "name": "forged",
            "kind": "http",
            "root": "https://files.example.test/approved",
            "authority_grant_id": "approved-grant",
            "authority_grant_fingerprint": "sag-v1-sha256:" + "0" * 64,
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_error"
