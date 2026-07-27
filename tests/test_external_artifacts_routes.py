import base64
import hashlib
import json
from pathlib import Path

import pytest
from api_helpers import TEST_STORE_AUTHORITY_GRANT_ID
from http_security_fakes import (
    FakeAddressResolver,
    FakeHttpResponse,
    FakeSafeHttpClient,
)
from starlette.testclient import TestClient

from lab_tracker.artifact_resolution import (
    ArtifactResolver,
    GitCompleted,
    GitResolver,
    HttpResolver,
    LocalFilesystemResolver,
    RcloneCompleted,
    RcloneResolver,
    ResolverRegistry,
    registry_from_env,
)
from lab_tracker.artifact_resolution_limits import (
    MAX_ARTIFACT_BYTE_OFFSET,
    MAX_INLINE_ARTIFACT_BYTES,
)
from lab_tracker.db_models import DataStoreModel
from lab_tracker.git_remote_policy import GitRemotePolicy
from lab_tracker.outbound_http import OutboundHttpPolicy
from lab_tracker.rclone_remote_policy import RcloneRemotePolicy


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _rclone_policy(*grants: str) -> RcloneRemotePolicy:
    return RcloneRemotePolicy.from_config(",".join(grants))


def _install_local_registry(client: TestClient, allowed_root: Path) -> None:
    client.app.state.resolver_registry = ResolverRegistry(
        [LocalFilesystemResolver(allowed_roots=[allowed_root])]
    )


def _create_dataset_with_artifact(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: str,
    uri: str,
    content_hash: str,
    source_system: str = "local",
    store_name: str | None = None,
    locator: str | None = None,
) -> str:
    assert (store_name is None) == (locator is None)
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
                    {
                        "source_system": source_system,
                        "uri": uri,
                        "content_hash": content_hash,
                        **(
                            {
                                "store_name": store_name,
                                "locator": locator,
                            }
                            if store_name is not None and locator is not None
                            else {}
                        ),
                    }
                ]
            },
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["dataset_id"]


