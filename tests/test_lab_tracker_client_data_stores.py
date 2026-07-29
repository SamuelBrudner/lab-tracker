from __future__ import annotations

import importlib
import json
from typing import Any

import httpx
import pytest

from lab_tracker_client import (
    STORE_CAPABILITY_VALUES,
    STORE_KIND_VALUES,
    LabTracker,
    LTAPIError,
    LTStoreAuthorityDeniedError,
    LTValidationError,
    create_data_store,
)

client_module = importlib.import_module("lab_tracker_client.client")


def _json_response(status_code: int, payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def _create_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "authority_grant_id": "project-local-primary",
        "project_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "name": "project-data",
        "kind": "local_fs",
        "root": "/srv/lab/project-data",
    }
    kwargs.update(overrides)
    return kwargs


def test_create_data_store_posts_project_scope_and_round_trips_store_identity() -> None:
    requests: list[httpx.Request] = []
    response_record = {
        "store_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "project_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "group_id": None,
        "name": "project-data",
        "kind": "local_fs",
        "root": "/srv/lab/project-data",
        "capabilities": ["bytes_by_path", "byte_range", "list"],
        "authority_grant_id": "project-local-primary",
        "authority_grant_fingerprint": f"sag-v1-sha256:{'1' * 64}",
        "is_default": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _json_response(201, {"data": response_record})

    with LabTracker(
        base_url="http://testserver",
        transport=httpx.MockTransport(handler),
    ) as lt:
        store = lt.create_data_store(**_create_kwargs())

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/data-stores"
    assert json.loads(request.content) == {
        "authority_grant_id": "project-local-primary",
        "project_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "name": "project-data",
        "kind": "local_fs",
        "root": "/srv/lab/project-data",
        "is_default": False,
    }
    assert store.id == response_record["store_id"]
    assert store.store_id == response_record["store_id"]
    assert store.authority_grant_id == "project-local-primary"
    assert store.authority_grant_fingerprint == f"sag-v1-sha256:{'1' * 64}"


def test_create_data_store_posts_group_scope_and_explicit_capabilities() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _json_response(
            201,
            {
                "data": {
                    "store_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    **requests[-1],
                }
            },
        )

    with LabTracker(
        base_url="http://testserver",
        transport=httpx.MockTransport(handler),
    ) as lt:
        store = lt.create_data_store(
            **_create_kwargs(
                project_id=None,
                group_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                authority_grant_id="group-http",
                name="published",
                kind=" HTTP ",
                root="https://artifacts.example.test/lab",
                capabilities=[" BYTES_BY_PATH ", "byte_range"],
                endpoint="https://compatibility-field.example.test",
                credential_ref="credential-reference",
                is_default=True,
            )
        )

    assert requests == [
        {
            "authority_grant_id": "group-http",
            "group_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            "name": "published",
            "kind": "http",
            "root": "https://artifacts.example.test/lab",
            "capabilities": ["bytes_by_path", "byte_range"],
            "endpoint": "https://compatibility-field.example.test",
            "credential_ref": "credential-reference",
            "is_default": True,
        }
    ]
    assert store.id == "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"project_id": None}, "exactly one"),
        (
            {"group_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc"},
            "exactly one",
        ),
        ({"project_id": ""}, "project_id"),
        ({"authority_grant_id": ""}, "authority_grant_id"),
        ({"authority_grant_id": "a" * 129}, "authority_grant_id"),
        ({"authority_grant_id": 7}, "authority_grant_id"),
        ({"kind": "filesystem"}, "store kind"),
        ({"capabilities": "bytes_by_path"}, "capabilities"),
        ({"capabilities": ["download"]}, "store capability"),
        ({"name": "  "}, "name"),
        ({"root": ""}, "root"),
    ],
)
def test_create_data_store_validates_before_network(
    overrides: dict[str, Any],
    message: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _json_response(201, {"data": {"store_id": "unexpected"}})

    with (
        LabTracker(
            base_url="http://testserver",
            transport=httpx.MockTransport(handler),
        ) as lt,
        pytest.raises(LTValidationError, match=message),
    ):
        lt.create_data_store(**_create_kwargs(**overrides))

    assert requests == []


def test_create_data_store_accepts_maximum_length_grant_id() -> None:
    bodies: list[dict[str, Any]] = []
    grant_id = "a" * 128

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _json_response(
            201,
            {"data": {"store_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"}},
        )

    with LabTracker(
        base_url="http://testserver",
        transport=httpx.MockTransport(handler),
    ) as lt:
        lt.create_data_store(**_create_kwargs(authority_grant_id=grant_id))

    assert bodies[0]["authority_grant_id"] == grant_id


def test_create_data_store_leaves_bounded_grant_matching_to_server() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _json_response(
            403,
            {
                "error": {
                    "code": "store_authority_denied",
                    "message": "Data store authority is unavailable.",
                    "issues": None,
                }
            },
        )

    with (
        LabTracker(
            base_url="http://testserver",
            access_token="token",
            transport=httpx.MockTransport(handler),
        ) as lt,
        pytest.raises(LTStoreAuthorityDeniedError),
    ):
        lt.create_data_store(
            **_create_kwargs(authority_grant_id="unknown grant?")
        )

    assert len(bodies) == 1
    assert bodies[0]["authority_grant_id"] == "unknown grant?"


def test_create_data_store_requires_authority_grant_keyword() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _json_response(201, {"data": {"store_id": "unexpected"}})

    with (
        LabTracker(
            base_url="http://testserver",
            transport=httpx.MockTransport(handler),
        ) as lt,
        pytest.raises(TypeError, match="authority_grant_id"),
    ):
        lt.create_data_store(
            project_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            name="project-data",
            kind="local_fs",
            root="/srv/lab/project-data",
        )

    assert requests == []


@pytest.mark.parametrize(
    "capabilities",
    [
        [],
        ["bytes_by_path", "bytes_by_path"],
    ],
)
def test_create_data_store_preserves_opaque_server_denial_without_retry(
    capabilities: list[str],
) -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _json_response(
            403,
            {
                "error": {
                    "code": "store_authority_denied",
                    "message": "Data store authority is unavailable.",
                    "issues": None,
                }
            },
        )

    with (
        LabTracker(
            base_url="http://testserver",
            access_token="token",
            transport=httpx.MockTransport(handler),
        ) as lt,
        pytest.raises(
            LTStoreAuthorityDeniedError,
            match="Data store authority is unavailable",
        ),
    ):
        lt.create_data_store(**_create_kwargs(capabilities=capabilities))

    assert len(bodies) == 1
    assert bodies[0]["capabilities"] == capabilities


def test_non_authority_403_remains_generic_api_error() -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return _json_response(
            403,
            {
                "error": {
                    "code": "forbidden",
                    "message": "Forbidden.",
                    "issues": None,
                }
            },
        )

    with (
        LabTracker(
            base_url="http://testserver",
            access_token="token",
            transport=httpx.MockTransport(handler),
        ) as lt,
        pytest.raises(LTAPIError, match="Forbidden") as captured,
    ):
        lt.create_data_store(**_create_kwargs())

    assert type(captured.value) is LTAPIError
    assert requests == 1


def test_create_data_store_rejects_malformed_success_response() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(201, {"data": []})

    with (
        LabTracker(
            base_url="http://testserver",
            transport=httpx.MockTransport(handler),
        ) as lt,
        pytest.raises(LTAPIError, match="object data field"),
    ):
        lt.create_data_store(**_create_kwargs())


def test_module_level_create_data_store_delegates_to_shared_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = object()
    received: list[dict[str, Any]] = []

    class _Client:
        def create_data_store(self, **kwargs: Any) -> object:
            received.append(kwargs)
            return expected

    monkeypatch.setattr(client_module, "client", _Client())

    assert create_data_store(**_create_kwargs()) is expected
    assert received == [_create_kwargs()]


def test_store_enum_values_are_exported_for_typed_callers() -> None:
    assert "local_fs" in STORE_KIND_VALUES
    assert "http" in STORE_KIND_VALUES
    assert "bytes_by_path" in STORE_CAPABILITY_VALUES
    assert "query" in STORE_CAPABILITY_VALUES
