from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from lab_tracker.app_parts.middleware import _apply_artifact_resolution_admission
from lab_tracker.artifact_resolution_admission import ArtifactResolutionAdmission
from lab_tracker.auth import AuthContext, Role


class _ControlFlowExit(BaseException):
    pass


@pytest.mark.parametrize(
    "kwargs",
    [
        {"global_in_flight_limit": 1.5, "per_actor_in_flight_limit": 1},
        {"global_in_flight_limit": 1, "per_actor_in_flight_limit": 1.5},
        {"global_in_flight_limit": True, "per_actor_in_flight_limit": 1},
        {"global_in_flight_limit": 1, "per_actor_in_flight_limit": True},
    ],
)
def test_admission_rejects_non_integral_or_boolean_limits(kwargs):
    with pytest.raises(ValueError, match="must be an integer"):
        ArtifactResolutionAdmission(**kwargs)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {"global_in_flight_limit": 0, "per_actor_in_flight_limit": 1},
            "global_in_flight_limit must be an integer of at least 1",
        ),
        (
            {"global_in_flight_limit": 33, "per_actor_in_flight_limit": 1},
            "global_in_flight_limit must be no greater than 32",
        ),
        (
            {"global_in_flight_limit": 2, "per_actor_in_flight_limit": 0},
            "per_actor_in_flight_limit must be an integer of at least 1",
        ),
        (
            {"global_in_flight_limit": 2, "per_actor_in_flight_limit": 3},
            "per_actor_in_flight_limit must be no greater than global_in_flight_limit",
        ),
    ],
)
def test_admission_rejects_out_of_range_or_inconsistent_limits(kwargs, message):
    with pytest.raises(ValueError, match=message):
        ArtifactResolutionAdmission(**kwargs)


def _request_for_actor(
    admission: ArtifactResolutionAdmission,
    actor: AuthContext,
    *,
    path: str = "/external-artifacts/resolve",
    root_path: str = "",
) -> Request:
    app = FastAPI()
    app.state.artifact_resolution_admission = admission
    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
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
    admission: ArtifactResolutionAdmission,
    actor_id: UUID,
) -> None:
    lease = admission.try_acquire(actor_id)
    assert lease is not None
    lease.release()


def test_admission_enforces_limits_atomically_and_leases_are_idempotent():
    admission = ArtifactResolutionAdmission(
        global_in_flight_limit=4,
        per_actor_in_flight_limit=4,
    )
    actor_ids = [uuid4() for _ in range(16)]
    start = threading.Barrier(len(actor_ids))

    def acquire(actor_id: UUID):
        start.wait()
        return admission.try_acquire(actor_id)

    with ThreadPoolExecutor(max_workers=len(actor_ids)) as executor:
        leases = list(executor.map(acquire, actor_ids))

    admitted = [lease for lease in leases if lease is not None]
    assert len(admitted) == 4
    for lease in admitted:
        lease.release()
        lease.release()

    replacement_leases = [admission.try_acquire(actor_id) for actor_id in actor_ids[:4]]
    assert all(lease is not None for lease in replacement_leases)
    for lease in replacement_leases:
        assert lease is not None
        lease.release()


def test_admission_distinguishes_actor_and_global_capacity_without_disclosing_it():
    admission = ArtifactResolutionAdmission(
        global_in_flight_limit=2,
        per_actor_in_flight_limit=1,
    )
    first_actor = uuid4()
    second_actor = uuid4()
    third_actor = uuid4()

    first_lease = admission.try_acquire(first_actor)
    assert first_lease is not None
    assert admission.try_acquire(first_actor) is None

    second_lease = admission.try_acquire(second_actor)
    assert second_lease is not None
    assert admission.try_acquire(third_actor) is None

    first_lease.release()
    second_lease.release()


@pytest.mark.parametrize(
    "failure_type",
    [RuntimeError, asyncio.CancelledError, _ControlFlowExit],
)
def test_admission_releases_lease_for_all_exception_hierarchies(failure_type):
    admission = ArtifactResolutionAdmission(
        global_in_flight_limit=1,
        per_actor_in_flight_limit=1,
    )
    actor = AuthContext(user_id=uuid4(), role=Role.VIEWER)
    request = _request_for_actor(admission, actor)

    async def failing_call_next(_request: Request) -> JSONResponse:
        raise failure_type()

    with pytest.raises(failure_type):
        asyncio.run(_apply_artifact_resolution_admission(request, failing_call_next))

    _assert_capacity_recovered(admission, actor.user_id)


def test_admission_releases_lease_after_successful_response():
    admission = ArtifactResolutionAdmission(
        global_in_flight_limit=1,
        per_actor_in_flight_limit=1,
    )
    actor = AuthContext(user_id=uuid4(), role=Role.VIEWER)
    request = _request_for_actor(admission, actor)

    async def successful_call_next(_request: Request) -> JSONResponse:
        return JSONResponse(status_code=204, content=None)

    response = asyncio.run(
        _apply_artifact_resolution_admission(request, successful_call_next)
    )

    assert response.status_code == 204
    _assert_capacity_recovered(admission, actor.user_id)


