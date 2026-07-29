from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from api_helpers import TEST_STORE_AUTHORITY_GRANT_ID
from http_security_fakes import FakeAddressResolver, FakeSafeHttpClient
from starlette.testclient import TestClient

import lab_tracker.routes.errors as route_errors
from lab_tracker.artifact_resolution import LocalFilesystemResolver, ResolverRegistry
from lab_tracker.bounded_subprocess import BoundedSubprocessExecutor, ProcessResult
from lab_tracker.data_store_definition import (
    DATA_STORE_CREDENTIAL_REF_MAX_LENGTH,
    DATA_STORE_ENDPOINT_MAX_LENGTH,
    DATA_STORE_NAME_MAX_LENGTH,
    DATA_STORE_ROOT_MAX_LENGTH,
)
from lab_tracker.http_store_health import HttpStoreHealthProbe
from lab_tracker.local_filesystem_authority import LocalFilesystemAuthority
from lab_tracker.local_filesystem_operations import BoundedLocalFilesystemOperations
from lab_tracker.local_store_health import LocalStoreHealthProbe
from lab_tracker.models import DataStore, StoreCapability, StoreKind
from lab_tracker.outbound_http import OutboundHttpPolicy
from lab_tracker.rclone_remote_policy import RcloneRemotePolicy
from lab_tracker.rclone_store_health import RcloneStoreHealthProbe
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository
from lab_tracker.sqlalchemy_repository_parts.data_stores import (
    SQLAlchemyDataStoreRepository,
)
from lab_tracker.store_health import (
    STORE_HEALTH_PROBE_UNAVAILABLE_MESSAGE,
    CachedStoreHealthProbe,
)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _install_local_registry(client: TestClient, allowed_root: Path) -> None:
    client.app.state.resolver_registry = ResolverRegistry(
        [LocalFilesystemResolver(allowed_roots=[allowed_root])]
    )


def _install_local_health(client: TestClient, allowed_root: Path) -> None:
    operations = BoundedLocalFilesystemOperations(
        authority=LocalFilesystemAuthority.from_roots([allowed_root]),
        executor=BoundedSubprocessExecutor(),
    )
    client.app.state.store_health_checker = CachedStoreHealthProbe(
        LocalStoreHealthProbe(
            inspector=operations,
        )
    )


