from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from lab_tracker import mcp_server
from lab_tracker.mcp_evidence_bundle import record_evidence_bundle
from lab_tracker.schemas import EvidenceBundleRequest

PROJECT_ID = "00000000-0000-4000-8000-000000000001"
QUESTION_ID = "00000000-0000-4000-8000-000000000002"
DATASET_ID = "00000000-0000-4000-8000-000000000003"
ANALYSIS_ID = "00000000-0000-4000-8000-000000000004"
CLAIM_ID = "00000000-0000-4000-8000-000000000005"
VISUALIZATION_ID = "00000000-0000-4000-8000-000000000006"
NOTE_ID = "00000000-0000-4000-8000-000000000007"
BUNDLE_KEY = "bundle-1"


def _json_response(status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def _component_ids() -> dict[str, str]:
    return {
        "dataset_id": DATASET_ID,
        "analysis_id": ANALYSIS_ID,
        "claim_id": CLAIM_ID,
        "visualization_id": VISUALIZATION_ID,
        "source_note_id": NOTE_ID,
    }


def _empty_legacy_buckets() -> dict[str, list[str]]:
    return {
        "notes": [],
        "datasets": [],
        "analyses": [],
        "claims": [],
        "visualizations": [],
    }


def _all_legacy_buckets() -> dict[str, list[str]]:
    return {
        "notes": [NOTE_ID],
        "datasets": [DATASET_ID],
        "analyses": [ANALYSIS_ID],
        "claims": [CLAIM_ID],
        "visualizations": [VISUALIZATION_ID],
    }


def _bundle_response(
    outcome: str,
    *,
    component_ids: dict[str, str | None] | None = None,
    plan: list[dict] | None = None,
) -> dict:
    resolved_ids = _component_ids() if component_ids is None else component_ids
    action = "reuse" if outcome == "reused" else "create"
    if plan is None:
        plan = [
            {
                "action": action,
                "entity_type": entity_type,
                "entity_id": (None if outcome == "preview" else resolved_ids[result_field]),
            }
            for entity_type, result_field in (
                ("dataset", "dataset_id"),
                ("analysis", "analysis_id"),
                ("claim", "claim_id"),
                ("visualization", "visualization_id"),
                ("source_note", "source_note_id"),
            )
        ]
    return {
        "data": {
            "outcome": outcome,
            "dry_run": outcome == "preview",
            "project_id": PROJECT_ID,
            "idempotency_key": BUNDLE_KEY,
            "component_ids": resolved_ids,
            "plan": plan,
            "warnings": [],
        }
    }


def _bundle_payload(upload_path: str | None = None) -> dict:
    visualization: dict = {
        "viz_type": "line",
        "file_path": "figures/baseline.png",
        "caption": "Baseline comparison",
    }
    if upload_path is not None:
        visualization.update(
            {
                "upload_file": True,
                "upload_file_path": upload_path,
                "content_type": "image/png",
            }
        )
    return {
        "project_id": PROJECT_ID,
        "primary_question_id": QUESTION_ID,
        "idempotency_key": BUNDLE_KEY,
        "dataset": {
            "commit_hash": "dataset-hash",
            "commit_manifest": {"files": [{"path": "data.csv", "checksum": "abc"}]},
            "status": "committed",
        },
        "analysis": {
            "method_hash": "method-hash",
            "code_version": "git:abc123",
            "environment_hash": "env-hash",
            "status": "committed",
        },
        "claim": {
            "statement": "Baseline controls change behavior.",
            "confidence": 80.0,
            "status": "supported",
        },
        "visualization": visualization,
        "source_note": {
            "raw_content": "Notebook source for baseline result.",
            "status": "committed",
        },
    }


def test_record_evidence_bundle_dry_run_posts_one_strict_command() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/evidence-bundles"
        return _json_response(200, _bundle_response("preview"))

    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(handler),
    )

    try:
        payload = record_evidence_bundle(client, dry_run=True, **_bundle_payload())
    finally:
        client.close()

    assert payload["data"]["outcome"] == "preview"
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body == {
        "project_id": PROJECT_ID,
        "primary_question_id": QUESTION_ID,
        "dataset": {
            "kind": "create",
            "commit_hash": "dataset-hash",
            "commit_manifest": {"files": [{"path": "data.csv", "checksum": "abc"}]},
            "status": "committed",
        },
        "analysis": {
            "kind": "create",
            "method_hash": "method-hash",
            "code_version": "git:abc123",
            "environment_hash": "env-hash",
            "status": "committed",
        },
        "claim": {
            "kind": "create",
            "statement": "Baseline controls change behavior.",
            "confidence": 80.0,
            "status": "supported",
        },
        "visualization": {
            "kind": "create",
            "viz_type": "line",
            "file_path": "figures/baseline.png",
            "caption": "Baseline comparison",
        },
        "source_note": {
            "kind": "create",
            "raw_content": "Notebook source for baseline result.",
            "status": "committed",
        },
        "dry_run": True,
        "idempotency_key": BUNDLE_KEY,
    }
    assert payload["data"]["created"] == _empty_legacy_buckets()
    assert payload["data"]["reused"] == _empty_legacy_buckets()