@pytest.mark.parametrize(
    "invalid_fields",
    [
        {"max_bytes": True},
        {"max_bytes": False},
        {"max_bytes": 1.0},
        {"max_bytes": "1"},
        {"max_bytes": 0},
        {"max_bytes": -1},
        {"max_bytes": MAX_INLINE_ARTIFACT_BYTES + 1},
        {"byte_start": True, "byte_end": 1},
        {"byte_start": 1.0, "byte_end": 2},
        {"byte_start": "1", "byte_end": 2},
        {"byte_start": -1, "byte_end": 2},
        {
            "byte_start": MAX_ARTIFACT_BYTE_OFFSET + 1,
            "byte_end": MAX_ARTIFACT_BYTE_OFFSET + 1,
        },
        {"byte_start": 0, "byte_end": MAX_ARTIFACT_BYTE_OFFSET + 1},
        {"byte_start": 1},
        {"byte_end": 1},
        {"byte_start": 2, "byte_end": 1},
    ],
)
def test_resolve_endpoint_rejects_invalid_content_bounds(
    client,
    admin_auth_headers,
    invalid_fields,
):
    response = client.post(
        "/external-artifacts/resolve",
        json={
            "entity_type": "dataset",
            "entity_id": "00000000-0000-0000-0000-000000000001",
            **invalid_fields,
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == 422, response.text


def test_resolve_endpoint_openapi_publishes_portable_content_bounds(client):
    schema = client.get("/openapi.json").json()["components"]["schemas"][
        "ResolveExternalArtifactRequest"
    ]

    max_bytes_schema = schema["properties"]["max_bytes"]["anyOf"][0]
    byte_start_schema = schema["properties"]["byte_start"]["anyOf"][0]
    byte_end_schema = schema["properties"]["byte_end"]["anyOf"][0]
    assert max_bytes_schema == {
        "type": "integer",
        "maximum": MAX_INLINE_ARTIFACT_BYTES,
        "minimum": 1,
    }
    assert byte_start_schema == {
        "type": "integer",
        "maximum": MAX_ARTIFACT_BYTE_OFFSET,
        "minimum": 0,
    }
    assert byte_end_schema == byte_start_schema


@pytest.mark.parametrize(
    ("source_system", "uri_factory"),
    [
        ("local", lambda artifact: artifact.as_uri()),
        ("http", lambda _artifact: "https://files.example/private.bin"),
        ("rclone", lambda _artifact: "rclone://private/private.bin"),
        (
            "git",
            lambda _artifact: (
                "git+https://git.example/private.git"
                "#private.bin@1111111111111111111111111111111111111111"
            ),
        ),
    ],
)
def test_resolve_endpoint_treats_direct_references_as_metadata_without_dispatch(
    client,
    admin_auth_headers,
    tmp_path,
    source_system,
    uri_factory,
):
    secret = b"operator-owned artifact"
    artifact = tmp_path / "private.bin"
    artifact.write_bytes(secret)
    direct_uri = uri_factory(artifact)
    project_id = client.post(
        "/projects",
        json={"name": f"Direct {source_system} metadata"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]

    class UnexpectedRegistry:
        def resolve_prepared(self, *_args, **_kwargs):
            raise AssertionError("direct reference reached resolver dispatch")

    client.app.state.resolver_registry = UnexpectedRegistry()

    for expected_hash in (_sha256(b"bogus"), _sha256(secret)):
        dataset_id = _create_dataset_with_artifact(
            client,
            admin_auth_headers,
            project_id=project_id,
            source_system=source_system,
            uri=direct_uri,
            content_hash=expected_hash,
        )

        response = client.post(
            "/external-artifacts/resolve",
            json={"entity_type": "dataset", "entity_id": dataset_id},
            headers=admin_auth_headers,
        )

        assert response.status_code == 200, response.text
        body = response.json()["data"]
        assert body["status"] == "unresolved"
        assert body["source_system"] == "store"
        assert body["uri"] == "store://[redacted]"
        assert body["expected_hash"] == expected_hash
        assert body["observed_hash"] is None
        assert body["content_base64"] is None
        assert body["returned_bytes"] == 0
        assert body["detail"] == "Store artifact could not be resolved."
        assert direct_uri not in response.text
        assert secret.decode() not in response.text


def test_resolve_endpoint_denies_direct_reference_from_legacy_manifest_metadata(
    client,
    admin_auth_headers,
):
    project_id = client.post(
        "/projects",
        json={"name": "Legacy direct-reference metadata"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Does legacy metadata bypass registered stores?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=admin_auth_headers,
    ).json()["data"]["question_id"]
    direct_uri = "https://files.example/private-legacy.bin"
    expected_hash = _sha256(b"legacy")
    dataset_response = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": question_id,
            "status": "committed",
            "commit_manifest": {
                "metadata": {
                    "external_artifacts": json.dumps(
                        [
                            {
                                "source_system": "http",
                                "uri": direct_uri,
                                "content_hash": expected_hash,
                            }
                        ]
                    )
                }
            },
        },
        headers=admin_auth_headers,
    )
    assert dataset_response.status_code == 201, dataset_response.text
    dataset_id = dataset_response.json()["data"]["dataset_id"]

    class UnexpectedRegistry:
        def resolve_prepared(self, *_args, **_kwargs):
            raise AssertionError("legacy direct reference reached resolver dispatch")

    client.app.state.resolver_registry = UnexpectedRegistry()
    response = client.post(
        "/external-artifacts/resolve",
        json={"entity_type": "dataset", "entity_id": dataset_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == "unresolved"
    assert body["source_system"] == "store"
    assert body["uri"] == "store://[redacted]"
    assert body["expected_hash"] == expected_hash
    assert body["observed_hash"] is None
    assert body["content_base64"] is None
    assert direct_uri not in response.text


def test_resolve_endpoint_redacts_denied_http_credentials_and_target(
    client,
    admin_auth_headers,
):
    secret = "hunter2"
    project_id = client.post(
        "/projects",
        json={"name": "Denied HTTP target"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=project_id,
        source_system="http",
        uri=f"http://user:{secret}@169.254.169.254/latest?token={secret}",
        content_hash=_sha256(b"x"),
    )

    response = client.post(
        "/external-artifacts/resolve",
        json={"entity_type": "dataset", "entity_id": dataset_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == "unresolved"
    assert body["source_system"] == "store"
    assert body["uri"] == "store://[redacted]"
    assert body["detail"] == "Store artifact could not be resolved."
    assert body["content_base64"] is None
    assert secret not in response.text
    assert "169.254.169.254" not in response.text


def test_resolve_endpoint_denies_direct_rclone_before_process_failure(
    client,
    admin_auth_headers,
):
    secret = "private-target-and-stderr"

    def failing_runner(_args):
        raise OSError(secret)

    client.app.state.resolver_registry = ResolverRegistry(
        [
            RcloneResolver(
                runner=failing_runner,
                remote_policy=_rclone_policy("private"),
            )
        ]
    )
    project_id = client.post(
        "/projects",
        json={"name": "Failed rclone target"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=project_id,
        source_system="rclone",
        uri=f"rclone://private/{secret}.bin",
        content_hash=_sha256(b"x"),
    )

    response = client.post(
        "/external-artifacts/resolve",
        json={"entity_type": "dataset", "entity_id": dataset_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == "unresolved"
    assert body["source_system"] == "store"
    assert body["uri"] == "store://[redacted]"
    assert body["detail"] == "Store artifact could not be resolved."
    assert body["content_base64"] is None
    assert body["observed_hash"] is None
    assert secret not in response.text


def test_resolve_endpoint_rejects_decoded_rclone_nul_without_500(
    client,
    admin_auth_headers,
):
    secret = "nul-target-secret"

    def unexpected_runner(_args):
        raise AssertionError("invalid locator must be refused before spawn")

    client.app.state.resolver_registry = ResolverRegistry(
        [
            RcloneResolver(
                runner=unexpected_runner,
                remote_policy=_rclone_policy("private"),
            )
        ]
    )
    project_id = client.post(
        "/projects",
        json={"name": "Malformed rclone target"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=project_id,
        source_system="rclone",
        uri=f"rclone://private/path%00{secret}.bin",
        content_hash=_sha256(b"x"),
    )

    response = client.post(
        "/external-artifacts/resolve",
        json={"entity_type": "dataset", "entity_id": dataset_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == "unresolved"
    assert body["source_system"] == "store"
    assert body["uri"] == "store://[redacted]"
    assert body["detail"] == "Store artifact could not be resolved."
    assert body["content_base64"] is None
    assert secret not in response.text


def test_resolve_endpoint_denies_direct_rclone_before_metadata_subprocess(
    client,
    admin_auth_headers,
):
    secret = "malformed-metadata-target"

    def malformed_runner(_args):
        return RcloneCompleted(returncode=0, stdout=b"[]", stderr=b"private stderr")

    client.app.state.resolver_registry = ResolverRegistry(
        [
            RcloneResolver(
                runner=malformed_runner,
                remote_policy=_rclone_policy("private"),
            )
        ]
    )
    project_id = client.post(
        "/projects",
        json={"name": "Malformed rclone metadata"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=project_id,
        source_system="rclone",
        uri=f"rclone://private/{secret}.bin",
        content_hash=_sha256(b"x"),
    )

    response = client.post(
        "/external-artifacts/resolve",
        json={"entity_type": "dataset", "entity_id": dataset_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == "unresolved"
    assert body["source_system"] == "store"
    assert body["uri"] == "store://[redacted]"
    assert body["detail"] == "Store artifact could not be resolved."
    assert body["content_base64"] is None
    assert secret not in response.text
    assert "private stderr" not in response.text


def test_registered_http_gate_returns_before_dns_or_stream_work(
    client,
    admin_auth_headers,
):
    response = FakeHttpResponse(
        chunks=(b"partial secret bytes",),
        on_chunk=lambda _index: (_ for _ in ()).throw(
            AssertionError("registered-store gate reached the HTTP stream")
        ),
    )
    http_client = FakeSafeHttpClient((response,))
    address_resolver = FakeAddressResolver(
        {"slow.example": ["93.184.216.34"]}
    )
    client.app.state.resolver_registry = ResolverRegistry(
        [
            HttpResolver(
                policy=OutboundHttpPolicy(
                    address_resolver=address_resolver
                ),
                client=http_client,
            )
        ]
    )
    project_id = client.post(
        "/projects",
        json={"name": "Deadline cleanup project"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    _create_store(
        client,
        admin_auth_headers,
        project_id=project_id,
        name="slow-store",
        kind="http",
        root="https://slow.example",
    )
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=project_id,
        source_system="store",
        uri="store://slow-store/private-result.bin",
        content_hash=_sha256(b"partial secret bytes"),
    )

    resolve_response = client.post(
        "/external-artifacts/resolve",
        json={"entity_type": "dataset", "entity_id": dataset_id},
        headers=admin_auth_headers,
    )

    assert resolve_response.status_code == 200, resolve_response.text
    body = resolve_response.json()["data"]
    assert body["status"] == "unresolved"
    assert body["source_system"] == "store"
    assert body["uri"] == "store://[redacted]"
    assert body["detail"] == "Store artifact could not be resolved."
    assert body["content_base64"] is None
    assert body["returned_bytes"] == 0
    assert "partial secret bytes" not in resolve_response.text
    assert "slow.example" not in resolve_response.text
    assert address_resolver.calls == []
    assert http_client.calls == []
    assert response.iterated_chunks == 0
    assert response.closed is False


def test_dataset_write_rejects_malformed_http_uri_without_server_error(
    client,
    admin_auth_headers,
):
    project_id = client.post(
        "/projects",
        json={"name": "Malformed HTTP target"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Can malformed references be stored?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=admin_auth_headers,
    ).json()["data"]["question_id"]
    response = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": question_id,
            "status": "committed",
            "commit_manifest": {
                "external_artifacts": [
                    {
                        "source_system": "http",
                        "uri": "https://[::1",
                        "content_hash": _sha256(b"x"),
                    }
                ]
            },
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == 422, response.text
    assert "well-formed IRI" in response.json()["error"]["message"]


def test_resolve_endpoint_returns_verified_content(client, admin_auth_headers, tmp_path):
    data = b"differential expression matrix"
    artifact = tmp_path / "result.txt"
    artifact.write_bytes(data)
    _install_local_registry(client, tmp_path)

    project_id = client.post(
        "/projects", json={"name": "Resolve project"}, headers=admin_auth_headers
    ).json()["data"]["project_id"]
    _create_store(
        client,
        admin_auth_headers,
        project_id=project_id,
        name="local-results",
        kind="local_fs",
        root=str(tmp_path),
    )
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=project_id,
        source_system="store",
        uri="store://local-results/result.txt",
        content_hash=_sha256(data),
    )

    response = client.post(
        "/external-artifacts/resolve",
        json={"entity_type": "dataset", "entity_id": dataset_id, "artifact_index": 0},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == "unresolved"
    assert body["source_system"] == "store"
    assert body["uri"] == "store://[redacted]"
    assert body["observed_hash"] is None
    assert body["content_base64"] is None
    assert body["detail"] == "Store artifact could not be resolved."
    assert body["entity_type"] == "dataset"


def test_resolve_endpoint_caps_a_requested_range(client, admin_auth_headers, tmp_path):
    data = b"0123456789"
    artifact = tmp_path / "ranged-result.bin"
    artifact.write_bytes(data)
    _install_local_registry(client, tmp_path)

    project_id = client.post(
        "/projects",
        json={"name": "Capped range project"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    _create_store(
        client,
        admin_auth_headers,
        project_id=project_id,
        name="local-results",
        kind="local_fs",
        root=str(tmp_path),
    )
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=project_id,
        source_system="store",
        uri="store://local-results/ranged-result.bin",
        content_hash=_sha256(data),
    )

    response = client.post(
        "/external-artifacts/resolve",
        json={
            "entity_type": "dataset",
            "entity_id": dataset_id,
            "max_bytes": 3,
            "byte_start": 2,
            "byte_end": 9,
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == "unresolved"
    assert body["uri"] == "store://[redacted]"
    assert body["observed_hash"] is None
    assert body["size_bytes"] is None
    assert body["returned_bytes"] == 0
    assert body["truncated"] is False
    assert body["content_base64"] is None
    assert body["detail"] == "Store artifact could not be resolved."


def test_resolve_endpoint_uses_registered_http_store_prefix_and_logical_identity(
    client,
    admin_auth_headers,
):
    data = b"registered HTTP bytes"
    address_resolver = FakeAddressResolver(
        {"store.example": ["93.184.216.34"]}
    )
    http_client = FakeSafeHttpClient(
        (FakeHttpResponse(chunks=(data,)),)
    )
    client.app.state.resolver_registry = ResolverRegistry(
        [
            HttpResolver(
                policy=OutboundHttpPolicy(address_resolver=address_resolver),
                client=http_client,
            )
        ]
    )
    project_id = client.post(
        "/projects",
        json={"name": "Registered HTTP resolve project"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    store_response = client.post(
        "/data-stores",
        json={
            "project_id": project_id,
            "name": "web",
            "kind": "http",
            "root": "https://store.example/base",
            "authority_grant_id": TEST_STORE_AUTHORITY_GRANT_ID,
        },
        headers=admin_auth_headers,
    )
    assert store_response.status_code == 201, store_response.text
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=project_id,
        source_system="legacy-http",
        uri="store://web/nested/artifact.bin",
        store_name="web",
        locator="nested/artifact.bin",
        content_hash=_sha256(data),
    )

    response = client.post(
        "/external-artifacts/resolve",
        json={
            "entity_type": "dataset",
            "entity_id": dataset_id,
            "artifact_index": 0,
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == "unresolved"
    assert body["source_system"] == "store"
    assert body["uri"] == "store://[redacted]"
    assert body["content_base64"] is None
    assert body["detail"] == "Store artifact could not be resolved."
    assert address_resolver.calls == []
    assert http_client.calls == []


def test_resolve_endpoint_uses_registered_rclone_root_and_logical_identity(
    client,
    admin_auth_headers,
):
    data = b"registered rclone bytes"
    calls: list[list[str]] = []

    def runner(args: list[str]) -> RcloneCompleted:
        calls.append(args)
        if args[0] == "size":
            return RcloneCompleted(
                returncode=0,
                stdout=f'{{"count":1,"bytes":{len(data)}}}'.encode(),
                stderr=b"",
            )
        if args[0] == "cat":
            return RcloneCompleted(returncode=0, stdout=data, stderr=b"")
        raise AssertionError(f"unexpected rclone args: {args}")

    client.app.state.resolver_registry = ResolverRegistry(
        [
            RcloneResolver(
                runner=runner,
                remote_policy=_rclone_policy("lab-onedrive"),
            )
        ]
    )
    project_id = client.post(
        "/projects",
        json={"name": "Registered rclone resolve project"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    store_response = client.post(
        "/data-stores",
        json={
            "project_id": project_id,
            "name": "cloud",
            "kind": "onedrive",
            "root": "/experiments",
            "credential_ref": "lab-onedrive",
            "authority_grant_id": TEST_STORE_AUTHORITY_GRANT_ID,
        },
        headers=admin_auth_headers,
    )
    assert store_response.status_code == 201, store_response.text
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=project_id,
        source_system="legacy-rclone",
        uri="store://cloud/nested/artifact.bin",
        store_name="cloud",
        locator="nested/artifact.bin",
        content_hash=_sha256(data),
    )

    response = client.post(
        "/external-artifacts/resolve",
        json={
            "entity_type": "dataset",
            "entity_id": dataset_id,
            "artifact_index": 0,
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == "unresolved"
    assert body["source_system"] == "store"
    assert body["uri"] == "store://[redacted]"
    assert body["content_base64"] is None
    assert body["detail"] == "Store artifact could not be resolved."
    assert calls == []
    assert "lab-onedrive" not in response.text
    assert "/experiments" not in response.text


@pytest.mark.parametrize(
    (
        "case_name",
        "uri",
        "legacy_root",
        "legacy_credential_ref",
        "forbidden_values",
    ),
    (
        (
            "encoded traversal",
            "store://cloud/nested/%2e%2e/encoded-traversal-secret.bin",
            None,
            None,
            (
                "encoded-traversal-secret",
                "configured-root-secret",
                "configured-remote-secret",
            ),
        ),
        (
            "invalid registered root",
            "store://cloud/nested/artifact.bin",
            "../invalid-root-secret",
            None,
            (
                "configured-root-secret",
                "invalid-root-secret",
                "configured-remote-secret",
            ),
        ),
        (
            "invalid registered remote",
            "store://cloud/nested/artifact.bin",
            None,
            ":s3,env_auth=invalid-remote-secret",
            ("configured-root-secret", "invalid-remote-secret", "env_auth"),
        ),
    ),
)
def test_resolve_endpoint_rejects_invalid_registered_rclone_targets_before_process_work(
    client,
    admin_auth_headers,
    case_name: str,
    uri: str,
    legacy_root: str | None,
    legacy_credential_ref: str | None,
    forbidden_values: tuple[str, ...],
):
    class ZeroCallProcessSpy:
        def __init__(self):
            self.calls: list[tuple[list[str], object]] = []

        def run(self, command, *, cwd=None, **_kwargs):
            self.calls.append((list(command), cwd))
            raise AssertionError("invalid registered target reached process execution")

    process_spy = ZeroCallProcessSpy()
    client.app.state.resolver_registry = ResolverRegistry(
        [
            RcloneResolver(
                executor=process_spy,
                remote_policy=_rclone_policy("configured-remote-secret"),
            )
        ]
    )
    project_id = client.post(
        "/projects",
        json={"name": f"Rejected rclone target: {case_name}"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    store_response = client.post(
        "/data-stores",
        json={
            "project_id": project_id,
            "name": "cloud",
            "kind": "rclone",
            "root": "/configured-root-secret",
            "credential_ref": "configured-remote-secret",
            "authority_grant_id": TEST_STORE_AUTHORITY_GRANT_ID,
        },
        headers=admin_auth_headers,
    )
    assert store_response.status_code == 201, store_response.text
    store_id = store_response.json()["data"]["store_id"]
    if legacy_root is not None or legacy_credential_ref is not None:
        with client.app.state.db_session_factory() as session:
            store = session.get(DataStoreModel, store_id)
            assert store is not None
            if legacy_root is not None:
                store.root = legacy_root
            if legacy_credential_ref is not None:
                store.credential_ref = legacy_credential_ref
            session.commit()
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=project_id,
        source_system="legacy-rclone",
        uri=uri,
        content_hash=_sha256(b"unreachable"),
    )

    response = client.post(
        "/external-artifacts/resolve",
        json={
            "entity_type": "dataset",
            "entity_id": dataset_id,
            "artifact_index": 0,
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == "unresolved"
    assert body["source_system"] == "store"
    assert body["uri"] == "store://[redacted]"
    assert body["content_base64"] is None
    assert process_spy.calls == []  # No subprocess invocation or cwd selection.
    assert all(value not in response.text for value in forbidden_values)


def test_resolve_endpoint_uses_registered_git_pin_and_logical_identity(
    client,
    admin_auth_headers,
    tmp_path,
):
    data = b"registered git bytes"
    object_id = "a" * 40
    remote = "https://git.example/lab/repo.git"
    calls: list[list[str]] = []

    def runner(args: list[str]) -> GitCompleted:
        calls.append(args)
        command = next(
            candidate
            for candidate in ("init", "ls-remote", "fetch", "cat-file")
            if candidate in args
        )
        command_index = args.index(command)
        tail = args[command_index:]
        if command == "init":
            assert tail == ["init", "-q", "--object-format=sha1"]
            return GitCompleted(0, b"", b"")
        if command == "ls-remote":
            assert tail == ["ls-remote", "--get-url", "--", remote]
            return GitCompleted(0, f"{remote}\n".encode(), b"")
        if command == "fetch":
            assert tail == [
                "fetch",
                "--quiet",
                "--no-tags",
                "--depth",
                "1",
                "--",
                remote,
                object_id,
            ]
            return GitCompleted(0, b"", b"")
        if tail == ["cat-file", "-s", f"{object_id}:src/model.py"]:
            return GitCompleted(0, str(len(data)).encode(), b"")
        assert tail == ["cat-file", "blob", f"{object_id}:src/model.py"]
        return GitCompleted(0, data, b"")

    cache_root = tmp_path / "git-cache"
    client.app.state.resolver_registry = ResolverRegistry(
        [
            GitResolver(
                runner=runner,
                cache_root=cache_root,
                remote_policy=GitRemotePolicy.from_config(remote),
            )
        ]
    )
    project_id = client.post(
        "/projects",
        json={"name": "Registered git resolve project"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    store_response = client.post(
        "/data-stores",
        json={
            "project_id": project_id,
            "name": "analysis-repo",
            "kind": "git",
            "root": remote,
            "authority_grant_id": TEST_STORE_AUTHORITY_GRANT_ID,
        },
        headers=admin_auth_headers,
    )
    assert store_response.status_code == 201, store_response.text
    logical_uri = f"store://analysis-repo/src/model.py@{object_id}"
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=project_id,
        source_system="legacy-git",
        uri=logical_uri,
        store_name="analysis-repo",
        locator=f"src/model.py@{object_id}",
        content_hash=_sha256(data),
    )

    response = client.post(
        "/external-artifacts/resolve",
        json={
            "entity_type": "dataset",
            "entity_id": dataset_id,
            "artifact_index": 0,
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == "unresolved"
    assert body["source_system"] == "store"
    assert body["uri"] == "store://[redacted]"
    assert body["content_base64"] is None
    assert body["detail"] == "Store artifact could not be resolved."
    assert calls == []
    assert remote not in response.text


@pytest.mark.parametrize(
    ("case_name", "uri", "legacy_root", "forbidden_values"),
    (
        (
            "mutable revision",
            "store://analysis-repo/src/model.py@HEAD",
            None,
            ("HEAD", "configured-repo-secret"),
        ),
        (
            "encoded traversal",
            f"store://analysis-repo/src/%2e%2e/encoded-secret.py@{'a' * 40}",
            None,
            ("encoded-secret", "configured-repo-secret"),
        ),
        (
            "invalid legacy remote",
            f"store://analysis-repo/src/model.py@{'a' * 40}",
            "../invalid-legacy-remote-secret",
            ("invalid-legacy-remote-secret", "configured-repo-secret"),
        ),
    ),
)
def test_resolve_endpoint_rejects_invalid_registered_git_targets_before_cache_or_process(
    client,
    admin_auth_headers,
    tmp_path,
    case_name: str,
    uri: str,
    legacy_root: str | None,
    forbidden_values: tuple[str, ...],
):
    class ZeroCallProcessSpy:
        def __init__(self):
            self.calls: list[tuple[list[str], object]] = []

        def run(self, command, *, cwd=None, **_kwargs):
            self.calls.append((list(command), cwd))
            raise AssertionError("invalid registered Git target reached a process")

    valid_remote = "https://git.example/configured-repo-secret.git"
    cache_root = tmp_path / f"git-cache-{case_name}"
    process_spy = ZeroCallProcessSpy()
    client.app.state.resolver_registry = ResolverRegistry(
        [
            GitResolver(
                executor=process_spy,
                cache_root=cache_root,
                remote_policy=GitRemotePolicy.from_config(valid_remote),
            )
        ]
    )
    project_id = client.post(
        "/projects",
        json={"name": f"Rejected Git target: {case_name}"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    store_response = client.post(
        "/data-stores",
        json={
            "project_id": project_id,
            "name": "analysis-repo",
            "kind": "git",
            "root": valid_remote,
            "authority_grant_id": TEST_STORE_AUTHORITY_GRANT_ID,
        },
        headers=admin_auth_headers,
    )
    assert store_response.status_code == 201, store_response.text
    if legacy_root is not None:
        store_id = store_response.json()["data"]["store_id"]
        with client.app.state.db_session_factory() as session:
            store = session.get(DataStoreModel, store_id)
            assert store is not None
            store.root = legacy_root
            session.commit()
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=project_id,
        source_system="legacy-git",
        uri=uri,
        content_hash=_sha256(b"unreachable"),
    )

    response = client.post(
        "/external-artifacts/resolve",
        json={
            "entity_type": "dataset",
            "entity_id": dataset_id,
            "artifact_index": 0,
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == "unresolved"
    assert body["source_system"] == "store"
    assert body["uri"] == "store://[redacted]"
    assert body["content_base64"] is None
    assert process_spy.calls == []
    assert not cache_root.exists()
    assert all(value not in response.text for value in forbidden_values)


def test_registered_store_gate_returns_before_drift_check(
    client,
    admin_auth_headers,
    tmp_path,
):
    recorded_hash = _sha256(b"what was recorded at capture")
    artifact = tmp_path / "result.txt"
    artifact.write_bytes(b"actual bytes on disk")
    _install_local_registry(client, tmp_path)

    project_id = client.post(
        "/projects", json={"name": "Drift project"}, headers=admin_auth_headers
    ).json()["data"]["project_id"]
    _create_store(
        client,
        admin_auth_headers,
        project_id=project_id,
        name="local-results",
        kind="local_fs",
        root=str(tmp_path),
    )
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=project_id,
        source_system="store",
        uri="store://local-results/result.txt",
        content_hash=recorded_hash,
    )

    response = client.post(
        "/external-artifacts/resolve",
        json={"entity_type": "dataset", "entity_id": dataset_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["status"] == "unresolved"
    assert body["uri"] == "store://[redacted]"
    assert body["expected_hash"] == recorded_hash
    assert body["observed_hash"] is None
    assert body["content_type"] is None
    assert body["size_bytes"] is None
    assert body["content_base64"] is None
    assert body["returned_bytes"] == 0
    assert body["truncated"] is False
    assert body["detail"] == "Store artifact could not be resolved."
    assert base64.b64encode(b"actual bytes on disk").decode("ascii") not in response.text


def test_resolve_endpoint_missing_index_is_404(client, admin_auth_headers, tmp_path):
    artifact = tmp_path / "result.txt"
    artifact.write_bytes(b"x")
    _install_local_registry(client, tmp_path)

    project_id = client.post(
        "/projects", json={"name": "Index project"}, headers=admin_auth_headers
    ).json()["data"]["project_id"]
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=project_id,
        uri=artifact.as_uri(),
        content_hash=_sha256(b"x"),
    )

    response = client.post(
        "/external-artifacts/resolve",
        json={"entity_type": "dataset", "entity_id": dataset_id, "artifact_index": 5},
        headers=admin_auth_headers,
    )

    assert response.status_code == 404


def test_resolve_endpoint_rejects_unauthorized_caller(
    client, scoped_project_member, admin_auth_headers, tmp_path
):
    data = b"hidden project data"
    artifact = tmp_path / "secret.txt"
    artifact.write_bytes(data)
    _install_local_registry(client, tmp_path)

    # Admin creates a dataset in a project the member cannot read.
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=scoped_project_member.hidden_project_id,
        uri=artifact.as_uri(),
        content_hash=_sha256(data),
    )

    response = client.post(
        "/external-artifacts/resolve",
        json={"entity_type": "dataset", "entity_id": dataset_id},
        headers=scoped_project_member.member_headers,
    )

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "not_found",
            "message": "Dataset does not exist.",
            "issues": None,
        }
    }
    assert "content_base64" not in response.text


def _create_store(client, headers, *, project_id, name, kind, root) -> None:
    response = client.post(
        "/data-stores",
        json={
            "project_id": project_id,
            "name": name,
            "kind": kind,
            "root": root,
            "authority_grant_id": TEST_STORE_AUTHORITY_GRANT_ID,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text


def test_resolve_endpoint_resolves_store_locator(client, admin_auth_headers, tmp_path):
    data = b"object addressed relative to a store"
    (tmp_path / "exp").mkdir()
    (tmp_path / "exp" / "x.txt").write_bytes(data)
    _install_local_registry(client, tmp_path)

    project_id = client.post(
        "/projects", json={"name": "Store locator project"}, headers=admin_auth_headers
    ).json()["data"]["project_id"]
    _create_store(
        client,
        admin_auth_headers,
        project_id=project_id,
        name="lab-fs",
        kind="local_fs",
        root=str(tmp_path),
    )
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
    assert body["status"] == "unresolved"
    assert body["source_system"] == "store"
    assert body["uri"] == "store://[redacted]"
    assert body["content_base64"] is None
    assert body["detail"] == "Store artifact could not be resolved."


def test_resolve_endpoint_rejects_mismatched_structured_store_identity(
    client,
    admin_auth_headers,
    tmp_path,
):
    data = b"must not resolve through mismatched structured store fields"
    (tmp_path / "exp").mkdir()
    (tmp_path / "exp" / "x.txt").write_bytes(data)
    _install_local_registry(client, tmp_path)

    project_id = client.post(
        "/projects", json={"name": "Field form project"}, headers=admin_auth_headers
    ).json()["data"]["project_id"]
    _create_store(
        client,
        admin_auth_headers,
        project_id=project_id,
        name="lab-fs",
        kind="local_fs",
        root=str(tmp_path),
    )
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Field form?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=admin_auth_headers,
    ).json()["data"]["question_id"]
    dataset_id = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": question_id,
            "status": "committed",
            "commit_manifest": {
                "external_artifacts": [
                    {
                        "source_system": "store",
                        "uri": "store://wrong-store/exp/x.txt",
                        "content_hash": _sha256(data),
                        "store_name": "lab-fs",
                        "locator": "exp/x.txt",
                    }
                ]
            },
        },
        headers=admin_auth_headers,
    ).json()["data"]["dataset_id"]

    response = client.post(
        "/external-artifacts/resolve",
        json={"entity_type": "dataset", "entity_id": dataset_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == "unresolved"
    assert body["uri"] == "store://[redacted]"
    assert body["detail"] == "Store artifact could not be resolved."
    assert body["content_base64"] is None
    assert "wrong-store" not in response.text
    assert str(tmp_path) not in response.text


def test_resolve_endpoint_uses_matching_structured_store_identity(
    client,
    admin_auth_headers,
    tmp_path,
):
    data = b"resolved through canonical structured store fields"
    (tmp_path / "exp").mkdir()
    (tmp_path / "exp" / "x.txt").write_bytes(data)
    _install_local_registry(client, tmp_path)

    project_id = client.post(
        "/projects", json={"name": "Matching field form project"}, headers=admin_auth_headers
    ).json()["data"]["project_id"]
    _create_store(
        client,
        admin_auth_headers,
        project_id=project_id,
        name="lab-fs",
        kind="local_fs",
        root=str(tmp_path),
    )
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Canonical field form?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=admin_auth_headers,
    ).json()["data"]["question_id"]
    dataset_id = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": question_id,
            "status": "committed",
            "commit_manifest": {
                "external_artifacts": [
                    {
                        "source_system": "store",
                        "uri": "store://lab-fs/exp/x.txt",
                        "content_hash": _sha256(data),
                        "store_name": "lab-fs",
                        "locator": "exp/x.txt",
                    }
                ]
            },
        },
        headers=admin_auth_headers,
    ).json()["data"]["dataset_id"]

    response = client.post(
        "/external-artifacts/resolve",
        json={"entity_type": "dataset", "entity_id": dataset_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == "unresolved"
    assert body["source_system"] == "store"
    assert body["uri"] == "store://[redacted]"
    assert body["content_base64"] is None
    assert body["detail"] == "Store artifact could not be resolved."


def test_local_store_locator_cannot_escape_registered_root_with_uri_or_fields(
    client,
    admin_auth_headers,
    tmp_path,
):
    global_root = tmp_path / "global"
    store_root = global_root / "registered"
    sibling_root = global_root / "sibling"
    store_root.mkdir(parents=True)
    sibling_root.mkdir()
    secret = sibling_root / "secret.bin"
    secret_bytes = b"sibling bytes outside the registered store"
    secret.write_bytes(secret_bytes)
    _install_local_registry(client, global_root)

    project_id = client.post(
        "/projects",
        json={"name": "Registered store confinement project"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    _create_store(
        client,
        admin_auth_headers,
        project_id=project_id,
        name="lab-fs",
        kind="local_fs",
        root=str(store_root),
    )
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Can a store locator leave its registered root?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=admin_auth_headers,
    ).json()["data"]["question_id"]

    references = (
        {
            "source_system": "store",
            "uri": "store://lab-fs/../sibling/secret.bin",
        },
        {
            "source_system": "store",
            "uri": "store://lab-fs/../sibling/secret.bin",
            "store_name": "lab-fs",
            "locator": "../sibling/secret.bin",
        },
    )
    for reference_fields in references:
        # The bogus-hash attempt would disclose an observed hash as DRIFTED in
        # the vulnerable implementation. Retrying that observed hash would then
        # return VERIFIED bytes. Both attempts must fail before filesystem I/O.
        for content_hash in (_sha256(b"bogus"), _sha256(secret_bytes)):
            dataset_response = client.post(
                "/datasets",
                json={
                    "project_id": project_id,
                    "primary_question_id": question_id,
                    "status": "committed",
                    "commit_manifest": {
                        "external_artifacts": [
                            {
                                **reference_fields,
                                "content_hash": content_hash,
                            }
                        ]
                    },
                },
                headers=admin_auth_headers,
            )
            assert dataset_response.status_code == 201, dataset_response.text
            dataset_id = dataset_response.json()["data"]["dataset_id"]

            response = client.post(
                "/external-artifacts/resolve",
                json={"entity_type": "dataset", "entity_id": dataset_id},
                headers=admin_auth_headers,
            )

            assert response.status_code == 200, response.text
            body = response.json()["data"]
            assert body["status"] == "unresolved"
            assert body["uri"] == "store://[redacted]"
            assert body["observed_hash"] is None
            assert body["content_base64"] is None
            assert body["returned_bytes"] == 0
            assert "secret.bin" not in response.text
            assert str(global_root) not in response.text


def test_malformed_store_uri_matrix_never_reaches_resolver(
    client,
    admin_auth_headers,
    tmp_path,
):
    project_id = client.post(
        "/projects",
        json={"name": "Malformed store URI project"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    _create_store(
        client,
        admin_auth_headers,
        project_id=project_id,
        name="lab-fs",
        kind="local_fs",
        root=str(tmp_path),
    )
    # An invalid reference must materialize to a static result. It still crosses
    # the registry boundary, but must return without touching any resolver.
    class UnexpectedResolver(ArtifactResolver):
        def can_resolve(self, ref):
            raise AssertionError("precomputed failure reached resolver selection")

        def resolve(self, ref, *, max_bytes, byte_range):
            raise AssertionError("precomputed failure reached resolver I/O")

    client.app.state.resolver_registry = ResolverRegistry([UnexpectedResolver()])
    malformed_cases = (
        "store://lab-fs//absolute-alias.txt",
        "store://lab-fs/../sibling.txt",
        "store://lab-fs/path.txt?download=1",
        "store://lab-fs/path.txt#fragment",
        "store://user@lab-fs/path.txt",
        "store://lab-fs/%2e%2e/sibling.txt",
        "store://lab-fs/%70ath.txt",
    )

    for index, uri in enumerate(malformed_cases):
        dataset_id = _create_dataset_with_artifact(
            client,
            admin_auth_headers,
            project_id=project_id,
            uri=uri,
            content_hash=_sha256(f"invalid-{index}".encode()),
            source_system="store",
        )

        response = client.post(
            "/external-artifacts/resolve",
            json={"entity_type": "dataset", "entity_id": dataset_id},
            headers=admin_auth_headers,
        )

        assert response.status_code == 200, response.text
        body = response.json()["data"]
        assert body["status"] == "unresolved"
        assert body["uri"] == "store://[redacted]"
        assert body["detail"] == "Store artifact could not be resolved."
        assert body["content_base64"] is None


def test_resolve_endpoint_unknown_store_is_unresolved(client, admin_auth_headers, tmp_path):
    _install_local_registry(client, tmp_path)
    project_id = client.post(
        "/projects", json={"name": "Missing store project"}, headers=admin_auth_headers
    ).json()["data"]["project_id"]
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=project_id,
        uri="store://nonexistent/x.txt",
        content_hash=_sha256(b"x"),
    )

    response = client.post(
        "/external-artifacts/resolve",
        json={"entity_type": "dataset", "entity_id": dataset_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["status"] == "unresolved"
    assert body["uri"] == "store://[redacted]"
    assert body["detail"] == "Store artifact could not be resolved."
    assert "nonexistent" not in response.text
    assert body["content_base64"] is None
    assert body["returned_bytes"] == 0


def test_resolve_endpoint_denies_registered_local_store_without_configured_roots(
    client,
    admin_auth_headers,
    tmp_path,
    monkeypatch,
):
    data = b"unconfigured"
    artifact = tmp_path / "result.txt"
    artifact.write_bytes(data)
    monkeypatch.delenv("LAB_TRACKER_RESOLVER_ALLOWED_ROOTS", raising=False)
    client.app.state.resolver_registry = registry_from_env()

    project_id = client.post(
        "/projects", json={"name": "Default-deny project"}, headers=admin_auth_headers
    ).json()["data"]["project_id"]
    _create_store(
        client,
        admin_auth_headers,
        project_id=project_id,
        name="unconfigured-local",
        kind="local_fs",
        root=str(tmp_path),
    )
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=project_id,
        uri="store://unconfigured-local/result.txt",
        content_hash=_sha256(data),
        source_system="store",
        store_name="unconfigured-local",
        locator="result.txt",
    )

    response = client.post(
        "/external-artifacts/resolve",
        json={"entity_type": "dataset", "entity_id": dataset_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["status"] == "unresolved"
    assert body["uri"] == "store://[redacted]"
    assert body["detail"] == "Store artifact could not be resolved."
    assert body["content_base64"] is None
    assert body["returned_bytes"] == 0
