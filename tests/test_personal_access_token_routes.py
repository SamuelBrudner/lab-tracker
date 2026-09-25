"""HTTP integration tests for lpat_ personal access tokens."""

from __future__ import annotations

from dataclasses import replace as dataclass_replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from lab_tracker.auth import (
    PAT_SCOPE_ALL,
    PAT_SCOPE_STAGE_EVIDENCE,
    AuthContext,
    PrincipalType,
    Role,
    utc_now,
)
from lab_tracker.errors import ServiceScopeDeniedError
from lab_tracker.models import EntityOrigin, NoteStatus
from lab_tracker.rate_limit import InMemoryRateLimiter
from lab_tracker.routes.shared import (
    ORIGIN_PROVIDER_MAX_LENGTH,
    OriginStamp,
    ensure_scope_allows_evidence_bundle,
    ensure_scope_allows_note_status,
    origin_stamp,
    stamp_kwargs,
)


def _bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _create_token(
    client: TestClient,
    headers: dict[str, str],
    *,
    label: str = "Copilot",
    role: str = "viewer",
    read_only: bool = True,
    scope: str = "all",
) -> dict:
    response = client.post(
        "/auth/tokens",
        json={
            "label": label,
            "role": role,
            "read_only": read_only,
            "scope": scope,
            "expires_at": (utc_now() + timedelta(days=7)).isoformat(),
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


@pytest.mark.parametrize("role,read_only,membership", [
    ("editor", False, "contributor"), ("viewer", True, "viewer"),
])
def test_setup_verifies_token_project_access_before_and_after_membership(
    client, admin_auth_headers, role, read_only, membership
):
    project_id = client.post(
        "/projects", json={"name": "Setup project"}, headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    path = f"/projects/{project_id}/access"
    assert client.get(path, headers=admin_auth_headers).json()["data"]["role"] == "owner"
    issued = _create_token(client, admin_auth_headers, role=role, read_only=read_only)
    token_headers = _bearer(issued["secret"])
    denied = client.get(path, headers=token_headers)
    missing = client.get(f"/projects/{uuid4()}/access", headers=token_headers)
    assert denied.status_code == missing.status_code == 404
    assert denied.json() == missing.json()
    assert client.get("/projects", headers=token_headers).json()["data"] == []

    user_id = client.get("/auth/me", headers=admin_auth_headers).json()["data"]["user_id"]
    body = {"user_id": user_id, "role": membership}
    # A token cannot repair its own access; the human's owner/admin session must.
    assert client.post(
        f"/projects/{project_id}/members", json=body, headers=token_headers,
    ).status_code in {401, 403}
    assert client.post(
        f"/projects/{project_id}/members", json=body, headers=admin_auth_headers,
    ).status_code == 201
    verified = client.get(path, headers=token_headers)
    assert verified.status_code == 200
    assert verified.json()["data"] == {"project_id": project_id, "role": membership}
    assert [p["project_id"] for p in client.get(
        "/projects", headers=token_headers,
    ).json()["data"]] == [project_id]
    capture = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": "Setup capture", "status": "staged"},
        headers=token_headers,
    )
    assert capture.status_code == (403 if read_only else 201)
    client.delete(f"/auth/tokens/{issued['token_id']}", headers=admin_auth_headers)
    assert client.get(path, headers=token_headers).status_code == 401


def test_setup_access_check_recognizes_inherited_group_ownership(client, admin_auth_headers):
    group = client.post("/groups", json={"name": "Setup group"}, headers=admin_auth_headers)
    assert group.status_code == 201
    group_id = group.json()["data"]["group_id"]
    project = client.post(
        "/projects", json={"name": "Inherited setup", "group_id": group_id},
        headers=admin_auth_headers,
    )
    assert project.status_code == 201
    project_id = project.json()["data"]["project_id"]
    user_id = client.get("/auth/me", headers=admin_auth_headers).json()["data"]["user_id"]
    assert client.post(
        f"/groups/{group_id}/members", json={"user_id": user_id, "role": "owner"},
        headers=admin_auth_headers,
    ).status_code == 201
    issued = _create_token(client, admin_auth_headers, role="editor", read_only=False)
    verified = client.get(f"/projects/{project_id}/access", headers=_bearer(issued["secret"]))
    assert verified.status_code == 200
    assert verified.json()["data"]["role"] == "owner"
    # Verification is read-only and does not introduce redundant direct memberships.
    assert client.get(
        f"/projects/{project_id}/members", headers=admin_auth_headers,
    ).json()["data"] == []


def test_batch_run_due_scoped_token_can_only_trigger_the_run(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project = client.post(
        "/projects",
        json={"name": "Scheduler token forbidden reads"},
        headers=admin_auth_headers,
    )
    assert project.status_code == 201, project.text
    goal = client.post(
        f"/projects/{project.json()['data']['project_id']}/goals",
        json={"goal_type": "paper", "title": "Scheduler-hidden goal"},
        headers=admin_auth_headers,
    )
    assert goal.status_code == 201, goal.text
    goal_id = goal.json()["data"]["goal_id"]
    issued = _create_token(
        client, admin_auth_headers, role="admin", scope="batch_run_due"
    )
    assert issued["scope"] == "batch_run_due"
    pat_headers = _bearer(issued["secret"])

    # The scheduler token triggers the due-batch run...
    run_due = client.post("/batches/run-due", headers=pat_headers)
    assert run_due.status_code == 200, run_due.text

    # ...but can do nothing else: not read, not other writes, not /auth.
    denied = [
        client.get("/projects", headers=pat_headers),
        client.get("/batches/runs", headers=pat_headers),
        client.get(f"/goals/{goal_id}", headers=pat_headers),
        client.get(f"/goals/{goal_id}/ara-artifact", headers=pat_headers),
        client.get(f"/data-stores/{uuid4()}/health", headers=pat_headers),
        client.get("/auth/me", headers=pat_headers),
        client.post(
            "/assistant/decision-context",
            json={"task_kind": "summary", "query": "hidden"},
            headers=pat_headers,
        ),
        client.post("/projects", json={"name": "Blocked"}, headers=pat_headers),
        client.post("/batches/run-now", json={}, headers=pat_headers),
    ]
    assert [response.status_code for response in denied] == [403] * len(denied)


def test_create_list_and_revoke_personal_access_token(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    issued = _create_token(client, admin_auth_headers, role="admin")

    assert issued["secret"].startswith("lpat_")
    assert issued["role"] == "admin"
    assert issued["read_only"] is True

    listed = client.get("/auth/tokens", headers=admin_auth_headers)
    assert listed.status_code == 200, listed.text
    listed_token = listed.json()["data"][0]
    assert listed_token["token_id"] == issued["token_id"]
    assert "secret" not in listed_token

    revoked = client.delete(
        f"/auth/tokens/{issued['token_id']}",
        headers=admin_auth_headers,
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["data"]["revoked_at"] is not None


def test_read_only_personal_access_token_can_read_but_not_write_or_auth(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_response = client.post(
        "/projects",
        json={"name": "PAT Read"},
        headers=admin_auth_headers,
    )
    assert project_response.status_code == 201, project_response.text
    issued = _create_token(client, admin_auth_headers, role="admin", read_only=True)
    pat_headers = _bearer(issued["secret"])

    listing = client.get("/projects", headers=pat_headers)
    forbidden_auth = client.get("/auth/me", headers=pat_headers)
    write_attempts = [
        client.post("/projects", json={"name": "Blocked"}, headers=pat_headers),
        client.post(
            "/datasets",
            json={
                "project_id": str(uuid4()),
                "commit_hash": "abc123",
                "primary_question_id": str(uuid4()),
            },
            headers=pat_headers,
        ),
        client.post(
            "/analyses",
            json={
                "dataset_id": str(uuid4()),
                "analysis_type": "notebook",
                "summary": "blocked",
            },
            headers=pat_headers,
        ),
        client.post(
            "/claims",
            json={
                "analysis_id": str(uuid4()),
                "text": "blocked",
                "status": "proposed",
            },
            headers=pat_headers,
        ),
        client.post(
            "/visualizations",
            json={
                "analysis_id": str(uuid4()),
                "viz_type": "figure",
                "file_path": "blocked.png",
            },
            headers=pat_headers,
        ),
    ]

    assert listing.status_code == 200, listing.text
    assert [response.status_code for response in write_attempts] == [403] * len(
        write_attempts
    )
    assert {response.json()["error"]["code"] for response in write_attempts} == {
        "service_forbidden"
    }
    assert forbidden_auth.status_code == 403


def test_read_only_viewer_token_can_read_scoped_decision_context_opaquely(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    scoped_project_member,
) -> None:
    issued = _create_token(
        client,
        scoped_project_member.member_headers,
        label="Read-only decision context",
        role="viewer",
        read_only=True,
    )
    pat_headers = _bearer(issued["secret"])

    visible = client.post(
        "/assistant/decision-context",
        json={
            "task_kind": "summary",
            "query": "scoped context",
            "project_id": scoped_project_member.visible_project_id,
        },
        headers=pat_headers,
    )

    assert visible.status_code == 200, visible.text
    assert visible.json()["data"]["scope"]["project"]["project_id"] == (
        scoped_project_member.visible_project_id
    )

    hidden = client.post(
        "/assistant/decision-context",
        json={
            "task_kind": "summary",
            "query": "scoped context",
            "project_id": scoped_project_member.hidden_project_id,
        },
        headers=pat_headers,
    )
    missing = client.post(
        "/assistant/decision-context",
        json={
            "task_kind": "summary",
            "query": "scoped context",
            "project_id": str(uuid4()),
        },
        headers=pat_headers,
    )

    assert hidden.status_code == missing.status_code == 403
    assert hidden.json() == missing.json() == {
        "error": {
            "code": "forbidden",
            "message": "Project access required.",
            "issues": None,
        }
    }

    hidden_question = client.post(
        "/questions",
        json={
            "project_id": scoped_project_member.hidden_project_id,
            "text": "A hidden decision-context anchor",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=admin_auth_headers,
    )
    assert hidden_question.status_code == 201, hidden_question.text
    hidden_question_id = hidden_question.json()["data"]["question_id"]
    missing_question_id = str(uuid4())

    hidden_anchor = client.post(
        "/assistant/decision-context",
        json={
            "task_kind": "summary",
            "query": "hidden anchor",
            "question_id": hidden_question_id,
        },
        headers=pat_headers,
    )
    missing_anchor = client.post(
        "/assistant/decision-context",
        json={
            "task_kind": "summary",
            "query": "hidden anchor",
            "question_id": missing_question_id,
        },
        headers=pat_headers,
    )

    assert hidden_anchor.status_code == missing_anchor.status_code == 200
    normalized_anchor_errors = []
    for response, supplied_id in (
        (hidden_anchor, hidden_question_id),
        (missing_anchor, missing_question_id),
    ):
        payload = response.json()
        error = payload["error"]
        assert error["code"] == "anchor_not_found"
        assert error["anchor"] == {
            "entity_type": "question",
            "entity_id": supplied_id,
        }
        assert scoped_project_member.hidden_project_id not in response.text
        assert "A hidden decision-context anchor" not in response.text
        error["message"] = error["message"].replace(supplied_id, "<supplied-id>")
        error["anchor"]["entity_id"] = "<supplied-id>"
        normalized_anchor_errors.append(payload)
    assert normalized_anchor_errors[0] == normalized_anchor_errors[1]

    near_miss = client.post(
        "/assistant/decision-context/",
        json={"task_kind": "summary", "query": "scoped context"},
        headers=pat_headers,
        follow_redirects=False,
    )
    assert near_miss.status_code == 403
    assert near_miss.json()["error"]["code"] == "service_forbidden"


def test_write_enabled_viewer_token_does_not_inherit_decision_context_exception(
    client: TestClient,
    scoped_project_member,
) -> None:
    issued = _create_token(
        client,
        scoped_project_member.member_headers,
        label="Write-enabled viewer",
        role="viewer",
        read_only=False,
    )

    response = client.post(
        "/assistant/decision-context",
        json={
            "task_kind": "summary",
            "query": "scoped context",
            "project_id": scoped_project_member.visible_project_id,
        },
        headers=_bearer(issued["secret"]),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "service_forbidden"


def test_write_enabled_token_uses_capped_role_not_live_user_role(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    username = f"viewer-{uuid4().hex[:8]}"
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
    viewer_headers = _bearer(login.json()["data"]["access_token"])
    issued = _create_token(
        client,
        viewer_headers,
        role="admin",
        read_only=False,
    )
    assert issued["role"] == "viewer"

    client.app.state.auth_service.update_user(user.user_id, role=Role.ADMIN)
    create_with_capped_token = client.post(
        "/projects",
        json={"name": "Still blocked"},
        headers=_bearer(issued["secret"]),
    )
    create_with_admin = client.post(
        "/projects",
        json={"name": "Promoted admin can write"},
        headers=admin_auth_headers,
    )

    assert create_with_capped_token.status_code == 403
    assert create_with_admin.status_code == 201


def test_write_enabled_editor_token_can_write(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    issued = _create_token(
        client,
        admin_auth_headers,
        role="editor",
        read_only=False,
    )

    response = client.post(
        "/projects",
        json={"name": "From editor token"},
        headers=_bearer(issued["secret"]),
    )

    assert response.status_code == 201, response.text


def test_invalid_personal_access_token_attempts_are_rate_limited(
    client: TestClient,
):
    client.app.state.pat_rate_limiter.max_attempts = 2
    headers = _bearer("lpat_missing")

    first = client.get("/projects", headers=headers)
    second = client.get("/projects", headers=headers)
    third = client.get("/projects", headers=headers)

    assert first.status_code == 401
    assert second.status_code == 401
    assert third.status_code == 429


def test_one_host_invalid_token_flood_does_not_rate_limit_other_hosts(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    """A host that fills its share of blocked token buckets is limited itself.

    Other hosts keep their share of the PAT table, so their mistyped tokens
    still get 401 and their forbidden requests still get 403, not 429.
    """
    issued = _create_token(client, admin_auth_headers, role="viewer", read_only=True)
    client.app.state.pat_rate_limiter = InMemoryRateLimiter(
        max_attempts=2,
        window_seconds=60,
        max_buckets=6,
        max_buckets_per_client=3,
    )
    attacker = TestClient(client.app, client=("203.0.113.9", 40000))
    other = TestClient(client.app, client=("198.51.100.7", 40000))

    flood = [
        attacker.get("/projects", headers=_bearer(f"lpat_guess-{index}"))
        for index in range(10)
        for _attempt in range(2)
    ]
    mistyped = other.get("/projects", headers=_bearer("lpat_typo"))
    allowed = other.get("/projects", headers=_bearer(issued["secret"]))
    forbidden = other.post(
        "/projects", json={"name": "Blocked"}, headers=_bearer(issued["secret"])
    )

    assert [response.status_code for response in flood[:6]] == [401] * 6
    assert {response.status_code for response in flood[6:]} == {429}
    assert mistyped.status_code == 401
    assert allowed.status_code == 200
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "service_forbidden"


def test_invalid_token_flood_from_one_ipv6_64_shares_one_client_quota(
    client: TestClient,
):
    """Rotating source addresses inside one IPv6 /64 does not mint new clients."""
    client.app.state.pat_rate_limiter = InMemoryRateLimiter(
        max_attempts=1,
        window_seconds=60,
        max_buckets=10,
        max_buckets_per_client=2,
    )

    flood = [
        TestClient(client.app, client=(f"2001:db8:1:2::{index + 1:x}", 40000)).get(
            "/projects", headers=_bearer(f"lpat_guess-{index}")
        )
        for index in range(6)
    ]
    other_prefix = TestClient(client.app, client=("2001:db8:1:3::1", 40000)).get(
        "/projects", headers=_bearer("lpat_typo")
    )

    assert [response.status_code for response in flood] == [401, 401, 429, 429, 429, 429]
    assert other_prefix.status_code == 401


def test_valid_token_forbidden_requests_stay_403_and_never_lock_the_token(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    """Policy denials are not credential failures (L35).

    A valid token that repeatedly asks for something its policy forbids keeps
    getting 403, is never locked out of the requests it may make, and still
    gets 403 (not 429) when its client is at its failure quota.
    """
    issued = _create_token(client, admin_auth_headers, role="viewer", read_only=True)
    client.app.state.pat_rate_limiter = InMemoryRateLimiter(
        max_attempts=2,
        window_seconds=60,
        max_buckets=10,
        max_buckets_per_client=2,
    )
    headers = _bearer(issued["secret"])

    forbidden = [
        client.post("/projects", json={"name": "Blocked"}, headers=headers)
        for _attempt in range(5)
    ]
    allowed = client.get("/projects", headers=headers)
    # Fill this client's failure quota with blocked guesses.
    guesses = [
        client.get("/projects", headers=_bearer(f"lpat_guess-{index}"))
        for index in range(2)
        for _attempt in range(2)
    ]
    forbidden_at_quota = client.post(
        "/projects", json={"name": "Blocked"}, headers=headers
    )

    assert {response.status_code for response in forbidden} == {403}
    assert {response.json()["error"]["code"] for response in forbidden} == {
        "service_forbidden"
    }
    assert allowed.status_code == 200
    assert [response.status_code for response in guesses] == [401, 401, 401, 401]
    assert client.get("/projects", headers=_bearer("lpat_guess-new")).status_code == 429
    assert forbidden_at_quota.status_code == 403
    assert forbidden_at_quota.json()["error"]["code"] == "service_forbidden"


def test_token_without_an_owner_is_a_counted_401_even_on_a_forbidden_path(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
):
    """An ownerless token never reaches the policy check, so a 403 cannot leak it."""
    issued = _create_token(client, admin_auth_headers, role="viewer", read_only=True)
    client.app.state.pat_rate_limiter = InMemoryRateLimiter(
        max_attempts=2,
        window_seconds=60,
        max_buckets=10,
        max_buckets_per_client=2,
    )
    monkeypatch.setattr(client.app.state.auth_service, "get_user_by_id", lambda _user_id: None)
    headers = _bearer(issued["secret"])

    responses = [
        client.post("/projects", json={"name": "Blocked"}, headers=headers) for _ in range(3)
    ]

    assert [response.status_code for response in responses] == [401, 401, 429]
    assert responses[0].json()["error"]["message"] == "Invalid personal access token."


def test_auth_middleware_matches_paths_relative_to_the_root_path(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    """Under a mounted root path the public and /auth policies still apply (L33/L34).

    ASGI servers put the root path in front of ``scope["path"]``; the router
    strips it, so the auth middleware must match the same route-relative path.
    """
    target_id, _ = _register_and_login(client, role=Role.VIEWER, prefix="target")
    admin_pat = _create_token(client, admin_auth_headers, role="admin", read_only=False)
    rooted = TestClient(client.app, root_path="/lab")

    health = rooted.get("/lab/health")
    app_shell = rooted.get("/lab/app")
    promote = rooted.patch(
        f"/lab/auth/users/{target_id}",
        json={"role": "admin"},
        headers=_bearer(admin_pat["secret"]),
    )

    assert health.status_code == 200, health.text
    assert app_shell.status_code == 200, app_shell.text
    assert "frame-ancestors 'none'" in app_shell.headers["Content-Security-Policy"]
    assert promote.status_code == 403, promote.text
    assert promote.json()["error"]["code"] == "service_forbidden"
    target = client.app.state.auth_service.get_user_by_id(UUID(target_id))
    assert target.role is Role.VIEWER
    # A path outside the root path reaches the router verbatim; the fence still holds.
    unrooted = rooted.get("/auth/users", headers=_bearer(admin_pat["secret"]))
    assert unrooted.status_code == 403, unrooted.text

    enrollment = client.post("/auth/devices/enrollment", json={}, headers=admin_auth_headers)
    assert enrollment.status_code == 201, enrollment.text
    consume = client.post(
        "/auth/devices/consume",
        json={"offer_token": enrollment.json()["data"]["offer_token"], "label": "phone"},
    )
    assert consume.status_code == 201, consume.text
    device = _bearer(consume.json()["data"]["secret"])

    assert rooted.get("/lab/auth/me", headers=device).status_code == 200
    for method, path in (
        ("GET", "/lab/auth/users"),
        ("GET", "/lab/auth/devices"),
        ("POST", "/lab/projects"),
    ):
        denied = rooted.request(method, path, json={"name": "x"}, headers=device)
        assert denied.status_code == 403, (path, denied.text)


def _register_and_login(
    client: TestClient,
    *,
    role: Role,
    prefix: str,
) -> tuple[str, dict[str, str]]:
    username = f"{prefix}-{uuid4().hex[:8]}"
    user = client.app.state.auth_service.register_user(
        username=username,
        password="secret",
        role=role,
    )
    login = client.post("/auth/login", json={"username": username, "password": "secret"})
    assert login.status_code == 200, login.text
    return str(user.user_id), _bearer(login.json()["data"]["access_token"])


def test_personal_access_token_loses_authority_when_its_user_is_demoted(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    root_project = client.post(
        "/projects",
        json={"name": "Root-only project"},
        headers=admin_auth_headers,
    )
    assert root_project.status_code == 201, root_project.text
    root_project_id = root_project.json()["data"]["project_id"]
    second_id, second_headers = _register_and_login(client, role=Role.ADMIN, prefix="second")
    issued = _create_token(client, second_headers, role="admin", read_only=False)
    pat_headers = _bearer(issued["secret"])
    before = client.get("/projects", headers=pat_headers)
    assert root_project_id in {item["project_id"] for item in before.json()["data"]}

    demoted = client.patch(
        f"/auth/users/{second_id}",
        json={"role": "viewer"},
        headers=admin_auth_headers,
    )
    assert demoted.status_code == 200, demoted.text

    listing = client.get("/projects", headers=pat_headers)
    create = client.post("/projects", json={"name": "Blocked"}, headers=pat_headers)
    delete = client.delete(f"/projects/{root_project_id}", headers=pat_headers)

    assert listing.status_code == 200, listing.text
    assert root_project_id not in {item["project_id"] for item in listing.json()["data"]}
    assert create.status_code == 403
    assert create.json()["error"]["code"] == "service_forbidden"
    assert delete.status_code == 403
    assert client.get(f"/projects/{root_project_id}", headers=admin_auth_headers).status_code == 200


def test_admin_can_list_and_revoke_another_users_personal_access_tokens(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    owner_id, owner_headers = _register_and_login(client, role=Role.EDITOR, prefix="owner")
    issued = _create_token(client, owner_headers, role="editor", read_only=False)
    _create_token(client, admin_auth_headers, label="Admin's own")

    listed = client.get(f"/auth/users/{owner_id}/tokens", headers=admin_auth_headers)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert [item["token_id"] for item in body["data"]] == [issued["token_id"]]
    assert body["meta"] == {"limit": 50, "offset": 0, "total": 1}
    assert "secret" not in body["data"][0]

    revoked = client.delete(
        f"/auth/users/{owner_id}/tokens/{issued['token_id']}",
        headers=admin_auth_headers,
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["data"]["revoked_at"] is not None
    assert client.get("/projects", headers=_bearer(issued["secret"])).status_code == 401


def test_admin_token_management_rejects_mismatched_and_unknown_targets(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    owner_id, owner_headers = _register_and_login(client, role=Role.EDITOR, prefix="owner")
    other_id, _ = _register_and_login(client, role=Role.VIEWER, prefix="other")
    issued = _create_token(client, owner_headers)

    mismatched = client.delete(
        f"/auth/users/{other_id}/tokens/{issued['token_id']}",
        headers=admin_auth_headers,
    )
    unknown_user = client.get(f"/auth/users/{uuid4()}/tokens", headers=admin_auth_headers)

    assert mismatched.status_code == 404
    assert unknown_user.status_code == 404
    assert client.get("/projects", headers=_bearer(issued["secret"])).status_code == 200
    assert owner_id != other_id


def test_admin_token_management_requires_an_interactive_admin(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    owner_id, owner_headers = _register_and_login(client, role=Role.EDITOR, prefix="owner")
    issued = _create_token(client, owner_headers)
    admin_pat = _create_token(client, admin_auth_headers, role="admin", read_only=False)

    by_editor = client.get(f"/auth/users/{owner_id}/tokens", headers=owner_headers)
    revoke_by_editor = client.delete(
        f"/auth/users/{owner_id}/tokens/{issued['token_id']}",
        headers=owner_headers,
    )
    by_service = client.get(
        f"/auth/users/{owner_id}/tokens",
        headers=_bearer(admin_pat["secret"]),
    )
    revoke_by_service = client.delete(
        f"/auth/users/{owner_id}/tokens/{issued['token_id']}",
        headers=_bearer(admin_pat["secret"]),
    )

    assert by_editor.status_code == 403
    assert by_editor.json()["error"]["message"] == "Admin privileges required."
    assert revoke_by_editor.status_code == 403
    assert by_service.status_code == 403
    assert revoke_by_service.status_code == 403
    assert client.get("/projects", headers=_bearer(issued["secret"])).status_code == 200


def _login_headers(client: TestClient, user_id: str) -> dict[str, str]:
    user = client.app.state.auth_service.get_user_by_id(UUID(user_id))
    login = client.post("/auth/login", json={"username": user.username, "password": "secret"})
    assert login.status_code == 200, login.text
    return _bearer(login.json()["data"]["access_token"])


def test_token_listings_report_the_effective_role_after_the_owner_is_demoted(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    second_id, second_headers = _register_and_login(client, role=Role.ADMIN, prefix="second")
    issued = _create_token(client, second_headers, role="admin", read_only=False)
    assert (issued["role"], issued["effective_role"]) == ("admin", "admin")

    demoted = client.patch(
        f"/auth/users/{second_id}",
        json={"role": "viewer"},
        headers=admin_auth_headers,
    )
    assert demoted.status_code == 200, demoted.text
    # The role change ended the old session; sign in again as the demoted user.
    second_headers = _login_headers(client, second_id)

    own = client.get("/auth/tokens", headers=second_headers)
    by_admin = client.get(f"/auth/users/{second_id}/tokens", headers=admin_auth_headers)
    revoked = client.delete(
        f"/auth/users/{second_id}/tokens/{issued['token_id']}",
        headers=admin_auth_headers,
    )

    assert own.status_code == 200, own.text
    assert by_admin.status_code == 200, by_admin.text
    assert revoked.status_code == 200, revoked.text
    for item in (own.json()["data"][0], by_admin.json()["data"][0], revoked.json()["data"]):
        assert item["token_id"] == issued["token_id"]
        assert item["role"] == "admin"
        assert item["effective_role"] == "viewer"


def test_token_listings_do_not_widen_the_effective_role_after_promotion(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    owner_id, owner_headers = _register_and_login(client, role=Role.EDITOR, prefix="owner")
    issued = _create_token(client, owner_headers, role="admin", read_only=False)
    assert (issued["role"], issued["effective_role"]) == ("editor", "editor")

    promoted = client.patch(
        f"/auth/users/{owner_id}",
        json={"role": "admin"},
        headers=admin_auth_headers,
    )
    assert promoted.status_code == 200, promoted.text

    listed = client.get(f"/auth/users/{owner_id}/tokens", headers=admin_auth_headers)
    assert listed.status_code == 200, listed.text
    item = listed.json()["data"][0]
    assert (item["role"], item["effective_role"]) == ("editor", "editor")


def _assert_service_forbidden(response) -> None:
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "service_forbidden"


def _grant_token_user_contributor_access(
    client: TestClient, admin_auth_headers: dict[str, str], project_id: str
) -> None:
    """A personal token uses its own role, so its user needs project membership."""
    user_id = client.get("/auth/me", headers=admin_auth_headers).json()["data"]["user_id"]
    response = client.post(
        f"/projects/{project_id}/members",
        json={"user_id": user_id, "role": "contributor"},
        headers=admin_auth_headers,
    )
    assert response.status_code == 201, response.text


def _service_actor(scope: str, label: str = "Codex hook") -> AuthContext:
    return AuthContext(
        user_id=uuid4(),
        role=Role.EDITOR,
        principal_type=PrincipalType.SERVICE,
        principal_label=label,
        service_scope=scope,
    )


def test_create_token_accepts_stage_evidence_scope_and_rejects_unknown(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    issued = _create_token(
        client, admin_auth_headers, role="editor", read_only=False, scope="stage_evidence"
    )
    assert issued["scope"] == "stage_evidence"

    rejected = client.post(
        "/auth/tokens",
        json={
            "label": "Unknown scope",
            "role": "editor",
            "read_only": False,
            "scope": "everything",
            "expires_at": (utc_now() + timedelta(days=7)).isoformat(),
        },
        headers=admin_auth_headers,
    )
    assert rejected.status_code == 422, rejected.text


def test_service_auth_context_carries_scope_and_label(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    def read_auth_context(request: Request) -> dict[str, object]:
        context = request.state.auth_context
        return {
            "principal_type": context.principal_type.value,
            "service_scope": context.service_scope,
            "principal_label": context.principal_label,
            "is_stage_evidence_scoped": context.is_stage_evidence_scoped,
        }

    client.app.add_api_route("/_test/auth-context", read_auth_context, methods=["GET"])
    issued = _create_token(
        client,
        admin_auth_headers,
        label="Hook bot",
        role="editor",
        read_only=False,
        scope="stage_evidence",
    )

    via_token = client.get("/_test/auth-context", headers=_bearer(issued["secret"]))
    via_browser = client.get("/_test/auth-context", headers=admin_auth_headers)

    assert via_token.status_code == 200, via_token.text
    assert via_token.json() == {
        "principal_type": "service",
        "service_scope": "stage_evidence",
        "principal_label": "Hook bot",
        "is_stage_evidence_scoped": True,
    }
    assert via_browser.status_code == 200, via_browser.text
    assert via_browser.json() == {
        "principal_type": "user",
        "service_scope": None,
        "principal_label": None,
        "is_stage_evidence_scoped": False,
    }


def test_origin_stamp_records_token_label_for_service_principals() -> None:
    service = _service_actor(PAT_SCOPE_ALL, label="Codex hook")
    browser = AuthContext(user_id=uuid4(), role=Role.ADMIN)
    device = AuthContext(
        user_id=uuid4(),
        role=Role.EDITOR,
        principal_type=PrincipalType.DEVICE,
        device_token_id=uuid4(),
        principal_label="Bench phone",
    )

    assert origin_stamp(service, EntityOrigin.AI_EXECUTED) == OriginStamp(
        origin=EntityOrigin.AI_EXECUTED, origin_provider="Codex hook"
    )
    assert origin_stamp(browser, EntityOrigin.USER) == OriginStamp(
        origin=EntityOrigin.USER, origin_provider=None
    )
    # Only service tokens are recorded as the provider; a paired device is
    # already stamped into the capture metadata.
    assert origin_stamp(device, EntityOrigin.USER).origin_provider is None

    long_label = "L" * 150
    truncated = origin_stamp(
        dataclass_replace(service, principal_label=long_label), EntityOrigin.USER
    )
    assert truncated.origin_provider == "L" * ORIGIN_PROVIDER_MAX_LENGTH
    assert len(truncated.origin_provider) == 80
    assert stamp_kwargs(truncated) == {
        "origin": EntityOrigin.USER,
        "origin_provider": "L" * ORIGIN_PROVIDER_MAX_LENGTH,
    }


def test_ensure_scope_allows_note_status_only_gates_stage_scoped_service_actors() -> None:
    browser = AuthContext(user_id=uuid4(), role=Role.ADMIN)
    all_scoped = _service_actor(PAT_SCOPE_ALL)
    stage_scoped = _service_actor(PAT_SCOPE_STAGE_EVIDENCE)

    ensure_scope_allows_note_status(browser, NoteStatus.COMMITTED)
    ensure_scope_allows_note_status(all_scoped, NoteStatus.COMMITTED)
    ensure_scope_allows_note_status(stage_scoped, NoteStatus.STAGED)
    with pytest.raises(ServiceScopeDeniedError, match="only stage notes"):
        ensure_scope_allows_note_status(stage_scoped, NoteStatus.COMMITTED)
    with pytest.raises(ServiceScopeDeniedError):
        ensure_scope_allows_note_status(stage_scoped, NoteStatus.ARCHIVED)


def test_ensure_scope_allows_evidence_bundle_rejects_commit_for_stage_scope() -> None:
    all_scoped = _service_actor(PAT_SCOPE_ALL)
    stage_scoped = _service_actor(PAT_SCOPE_STAGE_EVIDENCE)

    ensure_scope_allows_evidence_bundle(all_scoped, dry_run=False)
    ensure_scope_allows_evidence_bundle(stage_scoped, dry_run=True)
    with pytest.raises(ServiceScopeDeniedError, match="dry_run=true"):
        ensure_scope_allows_evidence_bundle(stage_scoped, dry_run=False)


def test_stage_evidence_token_can_stage_notes_but_not_commit_them(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project = client.post("/projects", json={"name": "Stage scope"}, headers=admin_auth_headers)
    assert project.status_code == 201, project.text
    project_id = project.json()["data"]["project_id"]
    _grant_token_user_contributor_access(client, admin_auth_headers, project_id)
    issued = _create_token(
        client, admin_auth_headers, label="CI hook", role="editor", read_only=False,
        scope="stage_evidence",
    )
    pat_headers = _bearer(issued["secret"])

    staged = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": "Staged by the hook"},
        headers=pat_headers,
    )
    assert staged.status_code == 201, staged.text
    assert staged.json()["data"]["status"] == "staged"
    note_id = staged.json()["data"]["note_id"]

    _assert_service_forbidden(
        client.post(
            "/notes",
            json={"project_id": project_id, "raw_content": "Committed", "status": "committed"},
            headers=pat_headers,
        )
    )
    _assert_service_forbidden(
        client.post(
            "/notes/upload-file",
            data={"project_id": project_id, "status": "committed"},
            files={"file": ("snap.jpg", b"image-bytes", "image/jpeg")},
            headers=pat_headers,
        )
    )
    quick = client.post(
        "/notes/quick-capture",
        data={"project_id": project_id},
        files={"file": ("snap2.jpg", b"image-2", "image/jpeg")},
        headers=pat_headers,
    )
    assert quick.status_code == 201, quick.text

    patched = client.patch(
        f"/notes/{note_id}", json={"transcribed_text": "typed up"}, headers=pat_headers
    )
    assert patched.status_code == 200, patched.text
    _assert_service_forbidden(
        client.patch(f"/notes/{note_id}", json={"status": "committed"}, headers=pat_headers)
    )
    restaged = client.patch(f"/notes/{note_id}", json={"status": "staged"}, headers=pat_headers)
    assert restaged.status_code == 200, restaged.text

    # The transcript action is admitted by the policy; a text note simply has
    # nothing to transcribe, which the route reports as a client error.
    transcript = client.post(f"/notes/{note_id}/transcript", headers=pat_headers)
    assert transcript.status_code != 403, transcript.text
    assert transcript.status_code < 500, transcript.text

    listing = client.get("/notes", params={"project_id": project_id}, headers=pat_headers)
    assert listing.status_code == 200, listing.text
    assert len(listing.json()["data"]) == 2

    denied = [
        client.post("/projects", json={"name": "Blocked"}, headers=pat_headers),
        client.post(
            "/questions",
            json={"project_id": project_id, "text": "Blocked?", "question_type": "other"},
            headers=pat_headers,
        ),
        client.post(
            "/datasets",
            json={"project_id": project_id, "primary_question_id": str(uuid4())},
            headers=pat_headers,
        ),
        client.post("/batches/run-due", headers=pat_headers),
        client.post(f"/notes/{note_id}/archive", headers=pat_headers),
        client.delete(f"/notes/{note_id}", headers=pat_headers),
        client.get("/auth/me", headers=pat_headers),
    ]
    for response in denied:
        _assert_service_forbidden(response)

    committed = client.get(
        "/notes",
        params={"project_id": project_id, "status": "committed"},
        headers=admin_auth_headers,
    )
    assert committed.json()["data"] == []


def test_stage_evidence_token_writes_stamp_origin_provider_with_the_token_label(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project = client.post("/projects", json={"name": "Stamped"}, headers=admin_auth_headers)
    project_id = project.json()["data"]["project_id"]
    _grant_token_user_contributor_access(client, admin_auth_headers, project_id)
    issued = _create_token(
        client, admin_auth_headers, label="CI hook", role="editor", read_only=False,
        scope="stage_evidence",
    )
    pat_headers = _bearer(issued["secret"])

    by_token = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": "Hook capture"},
        headers=pat_headers,
    )
    by_upload = client.post(
        "/notes/quick-capture",
        data={"project_id": project_id},
        files={"file": ("snap.jpg", b"image-bytes", "image/jpeg")},
        headers=pat_headers,
    )
    by_browser = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": "Typed capture"},
        headers=admin_auth_headers,
    )

    assert by_token.status_code == 201, by_token.text
    assert by_token.json()["data"]["origin"] == "user"
    assert by_token.json()["data"]["origin_provider"] == "CI hook"
    assert by_upload.status_code == 201, by_upload.text
    assert by_upload.json()["data"]["origin"] == "user"
    assert by_upload.json()["data"]["origin_provider"] == "CI hook"
    assert by_browser.status_code == 201, by_browser.text
    assert by_browser.json()["data"]["origin"] == "user"
    assert by_browser.json()["data"]["origin_provider"] is None