@pytest.mark.parametrize("visualization_id_field", ["viz_id", "visualization_id"])
def test_record_evidence_bundle_translates_existing_component_ids(
    visualization_id_field: str,
) -> None:
    seen_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_body.update(json.loads(request.content))
        return _json_response(200, _bundle_response("preview"))

    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(handler),
    )
    try:
        record_evidence_bundle(
            client,
            project_id=PROJECT_ID,
            dataset={"dataset_id": DATASET_ID, "status": "committed"},
            analysis={"analysis_id": ANALYSIS_ID, "method_hash": "ignored"},
            claim={"claim_id": CLAIM_ID, "statement": "ignored"},
            visualization={
                visualization_id_field: VISUALIZATION_ID,
                "caption": "ignored",
            },
            source_note={"note_id": NOTE_ID, "raw_content": "ignored"},
        )
    finally:
        client.close()

    assert seen_body["dataset"] == {"kind": "existing", "dataset_id": DATASET_ID}
    assert seen_body["analysis"] == {"kind": "existing", "analysis_id": ANALYSIS_ID}
    assert seen_body["claim"] == {"kind": "existing", "claim_id": CLAIM_ID}
    assert seen_body["visualization"] == {
        "kind": "existing",
        "viz_id": VISUALIZATION_ID,
    }
    assert seen_body["source_note"] == {"kind": "existing", "note_id": NOTE_ID}
    EvidenceBundleRequest.model_validate(seen_body)


