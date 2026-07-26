from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from lab_tracker.app_parts.middleware import _apply_store_health_admission
from lab_tracker.auth import AuthContext, Role
from lab_tracker.store_health import (
    CachedStoreHealthProbe,
    StoreHealth,
    StoreHealthStatus,
    StoreProbeTarget,
)
from lab_tracker.store_health_admission import StoreHealthAdmission


class _ControlFlowExit(BaseException):
    pass


def _request_for_actor(
    admission: StoreHealthAdmission,
    actor: AuthContext,
    *,
    method: str = "GET",
    path: str = "/data-stores/not-a-uuid/health",
    root_path: str = "",
) -> Request:
    app = FastAPI()
    app.state.store_health_admission = admission
    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": root_path,
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "state": {},
            "app": app,
        }
    )
    request.state.auth_context = actor
    return request


def _assert_capacity_recovered(
    admission: StoreHealthAdmission,
    actor_id: UUID,
) -> None:
    lease = admission.try_acquire(actor_id)
    assert lease is not None
    lease.release()


@pytest.mark.parametrize(
    ("method", "path", "expected_status"),
    [
        ("GET", "/data-stores/not-a-uuid/health", 429),
        ("GET", "/data-stores/00000000-0000-0000-0000-000000000001/health", 429),
        ("GET", "/data-stores/not-a-uuid/health/", 204),
        ("GET", "/data-stores//health", 204),
        ("GET", "/data-stores/a/b/health", 204),
        ("GET", "/prefix/data-stores/a/health", 204),
        ("GET", "/DATA-STORES/a/health", 204),
        ("POST", "/data-stores/a/health", 204),
        ("HEAD", "/data-stores/a/health", 204),
    ],
)
def test_health_admission_matches_only_the_exact_get_shape(
    method: str,
    path: str,
    expected_status: int,
) -> None:
    actor = AuthContext(user_id=uuid4(), role=Role.VIEWER)
    admission = StoreHealthAdmission(
        global_in_flight_limit=1,
        per_actor_in_flight_limit=1,
    )
    held = admission.try_acquire(actor.user_id)
    assert held is not None
    request = _request_for_actor(admission, actor, method=method, path=path)

    async def admitted_next(_request: Request) -> JSONResponse:
        return JSONResponse(status_code=204, content=None)

    try:
        response = asyncio.run(_apply_store_health_admission(request, admitted_next))
    finally:
        held.release()

    assert response.status_code == expected_status


def test_health_admission_matches_the_route_relative_to_root_path() -> None:
    actor = AuthContext(user_id=uuid4(), role=Role.VIEWER)
    admission = StoreHealthAdmission(
        global_in_flight_limit=1,
        per_actor_in_flight_limit=1,
    )
    held = admission.try_acquire(actor.user_id)
    assert held is not None
    request = _request_for_actor(
        admission,
        actor,
        path="/prefix/data-stores/not-a-uuid/health",
        root_path="/prefix",
    )

    async def unexpected_next(_request: Request) -> JSONResponse:
        raise AssertionError("root-path health request bypassed admission")

    try:
        response = asyncio.run(_apply_store_health_admission(request, unexpected_next))
    finally:
        held.release()

    assert response.status_code == 429


@pytest.mark.parametrize(
    "failure_type",
    [RuntimeError, asyncio.CancelledError, _ControlFlowExit],
)
def test_health_admission_releases_lease_for_all_exception_hierarchies(
    failure_type: type[BaseException],
) -> None:
    admission = StoreHealthAdmission(
        global_in_flight_limit=1,
        per_actor_in_flight_limit=1,
    )
    actor = AuthContext(user_id=uuid4(), role=Role.VIEWER)
    request = _request_for_actor(admission, actor)

    async def failing_call_next(_request: Request) -> JSONResponse:
        raise failure_type()

    with pytest.raises(failure_type):
        asyncio.run(_apply_store_health_admission(request, failing_call_next))

    _assert_capacity_recovered(admission, actor.user_id)


def test_health_admission_releases_lease_after_success() -> None:
    admission = StoreHealthAdmission(
        global_in_flight_limit=1,
        per_actor_in_flight_limit=1,
    )
    actor = AuthContext(user_id=uuid4(), role=Role.VIEWER)
    request = _request_for_actor(admission, actor)

    async def successful_call_next(_request: Request) -> JSONResponse:
        return JSONResponse(status_code=204, content=None)

    response = asyncio.run(_apply_store_health_admission(request, successful_call_next))

    assert response.status_code == 204
    _assert_capacity_recovered(admission, actor.user_id)


def _actor_id(client: TestClient, headers: dict[str, str]) -> UUID:
    token = headers["Authorization"].removeprefix("Bearer ")
    return client.app.state.token_service.verify_access_token(token).user_id