def test_artifact_admission_matches_the_route_relative_to_root_path():
    admission = ArtifactResolutionAdmission(
        global_in_flight_limit=1,
        per_actor_in_flight_limit=1,
    )
    actor = AuthContext(user_id=uuid4(), role=Role.VIEWER)
    held = admission.try_acquire(actor.user_id)
    assert held is not None
    request = _request_for_actor(
        admission,
        actor,
        path="/prefix/external-artifacts/resolve",
        root_path="/prefix",
    )

    async def unexpected_next(_request: Request) -> JSONResponse:
        raise AssertionError("root-path artifact request bypassed admission")

    try:
        response = asyncio.run(
            _apply_artifact_resolution_admission(request, unexpected_next)
        )
    finally:
        held.release()

    assert response.status_code == 429


def _actor_id(client: TestClient, headers: dict[str, str]) -> UUID:
    token = headers["Authorization"].removeprefix("Bearer ")
    return client.app.state.token_service.verify_access_token(token).user_id


def test_saturated_resolution_is_opaque_and_bypasses_request_scope(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    viewer_user,
):
    third_user = client.app.state.auth_service.register_user(
        username=f"third-admission-{uuid4().hex}",
        password="secret",
        role=Role.VIEWER,
    )
    third_token = client.app.state.token_service.issue_access_token(third_user).token
    third_headers = {"Authorization": f"Bearer {third_token}"}

    actor_admission = ArtifactResolutionAdmission(
        global_in_flight_limit=2,
        per_actor_in_flight_limit=1,
    )
    actor_lease = actor_admission.try_acquire(_actor_id(client, admin_auth_headers))
    assert actor_lease is not None

    global_admission = ArtifactResolutionAdmission(
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
    original_registry = client.app.state.resolver_registry
    original_admission = client.app.state.artifact_resolution_admission

    def unexpected_request_scope():
        raise AssertionError("a saturated resolution must not allocate a request session")

    class UnexpectedRegistry:
        def resolve(self, *_args, **_kwargs):
            raise AssertionError("a saturated resolution must not invoke a resolver")

    client.app.state.db_session_factory = unexpected_request_scope
    client.app.state.resolver_registry = UnexpectedRegistry()
    try:
        client.app.state.artifact_resolution_admission = actor_admission
        actor_saturated = client.post(
            "/external-artifacts/resolve",
            content=b"not JSON and not a valid resolution payload",
            headers=admin_auth_headers,
        )
        client.app.state.artifact_resolution_admission = global_admission
        globally_saturated = client.post(
            "/external-artifacts/resolve",
            content=b"also deliberately malformed",
            headers=third_headers,
        )
        unauthenticated = client.post(
            "/external-artifacts/resolve",
            content=b"not JSON",
        )
    finally:
        client.app.state.db_session_factory = original_factory
        client.app.state.resolver_registry = original_registry
        client.app.state.artifact_resolution_admission = original_admission
        actor_lease.release()
        global_admin_lease.release()
        global_viewer_lease.release()

    assert actor_saturated.status_code == 429
    assert globally_saturated.status_code == 429
    assert actor_saturated.content == globally_saturated.content
    assert actor_saturated.headers["Retry-After"] == "1"
    assert globally_saturated.headers["Retry-After"] == "1"
    assert actor_saturated.headers["X-Content-Type-Options"] == "nosniff"
    assert globally_saturated.headers["X-Content-Type-Options"] == "nosniff"
    assert unauthenticated.status_code == 401


def test_saturated_root_path_artifact_never_reaches_the_request_scope(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    actor_id = _actor_id(client, admin_auth_headers)
    admission = ArtifactResolutionAdmission(
        global_in_flight_limit=1,
        per_actor_in_flight_limit=1,
    )
    held = admission.try_acquire(actor_id)
    assert held is not None
    original_admission = client.app.state.artifact_resolution_admission
    original_factory = client.app.state.db_session_factory
    original_registry = client.app.state.resolver_registry

    def unexpected_request_scope():
        raise AssertionError(
            "root-path artifact saturation must precede the request session"
        )

    class UnexpectedRegistry:
        def resolve(self, *_args, **_kwargs):
            raise AssertionError("root-path artifact saturation reached resolution")

    client.app.state.artifact_resolution_admission = admission
    client.app.state.db_session_factory = unexpected_request_scope
    client.app.state.resolver_registry = UnexpectedRegistry()
    try:
        with TestClient(
            client.app,
            root_path="/prefix",
            raise_server_exceptions=False,
        ) as prefixed_client:
            response = prefixed_client.post(
                "/prefix/external-artifacts/resolve",
                content=b"deliberately malformed",
                headers=admin_auth_headers,
            )
    finally:
        client.app.state.artifact_resolution_admission = original_admission
        client.app.state.db_session_factory = original_factory
        client.app.state.resolver_registry = original_registry
        held.release()

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "1"
