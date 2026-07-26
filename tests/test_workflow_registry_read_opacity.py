from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from read_opacity_inventory import (
    READ_OPACITY_VARIANTS_BY_ID,
    WORKFLOW_REGISTRY_READ_OPACITY_VARIANTS,
    WORKFLOW_REGISTRY_SUITE,
)

from lab_tracker.api import LabTrackerAPI
from lab_tracker.artifact_resolution import StoreHealth, StoreHealthStatus
from lab_tracker.auth import Role, utc_now
from lab_tracker.models import DataStore, GraphChangeSet
from lab_tracker.routes import graph_batches as graph_batch_routes
from lab_tracker.routes import graph_drafts as graph_draft_routes
from lab_tracker.sqlalchemy_repository_parts.graph_drafts import (
    SQLAlchemyGraphChangeSetRepository,
)


@dataclass(frozen=True)
class WorkflowRegistryRecords:
    project_id: str
    graph_change_set_id: str
    graph_operation_id: str
    graph_creator_username: str
    store_id: str
    missing_graph_change_set_id: str
    missing_store_id: str


class StaticDraftClient:
    provider = "test"
    model = "opacity-model"

    def __init__(self, project_id: str) -> None:
        self.project_id = project_id

    def _patch(self) -> dict[str, Any]:
        return {
            "summary": "Opaque graph draft",
            "uncertain_fields": [],
            "clarification_requests": [],
            "operations": [
                {
                    "client_ref": "opaque-question",
                    "op": "create",
                    "entity_type": "question",
                    "semantic_type": "suggest_new_question",
                    "target_entity_id": None,
                    "payload_json": json.dumps(
                        {
                            "project_id": self.project_id,
                            "text": "Does the opaque read boundary run first?",
                            "question_type": "descriptive",
                            "status": "staged",
                        }
                    ),
                    "rationale": "Exercise full operation hydration.",
                    "confidence": 0.9,
                    "source_refs": [],
                }
            ],
        }

    def draft_from_note(self, **_kwargs: Any) -> dict[str, Any]:
        return self._patch()

    def draft_from_batch(self, **_kwargs: Any) -> dict[str, Any]:
        return self._patch()

    def close(self) -> None:
        return None


def _not_found_body(label: str) -> dict[str, object]:
    return {
        "error": {
            "code": "not_found",
            "message": f"{label} does not exist.",
            "issues": None,
        }
    }


def _read_paths(graph_change_set_id: str, store_id: str) -> tuple[str, ...]:
    paths = (
        f"/graph-drafts/{graph_change_set_id}",
        f"/batches/{graph_change_set_id}",
        f"/data-stores/{store_id}",
        f"/data-stores/{store_id}/health",
    )
    assert len(paths) == 4
    return paths


def _create_project(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str,
    group_id: str | None = None,
) -> str:
    payload = {"name": name}
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
    group_read_all: bool,
) -> str:
    response = client.post(
        "/groups",
        json={"name": name, "group_read_all": group_read_all},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["group_id"]


def _register_viewer(
    client: TestClient,
    *,
    username_prefix: str,
) -> tuple[str, dict[str, str]]:
    username = f"{username_prefix}-{uuid4().hex[:8]}"
    user = client.app.state.auth_service.register_user(
        username=username,
        password="secret",
        role=Role.VIEWER,
    )
    login = client.post(
        "/auth/login",
        json={"username": username, "password": "secret"},
    )
    assert login.status_code == 200, login.text
    return str(user.user_id), {"Authorization": f"Bearer {login.json()['data']['access_token']}"}


def _add_group_member(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    *,
    group_id: str,
    user_id: str,
) -> None:
    response = client.post(
        f"/groups/{group_id}/members",
        json={"user_id": user_id, "role": "viewer"},
        headers=admin_auth_headers,
    )
    assert response.status_code == 201, response.text


def _create_workflow_registry_records(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    *,
    project_id: str,
    store_root: Path,
    label: str,
) -> WorkflowRegistryRecords:
    note = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": f"Verify the {label} graph read boundary.",
            "status": "staged",
        },
        headers=admin_auth_headers,
    )
    assert note.status_code == 201, note.text
    client.app.state.graph_draft_client_factory = lambda _settings: StaticDraftClient(project_id)
    draft = client.post(
        f"/notes/{note.json()['data']['note_id']}/graph-drafts",
        headers=admin_auth_headers,
    )
    assert draft.status_code == 201, draft.text
    draft_data = draft.json()["data"]
    assert draft_data["created_by_username"]
    assert len(draft_data["operations"]) == 1

    store = client.post(
        "/data-stores",
        json={
            "project_id": project_id,
            "name": f"{label}-local-store",
            "kind": "local_fs",
            "root": str(store_root),
        },
        headers=admin_auth_headers,
    )
    assert store.status_code == 201, store.text
    return WorkflowRegistryRecords(
        project_id=project_id,
        graph_change_set_id=draft_data["change_set_id"],
        graph_operation_id=draft_data["operations"][0]["operation_id"],
        graph_creator_username=draft_data["created_by_username"],
        store_id=store.json()["data"]["store_id"],
        missing_graph_change_set_id=str(uuid4()),
        missing_store_id=str(uuid4()),
    )