def test_saturated_health_is_opaque_and_bypasses_the_request_scope(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    viewer_user,
) -> None:
    third_user = client.app.state.auth_service.register_user(
        username=f"third-health-admission-{uuid4().hex}",
        password="secret",
        role=Role.VIEWER,
    )
    third_headers = {
        "Authorization": (
            "Bearer "
            + client.app.state.token_service.issue_access_token(third_user).token
        )
    }

    actor_admission = StoreHealthAdmission(
        global_in_flight_limit=2,
        per_actor_in_flight_limit=1,
    )
    actor_lease = actor_admission.try_acquire(_actor_id(client, admin_auth_headers))
    assert actor_lease is not None

    global_admission = StoreHealthAdmission(
        global_in_flight_limit=2,
        per_actor_in_flight_limit=1,
    )
    global_admin_lease = global_admission.try_acquire(
        _actor_id(client, admin_auth_headers)
    )
    global_viewer_lease = global_admission.try_acquire(
        _actor_id(client, viewer_user.headers)
    )
    assert global_admin_lease is not None
    assert global_viewer_lease is not None

    original_factory = client.app.state.db_session_factory
    original_checker = client.app.state.store_health_checker
    original_admission = client.app.state.store_health_admission

    def unexpected_request_scope() -> None:
        raise AssertionError(
            "saturated health must not allocate an ordinary request session"
        )

    def unexpected_checker(*_args, **_kwargs) -> None:
        raise AssertionError("saturated health must not invoke the checker")

    client.app.state.db_session_factory = unexpected_request_scope
    client.app.state.store_health_checker = unexpected_checker
    try:
        client.app.state.store_health_admission = actor_admission
        actor_saturated = client.get(
            "/data-stores/existing/health",
            headers=admin_auth_headers,
        )
        malformed_saturated = client.get(
            "/data-stores/not-a-uuid/health",
            headers=admin_auth_headers,
        )
        client.app.state.store_health_admission = global_admission
        globally_saturated = client.get(
            "/data-stores/missing/health",
            headers=third_headers,
        )
        unauthenticated = client.get("/data-stores/missing/health")
    finally:
        client.app.state.db_session_factory = original_factory
        client.app.state.store_health_checker = original_checker
        client.app.state.store_health_admission = original_admission
        actor_lease.release()
        global_admin_lease.release()
        global_viewer_lease.release()

    assert actor_saturated.status_code == 429
    assert malformed_saturated.status_code == 429
    assert globally_saturated.status_code == 429
    assert actor_saturated.content == malformed_saturated.content
    assert actor_saturated.content == globally_saturated.content
    assert actor_saturated.headers["Retry-After"] == "1"
    assert globally_saturated.headers["Retry-After"] == "1"
    assert actor_saturated.headers["X-Content-Type-Options"] == "nosniff"
    assert globally_saturated.headers["X-Content-Type-Options"] == "nosniff"
    assert unauthenticated.status_code == 401


