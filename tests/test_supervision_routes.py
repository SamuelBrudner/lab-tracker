from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from lab_tracker.auth import Role


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register_user(
    client: TestClient,
    username: str,
    *,
    role: str = "viewer",
    headers: dict[str, str] | None = None,
) -> tuple[str, str]:
    response = client.post(
        "/auth/register",
        json={"username": username, "password": "secret", "role": role},
        headers=headers or {},
    )
    assert response.status_code == 201
    data = response.json()["data"]
    return data["access_token"], data["user"]["user_id"]


def test_admin_manages_supervision_edges(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    _, supervisor_user_id = _register_user(
        client,
        f"supervisor-{uuid4().hex[:8]}",
    )
    _, supervisee_user_id = _register_user(
        client,
        f"supervisee-{uuid4().hex[:8]}",
    )
    started_at = "2026-01-01T00:00:00+00:00"
    ended_at = "2026-02-01T00:00:00+00:00"

    create_response = client.post(
        "/supervision-edges",
        json={
            "supervisor_user_id": supervisor_user_id,
            "supervisee_user_id": supervisee_user_id,
            "started_at": started_at,
        },
        headers=admin_auth_headers,
    )
    assert create_response.status_code == 201
    edge = create_response.json()["data"]

    duplicate_response = client.post(
        "/supervision-edges",
        json={
            "supervisor_user_id": supervisor_user_id,
            "supervisee_user_id": supervisee_user_id,
            "started_at": "2026-01-15T00:00:00+00:00",
        },
        headers=admin_auth_headers,
    )
    active_response = client.get(
        "/supervision-edges",
        params={"supervisee_user_id": supervisee_user_id, "active_only": True},
        headers=admin_auth_headers,
    )
    close_response = client.patch(
        f"/supervision-edges/{edge['edge_id']}",
        json={"ended_at": ended_at},
        headers=admin_auth_headers,
    )
    successor_response = client.post(
        "/supervision-edges",
        json={
            "supervisor_user_id": supervisor_user_id,
            "supervisee_user_id": supervisee_user_id,
            "started_at": ended_at,
        },
        headers=admin_auth_headers,
    )
    as_of_response = client.get(
        "/supervision-edges",
        params={
            "supervisee_user_id": supervisee_user_id,
            "as_of": "2026-01-15T00:00:00+00:00",
        },
        headers=admin_auth_headers,
    )
    successor_edge_id = successor_response.json()["data"]["edge_id"]
    delete_response = client.delete(
        f"/supervision-edges/{successor_edge_id}",
        headers=admin_auth_headers,
    )
    deleted_get_response = client.get(
        f"/supervision-edges/{successor_edge_id}",
        headers=admin_auth_headers,
    )

    assert duplicate_response.status_code == 409
    assert active_response.status_code == 200
    assert [item["edge_id"] for item in active_response.json()["data"]] == [edge["edge_id"]]
    assert close_response.status_code == 200
    assert close_response.json()["data"]["ended_at"] == "2026-02-01T00:00:00Z"
    assert successor_response.status_code == 201
    assert as_of_response.status_code == 200
    assert [item["edge_id"] for item in as_of_response.json()["data"]] == [edge["edge_id"]]
    assert delete_response.status_code == 200
    assert deleted_get_response.status_code == 404


def test_supervision_edge_management_requires_write_role(
    client: TestClient,
):
    viewer_token, supervisor_user_id = _register_user(
        client,
        f"viewer-supervisor-{uuid4().hex[:8]}",
    )
    _, supervisee_user_id = _register_user(
        client,
        f"viewer-supervisee-{uuid4().hex[:8]}",
    )

    response = client.post(
        "/supervision-edges",
        json={
            "supervisor_user_id": supervisor_user_id,
            "supervisee_user_id": supervisee_user_id,
            "started_at": "2026-01-01T00:00:00+00:00",
        },
        headers=_auth_headers(viewer_token),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    assert response.json()["error"]["message"] == "Insufficient role."


@dataclass(frozen=True)
class _LabUser:
    headers: dict[str, str]
    user_id: str


@dataclass(frozen=True)
class _Lab:
    pi: _LabUser
    student: _LabUser
    labmate: _LabUser
    outsider: _LabUser
    group_id: str


def _lab_user(client: TestClient, prefix: str, role: Role = Role.EDITOR) -> _LabUser:
    username = f"{prefix}-{uuid4().hex[:8]}"
    user = client.app.state.auth_service.register_user(
        username=username,
        password="secret",
        role=role,
    )
    login = client.post("/auth/login", json={"username": username, "password": "secret"})
    assert login.status_code == 200
    return _LabUser(
        headers=_auth_headers(login.json()["data"]["access_token"]),
        user_id=str(user.user_id),
    )


@pytest.fixture()
def lab(client: TestClient, admin_auth_headers: dict[str, str]) -> _Lab:
    pi = _lab_user(client, "pi")
    student = _lab_user(client, "student")
    labmate = _lab_user(client, "labmate")
    outsider = _lab_user(client, "outsider")
    group = client.post("/groups", json={"name": "Lab"}, headers=admin_auth_headers)
    assert group.status_code == 201
    group_id = group.json()["data"]["group_id"]
    for member, role in ((pi, "owner"), (student, "contributor"), (labmate, "contributor")):
        response = client.post(
            f"/groups/{group_id}/members",
            json={"user_id": member.user_id, "role": role},
            headers=admin_auth_headers,
        )
        assert response.status_code == 201, response.text
    return _Lab(pi=pi, student=student, labmate=labmate, outsider=outsider, group_id=group_id)


def _create_edge(
    client: TestClient,
    headers: dict[str, str],
    supervisor: _LabUser,
    supervisee: _LabUser,
):
    return client.post(
        "/supervision-edges",
        json={
            "supervisor_user_id": supervisor.user_id,
            "supervisee_user_id": supervisee.user_id,
            "started_at": "2026-01-01T00:00:00+00:00",
        },
        headers=headers,
    )


def _assert_forbidden(response) -> None:
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "forbidden"


def test_editor_outside_the_lab_cannot_assert_supervision(client: TestClient, lab: _Lab):
    # Arbitrary pair, and the finding's scenario: naming oneself the PI's supervisor.
    _assert_forbidden(_create_edge(client, lab.outsider.headers, lab.pi, lab.student))
    _assert_forbidden(_create_edge(client, lab.outsider.headers, lab.outsider, lab.pi))


def test_group_members_who_do_not_own_the_group_cannot_assert_supervision(
    client: TestClient,
    lab: _Lab,
):
    # Being one endpoint is not authority: a student cannot claim a supervisor,
    # and a labmate cannot claim to supervise the PI.
    _assert_forbidden(_create_edge(client, lab.student.headers, lab.pi, lab.student))
    _assert_forbidden(_create_edge(client, lab.labmate.headers, lab.labmate, lab.pi))


def test_group_owner_manages_supervision_within_their_group(client: TestClient, lab: _Lab):
    created = _create_edge(client, lab.pi.headers, lab.pi, lab.student)
    assert created.status_code == 201, created.text
    edge_id = created.json()["data"]["edge_id"]

    retargeted = client.patch(
        f"/supervision-edges/{edge_id}",
        json={"supervisee_user_id": lab.labmate.user_id},
        headers=lab.pi.headers,
    )
    assert retargeted.status_code == 200, retargeted.text

    # The PI cannot move an edge onto someone outside every group they own.
    _assert_forbidden(
        client.patch(
            f"/supervision-edges/{edge_id}",
            json={"supervisee_user_id": lab.outsider.user_id},
            headers=lab.pi.headers,
        )
    )
    _assert_forbidden(_create_edge(client, lab.pi.headers, lab.pi, lab.outsider))

    deleted = client.delete(f"/supervision-edges/{edge_id}", headers=lab.pi.headers)
    assert deleted.status_code == 200


def test_supervision_reads_are_scoped_to_endpoints_and_owned_groups(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    lab: _Lab,
):
    lab_edge = _create_edge(client, admin_auth_headers, lab.pi, lab.student)
    assert lab_edge.status_code == 201
    lab_edge_id = lab_edge.json()["data"]["edge_id"]
    outside_edge = _create_edge(client, admin_auth_headers, lab.outsider, lab.labmate)
    assert outside_edge.status_code == 201
    outside_edge_id = outside_edge.json()["data"]["edge_id"]

    def listed(headers: dict[str, str]) -> tuple[set[str], int]:
        response = client.get("/supervision-edges", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        return {item["edge_id"] for item in body["data"]}, body["meta"]["total"]

    assert listed(admin_auth_headers) == ({lab_edge_id, outside_edge_id}, 2)
    # Owner of the group containing both endpoints of the lab edge only.
    assert listed(lab.pi.headers) == ({lab_edge_id}, 1)
    # Endpoints see the edges that name them, and nothing else.
    assert listed(lab.student.headers) == ({lab_edge_id}, 1)
    assert listed(lab.outsider.headers) == ({outside_edge_id}, 1)
    assert listed(lab.labmate.headers) == ({outside_edge_id}, 1)

    # Targeted reads of an edge outside the caller's scope are opaque 404s.
    hidden = client.get(f"/supervision-edges/{lab_edge_id}", headers=lab.outsider.headers)
    assert hidden.status_code == 404
    assert (
        client.patch(
            f"/supervision-edges/{lab_edge_id}",
            json={"ended_at": "2026-03-01T00:00:00+00:00"},
            headers=lab.outsider.headers,
        ).status_code
        == 404
    )
    assert (
        client.delete(f"/supervision-edges/{lab_edge_id}", headers=lab.outsider.headers)
    ).status_code == 404

    # A visible edge the caller cannot manage is an explicit permission failure.
    visible = client.get(f"/supervision-edges/{lab_edge_id}", headers=lab.student.headers)
    assert visible.status_code == 200
    _assert_forbidden(
        client.patch(
            f"/supervision-edges/{lab_edge_id}",
            json={"ended_at": "2026-03-01T00:00:00+00:00"},
            headers=lab.student.headers,
        )
    )
    _assert_forbidden(
        client.delete(f"/supervision-edges/{lab_edge_id}", headers=lab.student.headers)
    )


def test_supervision_list_paginates_after_scoping(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    lab: _Lab,
):
    # Unscoped edges ahead of the caller's edge must not consume its page.
    for _ in range(3):
        first = _lab_user(client, "other-a")
        second = _lab_user(client, "other-b")
        assert _create_edge(client, admin_auth_headers, first, second).status_code == 201
    mine = _create_edge(client, admin_auth_headers, lab.pi, lab.student)
    assert mine.status_code == 201

    response = client.get(
        "/supervision-edges",
        params={"limit": 1, "offset": 0},
        headers=lab.student.headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["edge_id"] for item in body["data"]] == [mine.json()["data"]["edge_id"]]
    assert body["meta"]["total"] == 1