def _create_batch_graph_draft(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    *,
    project_id: str,
    label: str,
) -> str:
    note = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": f"{label} batch evidence.",
            "status": "staged",
        },
        headers=admin_auth_headers,
    )
    assert note.status_code == 201, note.text
    client.app.state.graph_draft_client_factory = lambda _settings: StaticDraftClient(project_id)
    response = client.post(
        "/batches/run-now",
        json={"project_id": project_id},
        headers=admin_auth_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["change_set_id"]


@pytest.fixture()
def workflow_registry_records(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    scoped_project_member,
    tmp_path,
) -> WorkflowRegistryRecords:
    return _create_workflow_registry_records(
        client,
        admin_auth_headers,
        project_id=scoped_project_member.hidden_project_id,
        store_root=tmp_path,
        label="opaque",
    )


def test_exact_workflow_registry_read_variants_are_opaque(
    client: TestClient,
    scoped_project_member,
    workflow_registry_records: WorkflowRegistryRecords,
) -> None:
    records = workflow_registry_records
    cases = (
        (
            "graph-draft-detail",
            f"/graph-drafts/{records.graph_change_set_id}",
            f"/graph-drafts/{records.missing_graph_change_set_id}",
            "Graph draft",
        ),
        (
            "batch-detail",
            f"/batches/{records.graph_change_set_id}",
            f"/batches/{records.missing_graph_change_set_id}",
            "Graph draft",
        ),
        (
            "data-store-detail",
            f"/data-stores/{records.store_id}",
            f"/data-stores/{records.missing_store_id}",
            "Data store",
        ),
        (
            "data-store-health",
            f"/data-stores/{records.store_id}/health",
            f"/data-stores/{records.missing_store_id}/health",
            "Data store",
        ),
    )
    assert len(cases) == 4
    inventory_coverage_ids = {
        variant.coverage_id
        for variant in WORKFLOW_REGISTRY_READ_OPACITY_VARIANTS
    }
    assert {
        f"{WORKFLOW_REGISTRY_SUITE}.{case_name}"
        for case_name, _existing_path, _missing_path, _label in cases
    } == inventory_coverage_ids

    for case_name, existing_path, missing_path, label in cases:
        coverage_id = f"{WORKFLOW_REGISTRY_SUITE}.{case_name}"
        assert coverage_id in inventory_coverage_ids
        inventory_variant = READ_OPACITY_VARIANTS_BY_ID[coverage_id]
        assert inventory_variant.matches_request(
            method="GET",
            request_target=existing_path,
            variant="default",
        )
        assert inventory_variant.matches_request(
            method="GET",
            request_target=missing_path,
            variant="default",
        )
        hidden = client.get(
            existing_path,
            headers=scoped_project_member.member_headers,
        )
        missing = client.get(
            missing_path,
            headers=scoped_project_member.member_headers,
        )
        assert hidden.status_code == missing.status_code == 404
        assert hidden.json() == missing.json() == _not_found_body(label)


@pytest.mark.parametrize(
    ("path_prefix", "expected_decoration"),
    (
        ("/graph-drafts", "decorate:graph-drafts"),
        ("/batches", "decorate:batches"),
    ),
)
def test_graph_read_aliases_authorize_before_live_side_effects(
    path_prefix: str,
    expected_decoration: str,
    client: TestClient,
    admin_auth_headers: dict[str, str],
    scoped_project_member,
    workflow_registry_records: WorkflowRegistryRecords,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    original_materialize = SQLAlchemyGraphChangeSetRepository._from_rows
    original_record_usage = LabTrackerAPI.record_usage_event
    original_graph_decoration = graph_draft_routes._attach_graph_usernames
    original_batch_decoration = graph_batch_routes.attach_graph_usernames

    def materialize(
        repository: SQLAlchemyGraphChangeSetRepository,
        rows: list[Any],
        *,
        include_operations: bool = True,
    ) -> list[GraphChangeSet]:
        events.append("materialize")
        return original_materialize(
            repository,
            rows,
            include_operations=include_operations,
        )

    def record_usage(api: LabTrackerAPI, **kwargs: Any) -> None:
        events.append("usage")
        original_record_usage(api, **kwargs)

    def decorate_graph_draft(request: Any, change_set: GraphChangeSet) -> GraphChangeSet:
        events.append("decorate:graph-drafts")
        return original_graph_decoration(request, change_set)

    def decorate_batch(request: Any, change_set: GraphChangeSet) -> GraphChangeSet:
        events.append("decorate:batches")
        return original_batch_decoration(request, change_set)

    monkeypatch.setattr(SQLAlchemyGraphChangeSetRepository, "_from_rows", materialize)
    monkeypatch.setattr(LabTrackerAPI, "record_usage_event", record_usage)
    monkeypatch.setattr(
        graph_draft_routes,
        "_attach_graph_usernames",
        decorate_graph_draft,
    )
    monkeypatch.setattr(
        graph_batch_routes,
        "attach_graph_usernames",
        decorate_batch,
    )

    records = workflow_registry_records
    hidden = client.get(
        f"{path_prefix}/{records.graph_change_set_id}",
        headers=scoped_project_member.member_headers,
    )
    missing = client.get(
        f"{path_prefix}/{records.missing_graph_change_set_id}",
        headers=scoped_project_member.member_headers,
    )
    assert hidden.status_code == missing.status_code == 404
    assert events == []

    authorized = client.get(
        f"{path_prefix}/{records.graph_change_set_id}",
        headers=admin_auth_headers,
    )
    assert authorized.status_code == 200, authorized.text
    payload = authorized.json()["data"]
    assert len(payload["operations"]) == 1
    assert payload["created_by_username"] == records.graph_creator_username
    assert events == ["materialize", "usage", expected_decoration]


def test_store_health_authorizes_before_live_checker(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    scoped_project_member,
    workflow_registry_records: WorkflowRegistryRecords,
) -> None:
    checked: list[DataStore] = []

    def checker(store: DataStore) -> StoreHealth:
        checked.append(store)
        return StoreHealth(StoreHealthStatus.HEALTHY)

    client.app.state.store_health_checker = checker
    records = workflow_registry_records
    hidden = client.get(
        f"/data-stores/{records.store_id}/health",
        headers=scoped_project_member.member_headers,
    )
    missing = client.get(
        f"/data-stores/{records.missing_store_id}/health",
        headers=scoped_project_member.member_headers,
    )
    assert hidden.status_code == missing.status_code == 404
    assert checked == []

    authorized = client.get(
        f"/data-stores/{records.store_id}/health",
        headers=admin_auth_headers,
    )
    assert authorized.status_code == 200, authorized.text
    assert authorized.json()["data"]["status"] == "healthy"
    assert [str(store.store_id) for store in checked] == [records.store_id]


def test_project_and_group_inherited_readers_can_use_all_four_variants(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    scoped_project_member,
    tmp_path: Path,
) -> None:
    direct = _create_workflow_registry_records(
        client,
        admin_auth_headers,
        project_id=scoped_project_member.visible_project_id,
        store_root=tmp_path,
        label="direct-reader",
    )

    group_id = _create_group(
        client,
        admin_auth_headers,
        name="Inherited workflow registry",
        group_read_all=True,
    )
    _add_group_member(
        client,
        admin_auth_headers,
        group_id=group_id,
        user_id=scoped_project_member.member_user_id,
    )
    inherited_project_id = _create_project(
        client,
        admin_auth_headers,
        name="Inherited workflow project",
        group_id=group_id,
    )
    inherited = _create_workflow_registry_records(
        client,
        admin_auth_headers,
        project_id=inherited_project_id,
        store_root=tmp_path,
        label="inherited-reader",
    )

    for records in (direct, inherited):
        responses = [
            client.get(path, headers=scoped_project_member.member_headers)
            for path in _read_paths(records.graph_change_set_id, records.store_id)
        ]
        assert [response.status_code for response in responses] == [200] * 4
        assert responses[0].json()["data"]["change_set_id"] == (records.graph_change_set_id)
        assert responses[1].json()["data"]["change_set_id"] == (records.graph_change_set_id)
        assert responses[2].json()["data"]["store_id"] == records.store_id
        assert responses[3].json()["data"]["status"] == "healthy"


def test_plain_group_membership_reads_group_store_but_not_project_records(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    user_id, member_headers = _register_viewer(
        client,
        username_prefix="plain-group-member",
    )
    group_id = _create_group(
        client,
        admin_auth_headers,
        name="Non-inheriting workflow registry",
        group_read_all=False,
    )
    _add_group_member(
        client,
        admin_auth_headers,
        group_id=group_id,
        user_id=user_id,
    )
    project_id = _create_project(
        client,
        admin_auth_headers,
        name="Non-inherited workflow project",
        group_id=group_id,
    )
    records = _create_workflow_registry_records(
        client,
        admin_auth_headers,
        project_id=project_id,
        store_root=tmp_path,
        label="plain-group-project",
    )
    group_store = client.post(
        "/data-stores",
        json={
            "group_id": group_id,
            "name": "plain-group-store",
            "kind": "local_fs",
            "root": str(tmp_path),
        },
        headers=admin_auth_headers,
    )
    assert group_store.status_code == 201, group_store.text
    group_store_id = group_store.json()["data"]["store_id"]

    allowed = (
        client.get(f"/data-stores/{group_store_id}", headers=member_headers),
        client.get(
            f"/data-stores/{group_store_id}/health",
            headers=member_headers,
        ),
    )
    assert [response.status_code for response in allowed] == [200, 200]

    denied = [
        client.get(path, headers=member_headers)
        for path in _read_paths(records.graph_change_set_id, records.store_id)
    ]
    assert [response.status_code for response in denied] == [404] * 4
    assert denied[0].json() == denied[1].json() == _not_found_body("Graph draft")
    assert denied[2].json() == denied[3].json() == _not_found_body("Data store")


def test_exact_variants_preserve_authentication_and_capability_statuses(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    workflow_registry_records: WorkflowRegistryRecords,
) -> None:
    records = workflow_registry_records
    paths = _read_paths(records.graph_change_set_id, records.store_id)

    missing_auth = [client.get(path) for path in paths]
    assert [response.status_code for response in missing_auth] == [401] * 4
    assert {response.json()["error"]["message"] for response in missing_auth} == {
        "Missing Authorization header."
    }

    invalid_headers = {"Authorization": "Bearer invalid-token"}
    invalid = [client.get(path, headers=invalid_headers) for path in paths]
    assert [response.status_code for response in invalid] == [401] * 4
    assert {response.json()["error"]["code"] for response in invalid} == {"auth_error"}

    issued = client.post(
        "/auth/tokens",
        json={
            "label": "Workflow registry scheduler-only token",
            "role": "admin",
            "read_only": False,
            "scope": "batch_run_due",
            "expires_at": (utc_now() + timedelta(days=1)).isoformat(),
        },
        headers=admin_auth_headers,
    )
    assert issued.status_code == 201, issued.text
    scoped_headers = {"Authorization": f"Bearer {issued.json()['data']['secret']}"}
    forbidden = [client.get(path, headers=scoped_headers) for path in paths]
    assert [response.status_code for response in forbidden] == [403] * 4
    assert {json.dumps(response.json()["error"], sort_keys=True) for response in forbidden} == {
        json.dumps(
            {
                "code": "service_forbidden",
                "message": "Not permitted for this token.",
                "issues": None,
            },
            sort_keys=True,
        )
    }


@pytest.mark.parametrize(
    ("path", "expected_status"),
    (
        ("/graph-drafts/not-a-uuid", 404),
        ("/batches/not-a-uuid", 404),
        ("/data-stores/not-a-uuid", 422),
        ("/data-stores/not-a-uuid/health", 422),
    ),
)
def test_exact_variants_preserve_malformed_identifier_statuses(
    path: str,
    expected_status: int,
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    assert client.get(path, headers=admin_auth_headers).status_code == expected_status


def test_group_store_health_authorizes_before_live_checker(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    scoped_project_member,
    tmp_path: Path,
) -> None:
    member_id, member_headers = _register_viewer(
        client,
        username_prefix="health-group-member",
    )
    group_id = _create_group(
        client,
        admin_auth_headers,
        name="Health checker group",
        group_read_all=False,
    )
    _add_group_member(
        client,
        admin_auth_headers,
        group_id=group_id,
        user_id=member_id,
    )
    store = client.post(
        "/data-stores",
        json={
            "group_id": group_id,
            "name": "health-group-store",
            "kind": "local_fs",
            "root": str(tmp_path),
        },
        headers=admin_auth_headers,
    )
    assert store.status_code == 201, store.text
    store_id = store.json()["data"]["store_id"]
    checked: list[DataStore] = []

    def checker(data_store: DataStore) -> StoreHealth:
        checked.append(data_store)
        return StoreHealth(StoreHealthStatus.HEALTHY)

    client.app.state.store_health_checker = checker
    hidden = client.get(
        f"/data-stores/{store_id}/health",
        headers=scoped_project_member.member_headers,
    )
    missing = client.get(
        f"/data-stores/{uuid4()}/health",
        headers=scoped_project_member.member_headers,
    )
    assert hidden.status_code == missing.status_code == 404
    assert checked == []

    authorized = client.get(
        f"/data-stores/{store_id}/health",
        headers=member_headers,
    )
    assert authorized.status_code == 200, authorized.text
    assert [str(data_store.store_id) for data_store in checked] == [store_id]


def test_workflow_registry_mutations_keep_permission_errors(
    client: TestClient,
    scoped_project_member,
    workflow_registry_records: WorkflowRegistryRecords,
) -> None:
    records = workflow_registry_records
    cases: tuple[tuple[str, str, dict[str, Any]], ...] = (
        (
            "PATCH",
            (
                f"/graph-drafts/{records.graph_change_set_id}/operations/"
                f"{records.graph_operation_id}"
            ),
            {"json": {"status": "accepted"}},
        ),
        (
            "POST",
            f"/graph-drafts/{records.graph_change_set_id}/accept-all",
            {},
        ),
        ("POST", f"/graph-drafts/{records.graph_change_set_id}/submit", {}),
        (
            "POST",
            f"/graph-drafts/{records.graph_change_set_id}/review",
            {"json": {"status": "changes_requested", "note": "Forbidden"}},
        ),
        (
            "POST",
            f"/graph-drafts/{records.graph_change_set_id}/revise",
            {"data": {"feedback": "Forbidden"}},
        ),
        (
            "POST",
            f"/graph-drafts/{records.graph_change_set_id}/commit",
            {"json": {"message": "Forbidden"}},
        ),
        (
            "POST",
            "/data-stores",
            {
                "json": {
                    "project_id": records.project_id,
                    "name": "forbidden-store",
                    "kind": "local_fs",
                    "root": "/forbidden",
                }
            },
        ),
    )
    assert len(cases) == 7

    for method, path, kwargs in cases:
        response = client.request(
            method,
            path,
            headers=scoped_project_member.member_headers,
            **kwargs,
        )
        assert response.status_code == 401, f"{method} {path}: {response.text}"
        assert response.json()["error"]["code"] == "auth_error"


def test_workflow_registry_lists_hide_nonmember_records(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    scoped_project_member,
    workflow_registry_records: WorkflowRegistryRecords,
    tmp_path: Path,
) -> None:
    hidden = workflow_registry_records
    visible = _create_workflow_registry_records(
        client,
        admin_auth_headers,
        project_id=scoped_project_member.visible_project_id,
        store_root=tmp_path,
        label="visible-list",
    )
    visible_batch_id = _create_batch_graph_draft(
        client,
        admin_auth_headers,
        project_id=visible.project_id,
        label="visible",
    )
    hidden_batch_id = _create_batch_graph_draft(
        client,
        admin_auth_headers,
        project_id=hidden.project_id,
        label="hidden",
    )

    graph_response = client.get(
        "/graph-drafts",
        headers=scoped_project_member.member_headers,
    )
    batch_response = client.get(
        "/batches",
        headers=scoped_project_member.member_headers,
    )
    store_response = client.get(
        "/data-stores",
        headers=scoped_project_member.member_headers,
    )
    assert graph_response.status_code == batch_response.status_code == 200
    assert store_response.status_code == 200

    graph_ids = {item["change_set_id"] for item in graph_response.json()["data"]}
    assert {visible.graph_change_set_id, visible_batch_id} <= graph_ids
    assert {hidden.graph_change_set_id, hidden_batch_id}.isdisjoint(graph_ids)

    batch_ids = {item["change_set_id"] for item in batch_response.json()["data"]}
    assert batch_ids == {visible_batch_id}

    store_ids = {item["store_id"] for item in store_response.json()["data"]}
    assert store_ids == {visible.store_id}
