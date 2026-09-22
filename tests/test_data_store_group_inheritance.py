"""Inherited group stores are readable wherever they are listed (M53).

A group-scoped store is inherited by every project in its group, so anyone who
can read one of those projects, or the group itself, can list it, fetch it and
check its health. Everyone else gets the same opaque 404 as a missing store.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

import pytest
from api_helpers import TEST_STORE_AUTHORITY_GRANT_ID
from conftest import _register_test_user
from fastapi.testclient import TestClient

_NOT_FOUND = {
    "error": {
        "code": "not_found",
        "message": "Data store does not exist.",
        "issues": None,
    }
}


def _post(client: TestClient, headers: dict[str, str], path: str, payload: dict) -> dict:
    response = client.post(path, json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _group_store(client: TestClient, headers: dict[str, str], group_id: str, name: str) -> str:
    store = _post(
        client,
        headers,
        "/data-stores",
        {
            "group_id": group_id,
            "name": name,
            "kind": "s3",
            "root": f"/{name}",
            "authority_grant_id": TEST_STORE_AUTHORITY_GRANT_ID,
        },
    )
    return store["store_id"]


@dataclass(frozen=True)
class _Lab:
    group_id: str
    project_id: str
    group_store_id: str
    project_store_id: str
    other_group_store_id: str
    other_project_store_id: str


@pytest.fixture()
def lab(client: TestClient, admin_auth_headers: dict[str, str]) -> _Lab:
    headers = admin_auth_headers
    group_id = _post(client, headers, "/groups", {"name": f"Lab {uuid4().hex[:6]}"})["group_id"]
    other_group_id = _post(
        client, headers, "/groups", {"name": f"Other lab {uuid4().hex[:6]}"}
    )["group_id"]
    project_id = _post(
        client, headers, "/projects", {"name": "Grouped project", "group_id": group_id}
    )["project_id"]
    other_project_id = _post(
        client,
        headers,
        "/projects",
        {"name": "Other grouped project", "group_id": other_group_id},
    )["project_id"]
    project_store_id = _post(
        client,
        headers,
        "/data-stores",
        {
            "project_id": project_id,
            "name": "project-s3",
            "kind": "s3",
            "root": "/project-s3",
            "authority_grant_id": TEST_STORE_AUTHORITY_GRANT_ID,
        },
    )["store_id"]
    other_project_store_id = _post(
        client,
        headers,
        "/data-stores",
        {
            "project_id": other_project_id,
            "name": "other-project-s3",
            "kind": "s3",
            "root": "/other-project-s3",
            "authority_grant_id": TEST_STORE_AUTHORITY_GRANT_ID,
        },
    )["store_id"]
    return _Lab(
        group_id=group_id,
        project_id=project_id,
        group_store_id=_group_store(client, headers, group_id, "lab-shared"),
        project_store_id=project_store_id,
        other_group_store_id=_group_store(client, headers, other_group_id, "other-shared"),
        other_project_store_id=other_project_store_id,
    )


def _listed_ids(client: TestClient, headers: dict[str, str], **params: str) -> list[str]:
    response = client.get("/data-stores", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return [store["store_id"] for store in response.json()["data"]]


def _assert_readable(client: TestClient, headers: dict[str, str], store_id: str) -> None:
    fetched = client.get(f"/data-stores/{store_id}", headers=headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["data"]["store_id"] == store_id
    health = client.get(f"/data-stores/{store_id}/health", headers=headers)
    assert health.status_code == 200, health.text
    assert health.json()["data"]["store_id"] == store_id


def _assert_opaque(client: TestClient, headers: dict[str, str], store_id: str) -> None:
    for path in (f"/data-stores/{store_id}", f"/data-stores/{store_id}/health"):
        response = client.get(path, headers=headers)
        assert response.status_code == 404, response.text
        assert response.json() == _NOT_FOUND


@pytest.mark.parametrize("role", ["viewer", "contributor"])
def test_project_member_reads_inherited_group_store_it_lists(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    lab: _Lab,
    role: str,
) -> None:
    member = _register_test_user(client, username_prefix=f"project-{role}")
    _post(
        client,
        admin_auth_headers,
        f"/projects/{lab.project_id}/members",
        {"user_id": member.user_id, "role": role},
    )

    assert _listed_ids(client, member.headers, project_id=lab.project_id) == [
        lab.project_store_id,
        lab.group_store_id,
    ]
    assert _listed_ids(client, member.headers) == [lab.project_store_id, lab.group_store_id]
    _assert_readable(client, member.headers, lab.group_store_id)
    _assert_readable(client, member.headers, lab.project_store_id)
    _assert_opaque(client, member.headers, lab.other_group_store_id)
    _assert_opaque(client, member.headers, lab.other_project_store_id)


def test_group_member_lists_group_stores_without_project_membership(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    lab: _Lab,
) -> None:
    member = _register_test_user(client, username_prefix="group-viewer")
    _post(
        client,
        admin_auth_headers,
        f"/groups/{lab.group_id}/members",
        {"user_id": member.user_id, "role": "viewer"},
    )

    assert _listed_ids(client, member.headers, group_id=lab.group_id) == [lab.group_store_id]
    assert _listed_ids(client, member.headers) == [lab.group_store_id]
    _assert_readable(client, member.headers, lab.group_store_id)
    _assert_opaque(client, member.headers, lab.project_store_id)
    _assert_opaque(client, member.headers, lab.other_group_store_id)


def test_outsider_sees_no_group_store(
    client: TestClient,
    lab: _Lab,
) -> None:
    outsider = _register_test_user(client, username_prefix="store-outsider")

    assert _listed_ids(client, outsider.headers) == []
    _assert_opaque(client, outsider.headers, lab.group_store_id)
    _assert_opaque(client, outsider.headers, lab.other_group_store_id)