def test_health_saturation_does_not_capture_unrelated_routes(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    actor_id = _actor_id(client, admin_auth_headers)
    admission = StoreHealthAdmission(
        global_in_flight_limit=1,
        per_actor_in_flight_limit=1,
    )
    held = admission.try_acquire(actor_id)
    assert held is not None
    original = client.app.state.store_health_admission
    client.app.state.store_health_admission = admission
    try:
        response = client.get("/projects", headers=admin_auth_headers)
        encoded_slash = client.get(
            "/data-stores/a%2Fb/health",
            headers=admin_auth_headers,
        )
    finally:
        client.app.state.store_health_admission = original
        held.release()

    assert response.status_code == 200
    assert encoded_slash.status_code != 429


def test_saturated_root_path_health_never_reaches_the_request_scope(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    actor_id = _actor_id(client, admin_auth_headers)
    admission = StoreHealthAdmission(
        global_in_flight_limit=1,
        per_actor_in_flight_limit=1,
    )
    held = admission.try_acquire(actor_id)
    assert held is not None
    original_admission = client.app.state.store_health_admission
    original_factory = client.app.state.db_session_factory

    def unexpected_request_scope() -> None:
        raise AssertionError(
            "root-path health saturation must precede the ordinary request session"
        )

    client.app.state.store_health_admission = admission
    client.app.state.db_session_factory = unexpected_request_scope
    try:
        with TestClient(
            client.app,
            root_path="/prefix",
            raise_server_exceptions=False,
        ) as prefixed_client:
            response = prefixed_client.get(
                "/prefix/data-stores/not-a-uuid/health",
                headers=admin_auth_headers,
            )
    finally:
        client.app.state.store_health_admission = original_admission
        client.app.state.db_session_factory = original_factory
        held.release()

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "1"


def test_admitted_malformed_store_id_remains_422_and_releases_capacity(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    actor_id = _actor_id(client, admin_auth_headers)
    admission = StoreHealthAdmission(
        global_in_flight_limit=1,
        per_actor_in_flight_limit=1,
    )
    original = client.app.state.store_health_admission
    client.app.state.store_health_admission = admission
    try:
        response = client.get(
            "/data-stores/not-a-uuid/health",
            headers=admin_auth_headers,
        )
        _assert_capacity_recovered(admission, actor_id)
    finally:
        client.app.state.store_health_admission = original

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_error"


def test_http_singleflight_waiter_timeout_is_generic_and_keeps_the_leader(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    viewer_user,
    tmp_path,
) -> None:
    project = client.post(
        "/projects",
        json={"name": "Store health single-flight HTTP"},
        headers=admin_auth_headers,
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["data"]["project_id"]
    membership = client.post(
        f"/projects/{project_id}/members",
        json={"user_id": viewer_user.user_id, "role": "viewer"},
        headers=admin_auth_headers,
    )
    assert membership.status_code == 201, membership.text
    store = client.post(
        "/data-stores",
        json={
            "project_id": project_id,
            "name": "single-flight-http",
            "kind": "local_fs",
            "root": str(tmp_path),
        },
        headers=admin_auth_headers,
    )
    assert store.status_code == 201, store.text
    store_id = store.json()["data"]["store_id"]

    entered = Event()
    release = Event()
    calls_lock = Lock()
    calls = 0

    def blocking_probe(_target: StoreProbeTarget) -> StoreHealth:
        nonlocal calls
        with calls_lock:
            calls += 1
        entered.set()
        assert release.wait(timeout=5)
        return StoreHealth(StoreHealthStatus.HEALTHY)

    original_checker = client.app.state.store_health_checker
    checker = CachedStoreHealthProbe(
        blocking_probe,
        singleflight_wait_seconds=0.05,
    )
    client.app.state.store_health_checker = checker
    http_client = TestClient(client.app, raise_server_exceptions=False)
    executor = ThreadPoolExecutor(max_workers=2)
    try:
        leader = executor.submit(
            http_client.get,
            f"/data-stores/{store_id}/health",
            headers=admin_auth_headers,
        )
        assert entered.wait(timeout=5)
        follower = executor.submit(
            http_client.get,
            f"/data-stores/{store_id}/health",
            headers=viewer_user.headers,
        )
        follower_response = follower.result(timeout=5)
        assert leader.done() is False
        assert checker.in_flight_count == 1
        assert checker.entry_count == 0

        release.set()
        leader_response = leader.result(timeout=5)
        cached_response = http_client.get(
            f"/data-stores/{store_id}/health",
            headers=viewer_user.headers,
        )
    finally:
        release.set()
        executor.shutdown(wait=True)
        http_client.close()
        client.app.state.store_health_checker = original_checker

    assert follower_response.status_code == 500
    assert follower_response.json() == {
        "error": {
            "code": "internal_server_error",
            "message": "Internal server error.",
            "issues": None,
        }
    }
    assert leader_response.status_code == 200, leader_response.text
    assert cached_response.status_code == 200, cached_response.text
    assert calls == 1
    assert checker.in_flight_count == 0
    assert checker.entry_count == 1


def test_missing_checker_wiring_fails_closed_with_generic_error(
    app: FastAPI,
    tmp_path,
) -> None:
    user = app.state.auth_service.register_user(
        username=f"missing-health-checker-{uuid4().hex}",
        password="secret",
        role=Role.ADMIN,
    )
    headers = {
        "Authorization": (
            "Bearer " + app.state.token_service.issue_access_token(user).token
        )
    }
    with TestClient(app, raise_server_exceptions=False) as failing_client:
        project = failing_client.post(
            "/projects",
            json={"name": "Missing health checker"},
            headers=headers,
        )
        assert project.status_code == 201, project.text
        store = failing_client.post(
            "/data-stores",
            json={
                "project_id": project.json()["data"]["project_id"],
                "name": "missing-checker",
                "kind": "local_fs",
                "root": str(tmp_path),
            },
            headers=headers,
        )
        assert store.status_code == 201, store.text

        original_checker: Callable[..., object] = app.state.store_health_checker
        del app.state.store_health_checker
        try:
            response = failing_client.get(
                f"/data-stores/{store.json()['data']['store_id']}/health",
                headers=headers,
            )
            unrelated = failing_client.get("/projects", headers=headers)
        finally:
            app.state.store_health_checker = original_checker

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_server_error",
            "message": "Internal server error.",
            "issues": None,
        }
    }
    assert unrelated.status_code == 200, unrelated.text