def _create_project(client: TestClient, headers: dict[str, str], name: str) -> str:
    response = client.post("/projects", json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["project_id"]


def _create_dataset_with_artifact(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: str,
    uri: str,
    content_hash: str,
) -> str:
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Which artifact resolves?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    response = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": question_id,
            "status": "committed",
            "commit_manifest": {
                "external_artifacts": [
                    {"source_system": "store", "uri": uri, "content_hash": content_hash}
                ]
            },
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["dataset_id"]


def _store_payload(project_id: str, **overrides) -> dict:
    payload = {
        "project_id": project_id,
        "name": "lab-onedrive",
        "kind": "onedrive",
        "root": "/OneDrive/experiments",
        "authority_grant_id": TEST_STORE_AUTHORITY_GRANT_ID,
        "is_default": True,
    }
    payload.update(overrides)
    if "credential_ref" not in overrides:
        payload["credential_ref"] = (
            "onedrive-remote" if payload["kind"] == "onedrive" else None
        )
    return payload


def _create_group(client: TestClient, headers: dict[str, str], name: str) -> str:
    response = client.post("/groups", json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["group_id"]


def _create_project_in_group(
    client: TestClient, headers: dict[str, str], name: str, group_id: str
) -> str:
    response = client.post(
        "/projects", json={"name": name, "group_id": group_id}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["project_id"]


def _data_store_not_found_body() -> dict[str, object]:
    return {
        "error": {
            "code": "not_found",
            "message": "Data store does not exist.",
            "issues": None,
        }
    }


def _store_authority_denied_body() -> dict[str, object]:
    return {
        "error": {
            "code": "store_authority_denied",
            "message": "Data store authority is unavailable.",
            "issues": None,
        }
    }


def _seed_legacy_data_store(
    client: TestClient,
    *,
    project_id: str,
    name: str,
    kind: StoreKind,
    root: str,
    endpoint: str | None = None,
    credential_ref: str | None = None,
) -> str:
    """Persist one pre-validation row without routing through create semantics."""

    store = DataStore(
        store_id=uuid4(),
        project_id=UUID(project_id),
        name=name,
        kind=kind,
        root=root,
        endpoint=endpoint,
        credential_ref=credential_ref,
    )
    with client.app.state.db_session_factory() as session:
        repository = SQLAlchemyLabTrackerRepository(session)
        repository.data_stores.insert(store)
        session.commit()
    return str(store.store_id)


def test_create_and_get_data_store(client, admin_auth_headers):
    project_id = _create_project(client, admin_auth_headers, "Store project")

    created = client.post(
        "/data-stores", json=_store_payload(project_id), headers=admin_auth_headers
    )
    assert created.status_code == 201, created.text
    body = created.json()["data"]
    assert body["name"] == "lab-onedrive"
    assert body["kind"] == "onedrive"
    assert body["is_default"] is True
    assert body["authority_grant_id"] == TEST_STORE_AUTHORITY_GRANT_ID
    assert body["authority_grant_fingerprint"].startswith("sag-v1-sha256:")
    # capabilities defaulted from kind
    assert "bytes_by_path" in body["capabilities"]
    store_id = body["store_id"]

    fetched = client.get(f"/data-stores/{store_id}", headers=admin_auth_headers)
    assert fetched.status_code == 200
    assert fetched.json()["data"] == body


def test_legacy_unbound_store_remains_list_and_get_readable(
    client,
    admin_auth_headers,
):
    project_id = _create_project(
        client,
        admin_auth_headers,
        "Legacy unbound metadata",
    )
    store_id = _seed_legacy_data_store(
        client,
        project_id=project_id,
        name="legacy-unbound-http",
        kind=StoreKind.HTTP,
        root="https://legacy.example.test/metadata-only",
    )

    fetched = client.get(
        f"/data-stores/{store_id}",
        headers=admin_auth_headers,
    )
    listed = client.get(
        "/data-stores",
        params={"project_id": project_id},
        headers=admin_auth_headers,
    )

    assert fetched.status_code == 200, fetched.text
    fetched_store = fetched.json()["data"]
    assert fetched_store["store_id"] == store_id
    assert fetched_store["authority_grant_id"] is None
    assert fetched_store["authority_grant_fingerprint"] is None
    assert listed.status_code == 200, listed.text
    [listed_store] = listed.json()["data"]
    assert listed_store == fetched_store


@pytest.mark.parametrize(
    "capabilities",
    (
        [],
        ["bytes_by_path", "bytes_by_path"],
    ),
)
def test_create_data_store_returns_one_opaque_denial_for_missing_unknown_and_bad_caps(
    client,
    admin_auth_headers,
    capabilities,
):
    project_id = _create_project(client, admin_auth_headers, "Denied store authority")
    baseline = _store_payload(
        project_id,
        name="denied-authority",
        kind="http",
        root="https://files.example.test/base",
        is_default=False,
        capabilities=capabilities,
    )
    candidates = (
        {key: value for key, value in baseline.items() if key != "authority_grant_id"},
        {**baseline, "authority_grant_id": "unknown-authority"},
        baseline,
    )

    for payload in candidates:
        response = client.post(
            "/data-stores",
            json=payload,
            headers=admin_auth_headers,
        )
        assert response.status_code == 403
        assert response.json() == _store_authority_denied_body()

    listed = client.get(
        "/data-stores",
        params={"project_id": project_id},
        headers=admin_auth_headers,
    )
    assert listed.status_code == 200
    assert listed.json()["data"] == []


def test_create_data_store_defaults_capabilities_for_s3(client, admin_auth_headers):
    project_id = _create_project(client, admin_auth_headers, "S3 project")
    payload = _store_payload(
        project_id,
        name="s3-archive",
        kind="s3",
        root="/lab-archive",
        is_default=False,
    )

    created = client.post("/data-stores", json=payload, headers=admin_auth_headers)

    assert created.status_code == 201
    caps = created.json()["data"]["capabilities"]
    assert "versioned_snapshot" in caps


def test_list_data_stores_scoped_to_project(client, admin_auth_headers):
    project_id = _create_project(client, admin_auth_headers, "List project")
    client.post(
        "/data-stores", json=_store_payload(project_id), headers=admin_auth_headers
    )
    client.post(
        "/data-stores",
        json=_store_payload(
            project_id,
            name="s3-archive",
            kind="s3",
            root="/archive",
            is_default=False,
        ),
        headers=admin_auth_headers,
    )

    listed = client.get(
        "/data-stores", params={"project_id": project_id}, headers=admin_auth_headers
    )
    assert listed.status_code == 200
    names = {item["name"] for item in listed.json()["data"]}
    assert names == {"lab-onedrive", "s3-archive"}


def test_duplicate_store_name_in_project_conflicts(client, admin_auth_headers):
    project_id = _create_project(client, admin_auth_headers, "Dup project")
    first = client.post(
        "/data-stores", json=_store_payload(project_id), headers=admin_auth_headers
    )
    assert first.status_code == 201

    duplicate = client.post(
        "/data-stores",
        json=_store_payload(project_id, root="/elsewhere"),
        headers=admin_auth_headers,
    )
    assert duplicate.status_code == 409


def test_group_store_inherited_by_project_listing(client, admin_auth_headers):
    group_id = _create_group(client, admin_auth_headers, "Lab group")
    project_id = _create_project_in_group(client, admin_auth_headers, "Member project", group_id)

    created = client.post(
        "/data-stores",
        json={
            "group_id": group_id,
            "name": "lab-shared-s3",
            "kind": "s3",
            "root": "/lab-shared",
            "authority_grant_id": TEST_STORE_AUTHORITY_GRANT_ID,
        },
        headers=admin_auth_headers,
    )
    assert created.status_code == 201, created.text

    # The project lists the inherited group store as part of its effective set.
    listed = client.get(
        "/data-stores", params={"project_id": project_id}, headers=admin_auth_headers
    )
    assert listed.status_code == 200
    names = {item["name"] for item in listed.json()["data"]}
    assert "lab-shared-s3" in names


def test_group_store_resolves_for_project(client, admin_auth_headers, tmp_path):
    data = b"shared lab object"
    (tmp_path / "exp").mkdir()
    (tmp_path / "exp" / "x.txt").write_bytes(data)
    _install_local_registry(client, tmp_path)

    group_id = _create_group(client, admin_auth_headers, "Resolving lab")
    project_id = _create_project_in_group(client, admin_auth_headers, "Lab project", group_id)
    created = client.post(
        "/data-stores",
        json={
            "group_id": group_id,
            "name": "lab-fs",
            "kind": "local_fs",
            "root": str(tmp_path),
            "authority_grant_id": TEST_STORE_AUTHORITY_GRANT_ID,
        },
        headers=admin_auth_headers,
    )
    assert created.status_code == 201, created.text
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=project_id,
        uri="store://lab-fs/exp/x.txt",
        content_hash=_sha256(data),
    )

    response = client.post(
        "/external-artifacts/resolve",
        json={"entity_type": "dataset", "entity_id": dataset_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    # Candidate selection still sees the inherited group store, but registered
    # store use remains closed until grant revalidation and retained local
    # authority land.
    assert body["status"] == "unresolved"
    assert body["source_system"] == "store"
    assert body["uri"] == "store://[redacted]"
    assert body["content_base64"] is None
    assert body["detail"] == "Store artifact could not be resolved."


def test_project_store_shadows_same_name_inherited_group_store_before_fail_closed(
    client,
    admin_auth_headers,
    monkeypatch,
):
    group_id = _create_group(client, admin_auth_headers, "Shadowed store group")
    project_id = _create_project_in_group(
        client,
        admin_auth_headers,
        "Shadowing project",
        group_id,
    )
    group_store = client.post(
        "/data-stores",
        json={
            "group_id": group_id,
            "name": "shared-name",
            "kind": "http",
            "root": "https://group-secret.example.test/base",
            "authority_grant_id": TEST_STORE_AUTHORITY_GRANT_ID,
        },
        headers=admin_auth_headers,
    )
    project_store = client.post(
        "/data-stores",
        json={
            "project_id": project_id,
            "name": "shared-name",
            "kind": "http",
            "root": "https://project-secret.example.test/base",
            "authority_grant_id": TEST_STORE_AUTHORITY_GRANT_ID,
        },
        headers=admin_auth_headers,
    )
    assert group_store.status_code == 201, group_store.text
    assert project_store.status_code == 201, project_store.text
    project_store_id = project_store.json()["data"]["store_id"]
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=project_id,
        uri="store://shared-name/secret.bin",
        content_hash=_sha256(b"unavailable"),
    )
    original_get_by_name = SQLAlchemyDataStoreRepository.get_by_name
    selected_store_ids: list[str] = []

    def tracked_get_by_name(self, selected_project_id, name):  # noqa: ANN001, ANN202
        selected = original_get_by_name(self, selected_project_id, name)
        if selected is not None:
            selected_store_ids.append(str(selected.store_id))
        return selected

    monkeypatch.setattr(
        SQLAlchemyDataStoreRepository,
        "get_by_name",
        tracked_get_by_name,
    )

    response = client.post(
        "/external-artifacts/resolve",
        json={"entity_type": "dataset", "entity_id": dataset_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200, response.text
    assert selected_store_ids == [project_store_id]
    body = response.json()["data"]
    assert body["status"] == "unresolved"
    assert body["source_system"] == "store"
    assert body["uri"] == "store://[redacted]"
    assert body["detail"] == "Store artifact could not be resolved."
    assert "group-secret" not in response.text
    assert "project-secret" not in response.text


def test_create_group_store_requires_group_owner(client, scoped_project_member, admin_auth_headers):
    group_id = _create_group(client, admin_auth_headers, "Owned lab")

    # The scoped member is not a member/owner of this group.
    response = client.post(
        "/data-stores",
        json={"group_id": group_id, "name": "lab-fs", "kind": "s3", "root": "s3://x"},
        headers=scoped_project_member.member_headers,
    )
    assert response.status_code == 401


def test_create_data_store_requires_exactly_one_scope(client, admin_auth_headers):
    project_id = _create_project(client, admin_auth_headers, "Scope project")

    neither = client.post(
        "/data-stores",
        json={"name": "x", "kind": "s3", "root": "s3://x"},
        headers=admin_auth_headers,
    )
    assert neither.status_code == 422

    both = client.post(
        "/data-stores",
        json={
            "project_id": project_id,
            "group_id": project_id,
            "name": "x",
            "kind": "s3",
            "root": "s3://x",
        },
        headers=admin_auth_headers,
    )
    assert both.status_code == 422


def test_data_store_create_openapi_publishes_storage_bounds(client):
    openapi = client.get("/openapi.json").json()
    create_schema = openapi["components"]["schemas"]["DataStoreCreate"]
    schema = create_schema["properties"]

    assert schema["name"]["maxLength"] == DATA_STORE_NAME_MAX_LENGTH
    assert schema["root"]["maxLength"] == DATA_STORE_ROOT_MAX_LENGTH
    assert next(
        item["maxItems"]
        for item in schema["capabilities"]["anyOf"]
        if "maxItems" in item
    ) == len(StoreCapability)
    assert next(
        item["maxLength"]
        for item in schema["authority_grant_id"]["anyOf"]
        if "maxLength" in item
    ) == 128
    assert "authority_grant_id" not in create_schema.get("required", [])
    assert "authority_grant_fingerprint" not in schema
    assert next(
        item["maxLength"]
        for item in schema["endpoint"]["anyOf"]
        if "maxLength" in item
    ) == DATA_STORE_ENDPOINT_MAX_LENGTH
    assert next(
        item["maxLength"]
        for item in schema["credential_ref"]["anyOf"]
        if "maxLength" in item
    ) == DATA_STORE_CREDENTIAL_REF_MAX_LENGTH
    response_schema = openapi["components"]["schemas"]["DataStore"]["properties"]
    assert "authority_grant_id" in response_schema
    assert "authority_grant_fingerprint" in response_schema


def test_request_validation_logging_never_records_oversized_store_secret(
    client,
    admin_auth_headers,
    monkeypatch,
):
    project_id = _create_project(client, admin_auth_headers, "Schema-bound store")
    secret = "request-validation-secret-must-not-leak"
    oversized_root = (
        f"https://files.example/{secret}/"
        + "x" * DATA_STORE_ROOT_MAX_LENGTH
    )
    warnings: list[str] = []

    def record_warning(message, *args):
        warnings.append(message % args)

    monkeypatch.setattr(route_errors._logger, "warning", record_warning)
    response = client.post(
        "/data-stores",
        json=_store_payload(
            project_id,
            name="bounded-http",
            kind="http",
            root=oversized_root,
            credential_ref=None,
        ),
        headers=admin_auth_headers,
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "request_validation_error"
    assert secret not in response.text
    assert warnings == [
        "Handled HTTP error: method=POST path=/data-stores status_code=422 "
        "code=request_validation_error detail=Request validation failed."
    ]
    assert secret not in warnings[0]
    assert oversized_root not in warnings[0]


def test_create_data_store_authorizes_before_semantic_definition_validation(
    client,
    scoped_project_member,
    caplog,
):
    secret = "unauthorized-definition-secret-must-not-leak"
    with caplog.at_level(logging.WARNING, logger="lab_tracker.routes.errors"):
        response = client.post(
            "/data-stores",
            json={
                "project_id": scoped_project_member.visible_project_id,
                "name": " padded-name ",
                "kind": "http",
                "root": f"https://operator:{secret}@files.example/private",
            },
            headers=scoped_project_member.member_headers,
        )

    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "auth_error"
    assert secret not in response.text
    assert secret not in caplog.text


def test_semantic_validation_precedes_duplicate_lookup_and_redacts_secret(
    client,
    admin_auth_headers,
    caplog,
):
    project_id = _create_project(client, admin_auth_headers, "Validation order")
    created = client.post(
        "/data-stores",
        json={
            "project_id": project_id,
            "name": "remote-http",
            "kind": "http",
            "root": "https://files.example/base",
            "authority_grant_id": TEST_STORE_AUTHORITY_GRANT_ID,
        },
        headers=admin_auth_headers,
    )
    assert created.status_code == 201, created.text
    secret = "duplicate-validation-secret-must-not-leak"

    with caplog.at_level(logging.WARNING, logger="lab_tracker.routes.errors"):
        rejected = client.post(
            "/data-stores",
            json={
                "project_id": project_id,
                "name": "remote-http",
                "kind": "http",
                "root": f"https://operator:{secret}@files.example/private",
            },
            headers=admin_auth_headers,
        )

    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["error"]["code"] == "validation_error"
    assert secret not in rejected.text
    assert secret not in caplog.text
    listed = client.get(
        "/data-stores",
        params={"project_id": project_id},
        headers=admin_auth_headers,
    )
    assert [item["store_id"] for item in listed.json()["data"]] == [
        created.json()["data"]["store_id"]
    ]


def test_supported_remote_definitions_round_trip_canonically(
    client,
    admin_auth_headers,
):
    project_id = _create_project(client, admin_auth_headers, "Canonical stores")
    definitions = (
        (
            {
                "name": "remote-http",
                "kind": "http",
                "root": "HTTPS://Files.Example:443/base",
            },
            {
                "name": "remote-http",
                "kind": "http",
                "root": "https://files.example/base/",
                "endpoint": None,
                "credential_ref": None,
            },
        ),
        (
            {
                "name": "relative archive",
                "kind": "rclone",
                "root": "experiments/current",
                "credential_ref": "approved remote",
            },
            {
                "name": "relative archive",
                "kind": "rclone",
                "root": "experiments/current",
                "endpoint": None,
                "credential_ref": "approved remote",
            },
        ),
        (
            {
                "name": "rooted-archive",
                "kind": "onedrive",
                "root": "/experiments/current",
                "credential_ref": "lab-onedrive",
            },
            {
                "name": "rooted-archive",
                "kind": "onedrive",
                "root": "/experiments/current",
                "endpoint": None,
                "credential_ref": "lab-onedrive",
            },
        ),
        (
            {
                "name": "analysis-repository",
                "kind": "git",
                "root": "HTTPS://Git.Example:443/lab/repository.git",
            },
            {
                "name": "analysis-repository",
                "kind": "git",
                "root": "https://git.example/lab/repository.git",
                "endpoint": None,
                "credential_ref": None,
            },
        ),
    )

    for submitted, expected in definitions:
        response = client.post(
            "/data-stores",
            json={
                "project_id": project_id,
                "authority_grant_id": TEST_STORE_AUTHORITY_GRANT_ID,
                **submitted,
            },
            headers=admin_auth_headers,
        )

        assert response.status_code == 201, response.text
        body = response.json()["data"]
        assert {
            field: body[field]
            for field in ("name", "kind", "root", "endpoint", "credential_ref")
        } == expected


@pytest.mark.parametrize("endpoint", ("", " ", "https://dead.example/"))
def test_present_endpoint_is_rejected_without_truthiness_collapse(
    client,
    admin_auth_headers,
    endpoint,
):
    project_id = _create_project(
        client,
        admin_auth_headers,
        f"Present endpoint {endpoint!r}",
    )

    response = client.post(
        "/data-stores",
        json={
            "project_id": project_id,
            "name": "remote-http",
            "kind": "http",
            "root": "https://files.example/base",
            "endpoint": endpoint,
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"] == {
        "code": "validation_error",
        "message": "Data store endpoint is not allowed.",
        "issues": None,
    }


@pytest.mark.parametrize(
    ("kind", "root"),
    (
        ("http", "https://files.example/base"),
        ("rclone", "/archive"),
        ("git", "https://git.example/lab/repository.git"),
    ),
)
def test_blank_present_credential_reference_is_rejected(
    client,
    admin_auth_headers,
    kind,
    root,
):
    project_id = _create_project(
        client,
        admin_auth_headers,
        f"Blank credential {kind}",
    )

    response = client.post(
        "/data-stores",
        json={
            "project_id": project_id,
            "name": f"blank-{kind}",
            "kind": kind,
            "root": root,
            "credential_ref": "",
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"


def test_create_local_fs_store_rejects_non_absolute_roots_before_persistence(
    client,
    admin_auth_headers,
):
    project_id = _create_project(client, admin_auth_headers, "Invalid local roots")
    invalid_roots = (
        "relative/path",
        "../sibling",
        "~/expanded-by-host",
        "C:drive-relative",
        r"\\server\share",
        r"\\?\C:\device-path",
        " /whitespace-repaired-root ",
        f"{os.path.abspath('data')}\x85c1-control",
    )

    for index, root in enumerate(invalid_roots):
        name = f"invalid-local-{index}"
        response = client.post(
            "/data-stores",
            json=_store_payload(
                project_id,
                name=name,
                kind="local_fs",
                root=root,
                is_default=False,
            ),
            headers=admin_auth_headers,
        )

        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "validation_error"
        assert root not in response.text

    listed = client.get(
        "/data-stores",
        params={"project_id": project_id},
        headers=admin_auth_headers,
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"] == []


@pytest.mark.parametrize(
    "name",
    (
        "has space",
        "-leading",
        ".leading",
        "_leading",
        "unicode-é",
        "a" * 64,
        " valid",
        "valid ",
    ),
)
def test_create_data_store_rejects_noncanonical_names_without_persistence(
    client,
    admin_auth_headers,
    tmp_path,
    name,
):
    project_id = _create_project(client, admin_auth_headers, f"Invalid store {name!r}")

    response = client.post(
        "/data-stores",
        json=_store_payload(
            project_id,
            name=name,
            kind="local_fs",
            root=str(tmp_path),
            is_default=False,
        ),
        headers=admin_auth_headers,
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"
    assert name not in response.text
    listed = client.get(
        "/data-stores",
        params={"project_id": project_id},
        headers=admin_auth_headers,
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"] == []


def test_remote_store_preserves_supported_interior_spaces_without_stripping(
    client,
    admin_auth_headers,
):
    project_id = _create_project(client, admin_auth_headers, "Remote root semantics")

    response = client.post(
        "/data-stores",
        json=_store_payload(
            project_id,
            name="legacy remote",
            kind="onedrive",
            root="experiments/current",
            is_default=False,
        ),
        headers=admin_auth_headers,
    )

    assert response.status_code == 201, response.text
    assert response.json()["data"]["name"] == "legacy remote"
    assert response.json()["data"]["root"] == "experiments/current"


@pytest.mark.skipif(os.name == "nt", reason="Windows aliases trailing root spaces")
def test_local_root_is_persisted_exactly_without_whitespace_repair(
    client,
    admin_auth_headers,
    tmp_path,
):
    root = tmp_path / "store with trailing space "
    root.mkdir()
    exact_data = b"bytes from the exact registered root"
    (root / "artifact.bin").write_bytes(exact_data)
    repaired_root = Path(str(root).rstrip())
    repaired_root.mkdir()
    (repaired_root / "artifact.bin").write_bytes(b"wrong trimmed-root bytes")
    _install_local_registry(client, tmp_path)
    project_id = _create_project(client, admin_auth_headers, "Exact local root")

    response = client.post(
        "/data-stores",
        json=_store_payload(
            project_id,
            name="exact-local-root",
            kind="local_fs",
            root=str(root),
            is_default=False,
        ),
        headers=admin_auth_headers,
    )

    assert response.status_code == 201, response.text
    assert response.json()["data"]["root"] == str(root)
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=project_id,
        uri="store://exact-local-root/artifact.bin",
        content_hash=_sha256(exact_data),
    )

    resolved = client.post(
        "/external-artifacts/resolve",
        json={"entity_type": "dataset", "entity_id": dataset_id},
        headers=admin_auth_headers,
    )

    assert resolved.status_code == 200, resolved.text
    body = resolved.json()["data"]
    assert body["status"] == "unresolved"
    assert body["source_system"] == "store"
    assert body["uri"] == "store://[redacted]"
    assert body["content_base64"] is None
    assert body["detail"] == "Store artifact could not be resolved."


def test_create_data_store_requires_contributor(client, scoped_project_member):
    response = client.post(
        "/data-stores",
        json=_store_payload(scoped_project_member.visible_project_id),
        headers=scoped_project_member.member_headers,
    )
    # A viewer cannot register a store.
    assert response.status_code == 401


def test_data_store_health_local_fs(client, admin_auth_headers, tmp_path):
    _install_local_health(client, tmp_path)
    project_id = _create_project(client, admin_auth_headers, "Health project")
    store_id = client.post(
        "/data-stores",
        json=_store_payload(project_id, name="lab-fs", kind="local_fs", root=str(tmp_path)),
        headers=admin_auth_headers,
    ).json()["data"]["store_id"]

    healthy = client.get(f"/data-stores/{store_id}/health", headers=admin_auth_headers)
    assert healthy.status_code == 200, healthy.text
    assert healthy.json()["data"]["status"] == "unsupported"
    assert (
        healthy.json()["data"]["detail"] == STORE_HEALTH_PROBE_UNAVAILABLE_MESSAGE
    )


def test_data_store_health_local_fs_missing_root(client, admin_auth_headers, tmp_path):
    _install_local_health(client, tmp_path)
    project_id = _create_project(client, admin_auth_headers, "Unhealthy project")
    missing = str(tmp_path / "does-not-exist")
    store_id = client.post(
        "/data-stores",
        json=_store_payload(project_id, name="lab-fs", kind="local_fs", root=missing),
        headers=admin_auth_headers,
    ).json()["data"]["store_id"]

    response = client.get(f"/data-stores/{store_id}/health", headers=admin_auth_headers)
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["status"] == "unsupported"
    assert body["kind"] == "local_fs"
    assert body["detail"] == STORE_HEALTH_PROBE_UNAVAILABLE_MESSAGE


def test_data_store_health_local_fs_defaults_to_denied_and_redacted(
    client,
    admin_auth_headers,
    tmp_path,
):
    secret = "default-denied-local-root"
    root = tmp_path / secret
    project_id = _create_project(client, admin_auth_headers, "Denied local health")
    store_id = client.post(
        "/data-stores",
        json=_store_payload(
            project_id,
            name="denied-local",
            kind="local_fs",
            root=str(root),
        ),
        headers=admin_auth_headers,
    ).json()["data"]["store_id"]

    class RecordingExecutor:
        def __init__(self):
            self.calls = []

        def run(self, command, **kwargs):
            self.calls.append((command, kwargs))
            raise AssertionError("deny-all local health reached the process executor")

    executor = RecordingExecutor()
    operations = BoundedLocalFilesystemOperations(
        authority=LocalFilesystemAuthority.from_roots([]),
        executor=executor,
    )
    client.app.state.store_health_checker = CachedStoreHealthProbe(
        LocalStoreHealthProbe(
            inspector=operations,
        )
    )

    response = client.get(
        f"/data-stores/{store_id}/health",
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "unsupported"
    assert (
        response.json()["data"]["detail"] == STORE_HEALTH_PROBE_UNAVAILABLE_MESSAGE
    )
    assert secret not in response.text
    assert executor.calls == []


def test_git_store_health_uses_installed_policy_without_leaking_credentials(
    client,
    admin_auth_headers,
):
    project_id = _create_project(client, admin_auth_headers, "Git health project")
    secret = "git-health-secret-must-not-leak"
    store_id = _seed_legacy_data_store(
        client,
        project_id=project_id,
        name="analysis-repository",
        kind=StoreKind.GIT,
        root=f"https://operator:{secret}@git.example/lab/repository.git",
    )

    workdir = client.app.state.git_health_workdir
    assert workdir.is_dir()
    assert list(workdir.iterdir()) == []
    assert not (workdir / ".git").exists()

    response = client.get(
        f"/data-stores/{store_id}/health",
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "unsupported"
    assert (
        response.json()["data"]["detail"] == STORE_HEALTH_PROBE_UNAVAILABLE_MESSAGE
    )
    assert secret not in response.text
    assert list(workdir.iterdir()) == []
    assert not (workdir / ".git").exists()


def test_rclone_store_health_route_denies_before_process_and_redacts_target(
    client,
    admin_auth_headers,
):
    project_id = _create_project(client, admin_auth_headers, "Rclone health project")
    secret = "rclone-health-secret-must-not-leak"
    store_id = client.post(
        "/data-stores",
        json=_store_payload(
            project_id,
            name="remote-archive",
            kind="rclone",
            root="/private/archive",
            credential_ref=secret,
        ),
        headers=admin_auth_headers,
    ).json()["data"]["store_id"]

    class RecordingExecutor:
        def __init__(self):
            self.calls = []

        def run(self, command, **kwargs):
            self.calls.append((command, kwargs))
            return ProcessResult(
                returncode=0,
                stdout=b"",
                stdout_bytes=0,
                stderr_bytes=0,
            )

    executor = RecordingExecutor()
    client.app.state.store_health_checker = CachedStoreHealthProbe(
        RcloneStoreHealthProbe(
            policy=RcloneRemotePolicy.deny_all(),
            executor=executor,
        )
    )

    response = client.get(
        f"/data-stores/{store_id}/health",
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["status"] == "unsupported"
    assert body["detail"] == STORE_HEALTH_PROBE_UNAVAILABLE_MESSAGE
    assert secret not in response.text
    assert executor.calls == []


def test_http_store_health_route_redacts_the_authoritative_selected_target(
    client,
    admin_auth_headers,
):
    project_id = _create_project(client, admin_auth_headers, "HTTP health project")
    secret = "http-health-secret-must-not-leak"
    store_id = _seed_legacy_data_store(
        client,
        project_id=project_id,
        name="remote-http-store",
        kind=StoreKind.HTTP,
        root="https://allowed.example/safe-root",
        endpoint=f"https://operator:{secret}@denied.example/private",
        credential_ref=f"vault:{secret}",
    )
    dns = FakeAddressResolver({"allowed.example": ["93.184.216.34"]})
    http_client = FakeSafeHttpClient(())
    client.app.state.store_health_checker = CachedStoreHealthProbe(
        HttpStoreHealthProbe(
            policy=OutboundHttpPolicy(address_resolver=dns),
            client=http_client,
        )
    )

    response = client.get(
        f"/data-stores/{store_id}/health",
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "store_id": store_id,
        "kind": "http",
        "status": "unsupported",
        "detail": STORE_HEALTH_PROBE_UNAVAILABLE_MESSAGE,
    }
    assert secret not in response.text
    assert "denied.example" not in response.text
    assert "allowed.example" not in response.text
    assert dns.calls == []
    assert http_client.calls == []


def test_data_store_health_denied_for_unauthorized_project(
    client, scoped_project_member, admin_auth_headers, tmp_path
):
    store_id = client.post(
        "/data-stores",
        json=_store_payload(
            scoped_project_member.hidden_project_id,
            name="lab-fs",
            kind="local_fs",
            root=str(tmp_path),
        ),
        headers=admin_auth_headers,
    ).json()["data"]["store_id"]

    response = client.get(
        f"/data-stores/{store_id}/health", headers=scoped_project_member.member_headers
    )
    assert response.status_code == 404
    assert response.json() == _data_store_not_found_body()


def test_get_data_store_denied_for_unauthorized_project(
    client, scoped_project_member, admin_auth_headers
):
    store_id = client.post(
        "/data-stores",
        json=_store_payload(scoped_project_member.hidden_project_id),
        headers=admin_auth_headers,
    ).json()["data"]["store_id"]

    response = client.get(
        f"/data-stores/{store_id}", headers=scoped_project_member.member_headers
    )
    assert response.status_code == 404
    assert response.json() == _data_store_not_found_body()


def _register_group_member(client, admin_auth_headers, group_id: str) -> dict[str, str]:
    """Register a fresh viewer-role user and add them to the group."""

    from lab_tracker.auth import Role

    username = f"group-member-{uuid4().hex[:8]}"
    user = client.app.state.auth_service.register_user(
        username=username, password="secret", role=Role.VIEWER
    )
    membership = client.post(
        f"/groups/{group_id}/members",
        json={"user_id": str(user.user_id), "role": "viewer"},
        headers=admin_auth_headers,
    )
    assert membership.status_code == 201
    login = client.post(
        "/auth/login", json={"username": username, "password": "secret"}
    )
    token = login.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_group_member_can_read_group_scoped_store(
    client, admin_auth_headers, tmp_path
):
    """A group-scoped store has project_id=None; authorization must check the
    group scope, not deny every non-admin via ensure_project_read(None)."""

    _install_local_health(client, tmp_path)
    group_id = _create_group(client, admin_auth_headers, "Readable lab")
    store_id = client.post(
        "/data-stores",
        json={
            "group_id": group_id,
            "name": "lab-fs",
            "kind": "local_fs",
            "root": str(tmp_path),
            "authority_grant_id": TEST_STORE_AUTHORITY_GRANT_ID,
        },
        headers=admin_auth_headers,
    ).json()["data"]["store_id"]
    member_headers = _register_group_member(client, admin_auth_headers, group_id)

    get_response = client.get(f"/data-stores/{store_id}", headers=member_headers)
    health_response = client.get(
        f"/data-stores/{store_id}/health", headers=member_headers
    )

    assert get_response.status_code == 200
    assert get_response.json()["data"]["group_id"] == group_id
    assert health_response.status_code == 200
    assert health_response.json()["data"]["status"] == "unsupported"
    assert (
        health_response.json()["data"]["detail"]
        == STORE_HEALTH_PROBE_UNAVAILABLE_MESSAGE
    )


def test_group_scoped_store_still_denied_for_non_member(
    client, admin_auth_headers, scoped_project_member, tmp_path
):
    group_id = _create_group(client, admin_auth_headers, "Private lab")
    store_id = client.post(
        "/data-stores",
        json={
            "group_id": group_id,
            "name": "lab-fs",
            "kind": "local_fs",
            "root": str(tmp_path),
            "authority_grant_id": TEST_STORE_AUTHORITY_GRANT_ID,
        },
        headers=admin_auth_headers,
    ).json()["data"]["store_id"]

    response = client.get(
        f"/data-stores/{store_id}", headers=scoped_project_member.member_headers
    )
    health = client.get(
        f"/data-stores/{store_id}/health",
        headers=scoped_project_member.member_headers,
    )
    missing_store_id = uuid4()
    missing = client.get(
        f"/data-stores/{missing_store_id}",
        headers=scoped_project_member.member_headers,
    )
    missing_health = client.get(
        f"/data-stores/{missing_store_id}/health",
        headers=scoped_project_member.member_headers,
    )

    assert {
        response.status_code,
        health.status_code,
        missing.status_code,
        missing_health.status_code,
    } == {404}
    assert (
        response.json()
        == health.json()
        == missing.json()
        == missing_health.json()
        == _data_store_not_found_body()
    )