def test_record_evidence_bundle_upload_is_client_side_and_retry_safe(
    tmp_path: Path,
) -> None:
    upload_path = tmp_path / "baseline.png"
    file_bytes = b"fake-image"
    upload_path.write_bytes(file_bytes)
    checksum = hashlib.sha256(file_bytes).hexdigest()
    requests: list[httpx.Request] = []
    bundle_calls = 0
    upload_calls = 0
    asset: dict | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal asset, bundle_calls, upload_calls
        requests.append(request)
        if request.url.path == "/evidence-bundles":
            bundle_calls += 1
            body = json.loads(request.content)
            visualization = body["visualization"]
            assert visualization["file_path"] == "figures/baseline.png"
            assert "upload_file" not in visualization
            assert "upload_file_path" not in visualization
            assert visualization["upload_intent"] == {
                "checksum_sha256": checksum,
                "size_bytes": len(file_bytes),
                "filename": "baseline.png",
                "content_type": "image/png",
            }
            outcome = "created" if bundle_calls == 1 else "reused"
            return _json_response(201 if outcome == "created" else 200, _bundle_response(outcome))
        if request.url.path == f"/visualizations/{VISUALIZATION_ID}" and request.method == "GET":
            return _json_response(
                200,
                {"data": {"viz_id": VISUALIZATION_ID, "asset": asset}},
            )
        if request.url.path == f"/visualizations/{VISUALIZATION_ID}/file":
            upload_calls += 1
            assert request.method == "POST"
            assert request.headers["content-type"].startswith("multipart/form-data")
            assert checksum.encode() in request.content
            assert str(len(file_bytes)).encode() in request.content
            assert b"expected_current_storage_id" in request.content
            assert (b"absent" if upload_calls == 1 else b"storage-1") in request.content
            asset = {
                "storage_id": "storage-1",
                "filename": "baseline.png",
                "content_type": "image/png",
                "size_bytes": len(file_bytes),
                "checksum": checksum,
            }
            return _json_response(
                201 if upload_calls == 1 else 200,
                {
                    "data": {"viz_id": VISUALIZATION_ID, "asset": asset},
                    "meta": {
                        "asset_outcome": "created" if upload_calls == 1 else "reused"
                    },
                },
            )
        return _json_response(404, {"error": {"message": "not found"}})

    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(handler),
    )
    try:
        first = record_evidence_bundle(
            client,
            dry_run=False,
            **_bundle_payload(upload_path=str(upload_path)),
        )
        second = record_evidence_bundle(
            client,
            dry_run=False,
            **_bundle_payload(upload_path=str(upload_path)),
        )
    finally:
        client.close()

    assert first["data"]["outcome"] == "created"
    assert first["data"]["attachment"]["action"] == "uploaded"
    assert first["data"]["created"] == _all_legacy_buckets()
    assert first["data"]["reused"] == _empty_legacy_buckets()
    assert second["data"]["outcome"] == "reused"
    assert second["data"]["created"] == _empty_legacy_buckets()
    assert second["data"]["reused"] == _all_legacy_buckets()
    assert second["data"]["attachment"] == {
        "action": "reuse",
        "entity_type": "visualization_asset",
        "outcome": "reused",
        "visualization_id": VISUALIZATION_ID,
        "storage_id": "storage-1",
        "checksum_sha256": checksum,
        "reason": "matching_checksum",
    }
    assert [request.url.path for request in requests] == [
        "/evidence-bundles",
        f"/visualizations/{VISUALIZATION_ID}",
        f"/visualizations/{VISUALIZATION_ID}/file",
        "/evidence-bundles",
        f"/visualizations/{VISUALIZATION_ID}",
        f"/visualizations/{VISUALIZATION_ID}/file",
    ]


def test_keyed_dry_run_reuse_never_uploads_visualization_file(tmp_path: Path) -> None:
    upload_path = tmp_path / "dry-run.png"
    upload_path.write_bytes(b"dry-run-image")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/evidence-bundles"
        return _json_response(200, _bundle_response("reused"))

    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = record_evidence_bundle(
            client,
            dry_run=True,
            **_bundle_payload(upload_path=str(upload_path)),
        )
    finally:
        client.close()

    assert [request.url.path for request in requests] == ["/evidence-bundles"]
    assert result["data"]["outcome"] == "reused"
    assert result["data"]["attachment"]["outcome"] == "preview"


def test_record_evidence_bundle_preflights_upload_before_commit(tmp_path: Path) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _json_response(500, {})

    missing = tmp_path / "missing.png"
    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(
            mcp_server.LabTrackerAPIValidationError,
            match="does not exist",
        ):
            record_evidence_bundle(
                client,
                dry_run=False,
                **_bundle_payload(upload_path=str(missing)),
            )
    finally:
        client.close()

    assert calls == 0


