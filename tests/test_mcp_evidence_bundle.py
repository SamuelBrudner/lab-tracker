from __future__ import annotations

import json
from pathlib import Path

import httpx

from lab_tracker import mcp_server
from lab_tracker.mcp_evidence_bundle import (
    IDEMPOTENCY_METADATA_KEY,
    record_evidence_bundle,
)


def _json_response(status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def _empty_list() -> dict:
    return {"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}}


def _bundle_payload(upload_path: str | None = None) -> dict:
    visualization: dict = {
        "viz_type": "line",
        "file_path": upload_path or "figures/baseline.png",
        "caption": "Baseline comparison",
    }
    if upload_path is not None:
        visualization["upload_file"] = True
    return {
        "project_id": "project-1",
        "primary_question_id": "question-1",
        "idempotency_key": "bundle-1",
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
            "confidence": 0.8,
            "status": "supported",
        },
        "visualization": visualization,
        "source_note": {
            "raw_content": "Notebook source for baseline result.",
            "status": "committed",
        },
    }


def test_record_evidence_bundle_dry_run_plans_without_writes() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        return _json_response(200, _empty_list())

    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(handler),
    )

    try:
        payload = record_evidence_bundle(client, dry_run=True, **_bundle_payload())
    finally:
        client.close()

    data = payload["data"]
    assert data["dry_run"] is True
    assert [item["entity_type"] for item in data["plan"][:4]] == [
        "dataset",
        "analysis",
        "claim",
        "visualization",
    ]
    assert data["plan"][0]["request"]["commit_manifest"]["metadata"][
        IDEMPOTENCY_METADATA_KEY
    ] == "bundle-1"
    assert all(request.method == "GET" for request in requests)


def test_record_evidence_bundle_commit_reuses_on_idempotent_retry(tmp_path: Path) -> None:
    upload_path = tmp_path / "baseline.png"
    upload_path.write_bytes(b"fake-image")
    state: dict[str, list[dict]] = {
        "datasets": [],
        "analyses": [],
        "claims": [],
        "visualizations": [],
        "notes": [],
    }
    post_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            if request.url.path == "/datasets":
                return _json_response(200, {"data": state["datasets"], "meta": {}})
            if request.url.path == "/analyses":
                return _json_response(200, {"data": state["analyses"], "meta": {}})
            if request.url.path == "/claims":
                return _json_response(200, {"data": state["claims"], "meta": {}})
            if request.url.path == "/visualizations":
                return _json_response(200, {"data": state["visualizations"], "meta": {}})
            if request.url.path == "/notes":
                return _json_response(200, {"data": state["notes"], "meta": {}})
        post_paths.append(request.url.path)
        if request.url.path == "/datasets":
            body = json.loads(request.content.decode("utf-8"))
            assert body["commit_manifest"]["metadata"][IDEMPOTENCY_METADATA_KEY] == (
                "bundle-1"
            )
            item = {
                "dataset_id": "dataset-1",
                "project_id": body["project_id"],
                "primary_question_id": body["primary_question_id"],
                "commit_hash": body["commit_hash"],
                "commit_manifest": body["commit_manifest"],
                "status": body["status"],
            }
            state["datasets"].append(item)
            return _json_response(201, {"data": item})
        if request.url.path == "/analyses":
            body = json.loads(request.content.decode("utf-8"))
            item = {
                "analysis_id": "analysis-1",
                "project_id": body["project_id"],
                "dataset_ids": body["dataset_ids"],
                "method_hash": body["method_hash"],
                "code_version": body["code_version"],
                "status": body["status"],
            }
            state["analyses"].append(item)
            return _json_response(201, {"data": item})
        if request.url.path == "/claims":
            body = json.loads(request.content.decode("utf-8"))
            item = {
                "claim_id": "claim-1",
                "project_id": body["project_id"],
                "statement": body["statement"],
                "confidence": body["confidence"],
                "status": body["status"],
                "supported_by_dataset_ids": body["supported_by_dataset_ids"],
                "supported_by_analysis_ids": body["supported_by_analysis_ids"],
            }
            state["claims"].append(item)
            return _json_response(201, {"data": item})
        if request.url.path == "/visualizations":
            body = json.loads(request.content.decode("utf-8"))
            item = {
                "viz_id": "visualization-1",
                "analysis_id": body["analysis_id"],
                "viz_type": body["viz_type"],
                "file_path": body["file_path"],
                "caption": body["caption"],
                "related_claim_ids": body["related_claim_ids"],
            }
            state["visualizations"].append(item)
            return _json_response(201, {"data": item})
        if request.url.path == "/visualizations/visualization-1/file":
            state["visualizations"][0]["asset"] = {"storage_id": "storage-1"}
            return _json_response(201, {"data": {"storage_id": "storage-1"}})
        if request.url.path == "/notes":
            body = json.loads(request.content.decode("utf-8"))
            item = {
                "note_id": "note-1",
                "project_id": body["project_id"],
                "raw_content": body["raw_content"],
                "targets": body["targets"],
                "metadata": body["metadata"],
                "status": body["status"],
            }
            state["notes"].append(item)
            return _json_response(201, {"data": item})
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
        post_count_after_first = len(post_paths)
        second = record_evidence_bundle(
            client,
            dry_run=False,
            **_bundle_payload(upload_path=str(upload_path)),
        )
    finally:
        client.close()

    assert first["data"]["created"] == {
        "notes": ["note-1"],
        "datasets": ["dataset-1"],
        "analyses": ["analysis-1"],
        "claims": ["claim-1"],
        "visualizations": ["visualization-1"],
    }
    assert post_paths == [
        "/datasets",
        "/analyses",
        "/claims",
        "/visualizations",
        "/visualizations/visualization-1/file",
        "/notes",
    ]
    assert len(post_paths) == post_count_after_first
    assert second["data"]["reused"]["datasets"] == ["dataset-1"]
    assert second["data"]["reused"]["analyses"] == ["analysis-1"]
    assert second["data"]["reused"]["claims"] == ["claim-1"]
    assert second["data"]["reused"]["visualizations"] == ["visualization-1"]
    assert second["data"]["reused"]["notes"] == ["note-1"]