def test_record_evidence_bundle_rejects_blocked_upload_type_before_commit(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _json_response(500, {})

    upload_path = tmp_path / "figure.svg"
    upload_path.write_text("<svg/>", encoding="utf-8")
    payload = _bundle_payload(upload_path=str(upload_path))
    payload["visualization"]["content_type"] = "image/svg+xml"
    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(
            mcp_server.LabTrackerAPIValidationError,
            match="is not allowed",
        ):
            record_evidence_bundle(client, dry_run=False, **payload)
    finally:
        client.close()

    assert calls == 0


def test_record_evidence_bundle_uploads_immutable_snapshot_and_cleans_it(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "baseline.png"
    original_bytes = b"original-figure"
    replacement_bytes = b"replacement-figure"
    source_path.write_bytes(original_bytes)
    original_checksum = hashlib.sha256(original_bytes).hexdigest()

    class SnapshotClient:
        bundle_payload: dict | None = None
        uploaded_path: Path | None = None
        uploaded_bytes: bytes | None = None

        def record_evidence_bundle(self, **kwargs):
            self.bundle_payload = kwargs
            source_path.write_bytes(replacement_bytes)
            return _bundle_response("created")

        def get_visualization(self, visualization_id: str):
            assert visualization_id == VISUALIZATION_ID
            return {"data": {"viz_id": visualization_id, "asset": None}}

        def upload_visualization_file(self, **kwargs):
            self.uploaded_path = Path(kwargs["file_path"])
            self.uploaded_bytes = self.uploaded_path.read_bytes()
            assert kwargs["checksum_sha256"] == original_checksum
            assert kwargs["size_bytes"] == len(original_bytes)
            assert kwargs["expected_current_storage_id"] == "absent"
            return {
                "data": {
                    "viz_id": kwargs["viz_id"],
                    "asset": {
                        "storage_id": "storage-1",
                        "filename": self.uploaded_path.name,
                        "content_type": kwargs["content_type"],
                        "size_bytes": len(self.uploaded_bytes),
                        "checksum": hashlib.sha256(self.uploaded_bytes).hexdigest(),
                    },
                }
            }

    client = SnapshotClient()
    result = record_evidence_bundle(
        client,
        dry_run=False,
        **_bundle_payload(upload_path=str(source_path)),
    )

    assert client.bundle_payload is not None
    upload_intent = client.bundle_payload["visualization"]["upload_intent"]
    assert upload_intent == {
        "checksum_sha256": original_checksum,
        "size_bytes": len(original_bytes),
        "filename": source_path.name,
        "content_type": "image/png",
    }
    assert source_path.read_bytes() == replacement_bytes
    assert client.uploaded_bytes == original_bytes
    assert client.uploaded_path is not None
    assert client.uploaded_path.name == source_path.name
    assert not client.uploaded_path.exists()
    assert result["data"]["attachment"]["checksum_sha256"] == original_checksum


def test_record_evidence_bundle_conditionally_replaces_observed_asset(
    tmp_path: Path,
) -> None:
    upload_path = tmp_path / "replacement.png"
    content = b"replacement-figure"
    upload_path.write_bytes(content)
    checksum = hashlib.sha256(content).hexdigest()

    class ReplacementClient:
        upload_kwargs: dict | None = None

        def record_evidence_bundle(self, **_kwargs):
            return _bundle_response("reused")

        def get_visualization(self, visualization_id: str):
            return {
                "data": {
                    "viz_id": visualization_id,
                    "asset": {
                        "storage_id": "16ca7383-f9e5-4fe9-87af-2743a960f7b0",
                        "filename": "old.png",
                        "content_type": "image/png",
                        "size_bytes": 3,
                        "checksum": "0" * 64,
                    },
                }
            }

        def upload_visualization_file(self, **kwargs):
            self.upload_kwargs = kwargs
            return {
                "data": {
                    "viz_id": kwargs["viz_id"],
                    "asset": {
                        "storage_id": "87513f36-276d-44c8-b649-a767c66308a1",
                        "filename": upload_path.name,
                        "content_type": "image/png",
                        "size_bytes": len(content),
                        "checksum": checksum,
                    },
                },
                "meta": {"asset_outcome": "replaced"},
            }

    client = ReplacementClient()
    result = record_evidence_bundle(
        client,
        dry_run=False,
        **_bundle_payload(upload_path=str(upload_path)),
    )

    assert client.upload_kwargs is not None
    assert client.upload_kwargs["expected_current_storage_id"] == (
        "16ca7383-f9e5-4fe9-87af-2743a960f7b0"
    )
    assert client.upload_kwargs["checksum_sha256"] == checksum
    assert client.upload_kwargs["size_bytes"] == len(content)
    assert result["data"]["attachment"]["action"] == "uploaded"


def test_record_evidence_bundle_repairs_missing_backing_blob_despite_matching_metadata(
    tmp_path: Path,
) -> None:
    upload_path = tmp_path / "repair.png"
    content = b"repair-missing-backing-blob"
    upload_path.write_bytes(content)
    checksum = hashlib.sha256(content).hexdigest()
    stale_storage_id = "16ca7383-f9e5-4fe9-87af-2743a960f7b0"
    repaired_storage_id = "87513f36-276d-44c8-b649-a767c66308a1"

    class MissingBlobClient:
        upload_kwargs: dict | None = None

        def record_evidence_bundle(self, **_kwargs):
            return _bundle_response("reused")

        def get_visualization(self, visualization_id: str):
            # The database metadata still matches, but only the upload endpoint
            # can authoritatively check whether the backing storage object exists.
            return {
                "data": {
                    "viz_id": visualization_id,
                    "asset": {
                        "storage_id": stale_storage_id,
                        "filename": upload_path.name,
                        "content_type": "image/png",
                        "size_bytes": len(content),
                        "checksum": checksum,
                    },
                }
            }

        def upload_visualization_file(self, **kwargs):
            self.upload_kwargs = kwargs
            return {
                "data": {
                    "viz_id": kwargs["viz_id"],
                    "asset": {
                        "storage_id": repaired_storage_id,
                        "filename": upload_path.name,
                        "content_type": "image/png",
                        "size_bytes": len(content),
                        "checksum": checksum,
                    },
                },
                "meta": {"asset_outcome": "replaced"},
            }

    client = MissingBlobClient()
    result = record_evidence_bundle(
        client,
        dry_run=False,
        **_bundle_payload(upload_path=str(upload_path)),
    )

    assert client.upload_kwargs is not None
    assert client.upload_kwargs["expected_current_storage_id"] == stale_storage_id
    assert result["data"]["attachment"] == {
        "action": "uploaded",
        "entity_type": "visualization_asset",
        "outcome": "created",
        "visualization_id": VISUALIZATION_ID,
        "storage_id": repaired_storage_id,
        "checksum_sha256": checksum,
    }


def test_record_evidence_bundle_accepts_server_side_concurrent_retry_reuse(
    tmp_path: Path,
) -> None:
    upload_path = tmp_path / "concurrent.png"
    content = b"concurrent-figure"
    upload_path.write_bytes(content)
    checksum = hashlib.sha256(content).hexdigest()

    class ConcurrentRetryClient:
        def record_evidence_bundle(self, **_kwargs):
            return _bundle_response("reused")

        def get_visualization(self, visualization_id: str):
            return {"data": {"viz_id": visualization_id, "asset": None}}

        def upload_visualization_file(self, **kwargs):
            assert kwargs["expected_current_storage_id"] == "absent"
            return {
                "data": {
                    "viz_id": kwargs["viz_id"],
                    "asset": {
                        "storage_id": "57b620af-9bc3-471f-936d-bb937a7de726",
                        "filename": upload_path.name,
                        "content_type": "image/png",
                        "size_bytes": len(content),
                        "checksum": checksum,
                    },
                },
                "meta": {"asset_outcome": "reused"},
            }

    result = record_evidence_bundle(
        ConcurrentRetryClient(),
        dry_run=False,
        **_bundle_payload(upload_path=str(upload_path)),
    )

    assert result["data"]["attachment"] == {
        "action": "reuse",
        "entity_type": "visualization_asset",
        "outcome": "reused",
        "visualization_id": VISUALIZATION_ID,
        "storage_id": "57b620af-9bc3-471f-936d-bb937a7de726",
        "checksum_sha256": checksum,
        "reason": "matching_checksum",
    }


@pytest.mark.parametrize(
    "response",
    [
        {"data": {"outcome": "created"}},
        {
            "data": {
                "outcome": "created",
                "dry_run": False,
                "project_id": PROJECT_ID,
                "idempotency_key": BUNDLE_KEY,
                "component_ids": _component_ids(),
                "plan": [{"action": "created", "entity_type": "dataset"}],
                "warnings": [],
            }
        },
    ],
)
def test_record_evidence_bundle_rejects_malformed_success_response(
    response: dict,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(201, response)

    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(mcp_server.LabTrackerAPIError) as excinfo:
            record_evidence_bundle(client, dry_run=False, **_bundle_payload())
    finally:
        client.close()

    assert excinfo.value.code == "invalid_evidence_bundle_response"


def test_record_evidence_bundle_requires_ids_for_requested_components() -> None:
    component_ids: dict[str, str | None] = _component_ids()
    component_ids["claim_id"] = None

    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(
            201,
            _bundle_response("created", component_ids=component_ids),
        )

    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(mcp_server.LabTrackerAPIError) as excinfo:
            record_evidence_bundle(client, dry_run=False, **_bundle_payload())
    finally:
        client.close()

    assert excinfo.value.code == "invalid_evidence_bundle_response"
    assert "claim_id" in str(excinfo.value)


def test_record_evidence_bundle_surfaces_committed_attachment_failure(
    tmp_path: Path,
) -> None:
    upload_path = tmp_path / "baseline.png"
    upload_path.write_bytes(b"fake-image")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/evidence-bundles":
            return _json_response(201, _bundle_response("created"))
        if request.url.path == f"/visualizations/{VISUALIZATION_ID}":
            return _json_response(200, {"data": {"asset": None}})
        if request.url.path == f"/visualizations/{VISUALIZATION_ID}/file":
            return _json_response(503, {"error": {"message": "storage unavailable"}})
        return _json_response(404, {})

    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(mcp_server.LabTrackerAPIError) as excinfo:
            record_evidence_bundle(
                client,
                dry_run=False,
                **_bundle_payload(upload_path=str(upload_path)),
            )
    finally:
        client.close()

    assert excinfo.value.code == "evidence_bundle_attachment_failed"
    assert "already committed" in str(excinfo.value)
    assert "same idempotency_key" in str(excinfo.value)
    assert "storage unavailable" in str(excinfo.value)


def test_record_evidence_bundle_rejects_mismatched_uploaded_asset_metadata(
    tmp_path: Path,
) -> None:
    upload_path = tmp_path / "baseline.png"
    upload_path.write_bytes(b"fake-image")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/evidence-bundles":
            return _json_response(201, _bundle_response("created"))
        if request.url.path == f"/visualizations/{VISUALIZATION_ID}":
            return _json_response(200, {"data": {"asset": None}})
        if request.url.path == f"/visualizations/{VISUALIZATION_ID}/file":
            return _json_response(
                201,
                {
                    "data": {
                        "asset": {
                            "storage_id": "storage-1",
                            "filename": "baseline.png",
                            "content_type": "image/png",
                            "size_bytes": len(b"fake-image"),
                            "checksum": "0" * 64,
                        }
                    }
                },
            )
        return _json_response(404, {})

    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(mcp_server.LabTrackerAPIError) as excinfo:
            record_evidence_bundle(
                client,
                dry_run=False,
                **_bundle_payload(upload_path=str(upload_path)),
            )
    finally:
        client.close()

    assert excinfo.value.code == "evidence_bundle_attachment_failed"
    assert "checksum" in str(excinfo.value)


def test_record_evidence_bundle_conflict_preserves_http_409() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(
            409,
            {"error": {"code": "conflict", "message": "idempotency key conflict"}},
        )

    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(mcp_server.LabTrackerAPIError) as excinfo:
            record_evidence_bundle(client, **_bundle_payload())
    finally:
        client.close()

    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "conflict"
