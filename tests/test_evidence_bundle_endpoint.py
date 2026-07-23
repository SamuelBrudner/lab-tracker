from __future__ import annotations

import json
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from lab_tracker.auth import Role
from lab_tracker.mcp_api_client import LabTrackerAPIClient, MCPSettings


def _project(client: TestClient, headers: dict[str, str], name: str = "Bundle project") -> str:
    response = client.post("/projects", json={"name": name}, headers=headers)
    assert response.status_code == 201
    return response.json()["data"]["project_id"]


def _question(client: TestClient, headers: dict[str, str], project_id: str) -> str:
    response = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Does the bundle remain atomic?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["data"]["question_id"]


def _note_bundle(project_id: str, *, content: str = "Atomic evidence") -> dict[str, object]:
    return {
        "project_id": project_id,
        "source_note": {"kind": "create", "raw_content": content},
        "dry_run": False,
        "idempotency_key": "bundle-note-key",
    }


def test_evidence_bundle_create_replay_and_conflict(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)

    created = client.post(
        "/evidence-bundles",
        json=_note_bundle(project_id),
        headers=admin_auth_headers,
    )
    replayed = client.post(
        "/evidence-bundles",
        json=_note_bundle(project_id, content="  Atomic evidence  "),
        headers=admin_auth_headers,
    )
    conflict = client.post(
        "/evidence-bundles",
        json=_note_bundle(project_id, content="Different evidence"),
        headers=admin_auth_headers,
    )

    assert created.status_code == 201, created.text
    assert replayed.status_code == 200, replayed.text
    assert conflict.status_code == 409, conflict.text
    created_data = created.json()["data"]
    replayed_data = replayed.json()["data"]
    assert created_data["outcome"] == "created"
    assert replayed_data["outcome"] == "reused"
    assert replayed_data["component_ids"] == created_data["component_ids"]
    assert conflict.json()["error"]["code"] == "conflict"

    notes = client.get(
        "/notes",
        params={"project_id": project_id, "limit": 50},
        headers=admin_auth_headers,
    )
    assert notes.status_code == 200
    assert len(notes.json()["data"]) == 1


def test_evidence_bundle_preview_is_mutation_free(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    response = client.post(
        "/evidence-bundles",
        json={
            "project_id": project_id,
            "source_note": {"kind": "create", "raw_content": "Preview only"},
        },
        headers=admin_auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["outcome"] == "preview"
    assert response.json()["data"]["component_ids"]["source_note_id"] is None
    notes = client.get(
        "/notes",
        params={"project_id": project_id, "limit": 50},
        headers=admin_auth_headers,
    )
    assert notes.json()["data"] == []


def test_keyed_evidence_bundle_preview_matches_commit_idempotency_semantics(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    committed_payload = _note_bundle(project_id, content="Canonical keyed evidence")
    created = client.post(
        "/evidence-bundles",
        json=committed_payload,
        headers=admin_auth_headers,
    )
    replay_payload = deepcopy(committed_payload)
    replay_payload["dry_run"] = True
    reused = client.post(
        "/evidence-bundles",
        json=replay_payload,
        headers=admin_auth_headers,
    )
    conflict_payload = deepcopy(replay_payload)
    conflict_payload["source_note"]["raw_content"] = "Conflicting keyed evidence"
    conflict = client.post(
        "/evidence-bundles",
        json=conflict_payload,
        headers=admin_auth_headers,
    )

    assert created.status_code == 201, created.text
    assert reused.status_code == 200, reused.text
    assert reused.json()["data"]["outcome"] == "reused"
    assert reused.json()["data"]["component_ids"] == created.json()["data"]["component_ids"]
    assert conflict.status_code == 409, conflict.text
    notes = client.get(
        "/notes",
        params={"project_id": project_id, "limit": 50},
        headers=admin_auth_headers,
    )
    assert len(notes.json()["data"]) == 1


def test_mcp_client_commits_upload_intent_and_omits_absent_components(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    question_id = _question(client, admin_auth_headers, project_id)
    seen_payload: dict[str, object] = {}

    def forward_to_fastapi(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.content))
        response = client.request(
            request.method,
            request.url.path,
            content=request.content,
            headers={
                **admin_auth_headers,
                "content-type": request.headers["content-type"],
            },
        )
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers=response.headers,
            request=request,
        )

    mcp_client = LabTrackerAPIClient(
        MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(forward_to_fastapi),
    )
    try:
        request = {
            "project_id": project_id,
            "primary_question_id": question_id,
            "dataset": {"kind": "create"},
            "analysis": {
                "kind": "create",
                "method_hash": "mcp-method",
                "code_version": "git:mcp-boundary",
            },
            "visualization": {
                "kind": "create",
                "viz_type": "line",
                "file_path": "figures/mcp-boundary.png",
                "upload_intent": {
                    "checksum_sha256": "a" * 64,
                    "size_bytes": 17,
                    "filename": "mcp-boundary.png",
                    "content_type": "image/png",
                },
            },
            "dry_run": False,
            "idempotency_key": "mcp-boundary-key",
        }
        created = mcp_client.record_evidence_bundle(**request)
        replayed = mcp_client.record_evidence_bundle(**request)
    finally:
        mcp_client.close()

    assert created["data"]["outcome"] == "created"
    assert replayed["data"]["outcome"] == "reused"
    assert seen_payload["visualization"]["upload_intent"] == {
        "checksum_sha256": "a" * 64,
        "size_bytes": 17,
        "filename": "mcp-boundary.png",
        "content_type": "image/png",
    }
    visualization_steps = [
        step for step in created["data"]["plan"] if step["entity_type"] == "visualization"
    ]
    assert (
        visualization_steps[0]["details"]["upload_intent"]
        == seen_payload["visualization"]["upload_intent"]
    )
    for absent_component in ("claim", "source_note"):
        assert absent_component not in seen_payload


@pytest.mark.parametrize(
    "source_note",
    [None, [], "note", {}, {"kind": "existing", "note_id": str(uuid4()), "raw_content": "mixed"}],
)
def test_evidence_bundle_rejects_malformed_components(
    source_note: object,
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    response = client.post(
        "/evidence-bundles",
        json={"project_id": project_id, "source_note": source_note},
        headers=admin_auth_headers,
    )
    assert response.status_code == 422


def test_evidence_bundle_rejects_empty_commit_and_missing_key(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    empty = client.post(
        "/evidence-bundles",
        json={"project_id": project_id},
        headers=admin_auth_headers,
    )
    missing_key = client.post(
        "/evidence-bundles",
        json={
            "project_id": project_id,
            "source_note": {"kind": "create", "raw_content": "No key"},
            "dry_run": False,
        },
        headers=admin_auth_headers,
    )
    assert empty.status_code == 422
    assert missing_key.status_code == 422


def test_late_invalid_component_writes_nothing(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    question_id = _question(client, admin_auth_headers, project_id)
    response = client.post(
        "/evidence-bundles",
        json={
            "project_id": project_id,
            "primary_question_id": question_id,
            "dataset": {"kind": "create"},
            "analysis": {
                "kind": "create",
                "method_hash": "method-v1",
                "code_version": "code-v1",
                "status": "committed",
            },
            "dry_run": False,
            "idempotency_key": "invalid-late-component",
        },
        headers=admin_auth_headers,
    )
    assert response.status_code == 422, response.text
    datasets = client.get(
        "/datasets",
        params={"project_id": project_id, "limit": 50},
        headers=admin_auth_headers,
    )
    assert datasets.json()["data"] == []


def test_complete_bundle_infers_links_and_note_target(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    question_id = _question(client, admin_auth_headers, project_id)
    response = client.post(
        "/evidence-bundles",
        json={
            "project_id": project_id,
            "primary_question_id": question_id,
            "dataset": {"kind": "create"},
            "analysis": {
                "kind": "create",
                "method_hash": "method-v1",
                "code_version": "code-v1",
            },
            "claim": {
                "kind": "create",
                "statement": "The evidence is linked.",
                "confidence": 80,
                "status": "supported",
            },
            "visualization": {
                "kind": "create",
                "viz_type": "line",
                "file_path": "figures/evidence.png",
            },
            "source_note": {"kind": "create", "raw_content": "Linked source note"},
            "dry_run": False,
            "idempotency_key": "complete-bundle",
        },
        headers=admin_auth_headers,
    )
    assert response.status_code == 201, response.text
    ids = response.json()["data"]["component_ids"]
    assert all(ids.values())

    note = client.get(f"/notes/{ids['source_note_id']}", headers=admin_auth_headers)
    assert note.status_code == 200
    assert note.json()["data"]["targets"] == [
        {"entity_type": "visualization", "entity_id": ids["visualization_id"]}
    ]
    assert "lab_tracker_evidence_bundle_idempotency_key" not in note.json()["data"]["metadata"]

    dataset = client.get(f"/datasets/{ids['dataset_id']}", headers=admin_auth_headers)
    assert dataset.status_code == 200
    assert (
        "lab_tracker_evidence_bundle_idempotency_key"
        not in dataset.json()["data"]["commit_manifest"]["metadata"]
    )

    analysis = client.get(f"/analyses/{ids['analysis_id']}", headers=admin_auth_headers)
    assert analysis.json()["data"]["dataset_ids"] == [ids["dataset_id"]]
    claim = client.get(f"/claims/{ids['claim_id']}", headers=admin_auth_headers)
    assert claim.json()["data"]["status"] == "supported"
    assert claim.json()["data"]["supported_by_dataset_ids"] == [ids["dataset_id"]]
    assert claim.json()["data"]["supported_by_analysis_ids"] == [ids["analysis_id"]]
    assert claim.json()["data"]["answers_question_ids"] == [question_id]
    visualization = client.get(
        f"/visualizations/{ids['visualization_id']}",
        headers=admin_auth_headers,
    )
    assert visualization.json()["data"]["analysis_id"] == ids["analysis_id"]
    assert visualization.json()["data"]["related_claim_ids"] == [ids["claim_id"]]


def test_preview_exposes_normalized_values_and_inferred_links(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    question_id = _question(client, admin_auth_headers, project_id)
    response = client.post(
        "/evidence-bundles",
        json={
            "project_id": project_id,
            "primary_question_id": question_id,
            "dataset": {
                "kind": "create",
                "commit_manifest": {
                    "files": [{"path": " data.csv ", "checksum": " abc "}],
                },
            },
            "analysis": {
                "kind": "create",
                "method_hash": " method-v1 ",
                "code_version": " code-v1 ",
            },
            "claim": {
                "kind": "create",
                "statement": " A linked claim. ",
                "confidence": 80,
            },
            "visualization": {
                "kind": "create",
                "viz_type": " line ",
                "file_path": " figures/evidence.png ",
            },
            "source_note": {"kind": "create", "raw_content": " Source note "},
        },
        headers=admin_auth_headers,
    )
    assert response.status_code == 200, response.text
    plan = {step["entity_type"]: step["details"] for step in response.json()["data"]["plan"]}
    assert plan["dataset"]["commit_manifest"]["files"] == [{"path": "data.csv", "checksum": "abc"}]
    assert plan["analysis"]["dataset_ids"] == ["$bundle.dataset"]
    assert plan["claim"]["supported_by_analysis_ids"] == ["$bundle.analysis"]
    assert plan["visualization"]["analysis_id"] == "$bundle.analysis"
    assert plan["source_note"]["targets"] == [
        {
            "entity_type": "visualization",
            "entity_id": "$bundle.visualization",
        }
    ]


@pytest.mark.parametrize(
    "component",
    [
        {
            "dataset": {
                "kind": "create",
                "status": "committed",
            }
        },
        {
            "claim": {
                "kind": "create",
                "statement": "Rejected without rationale",
                "confidence": 10,
                "status": "rejected",
            }
        },
    ],
)
@pytest.mark.parametrize("dry_run", [True, False])
def test_preview_and_commit_share_status_validation(
    component: dict[str, object],
    dry_run: bool,
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    question_id = _question(client, admin_auth_headers, project_id)
    payload: dict[str, object] = {
        "project_id": project_id,
        "primary_question_id": question_id,
        **deepcopy(component),
        "dry_run": dry_run,
    }
    if not dry_run:
        payload["idempotency_key"] = f"invalid-status-{uuid4()}"
    response = client.post(
        "/evidence-bundles",
        json=payload,
        headers=admin_auth_headers,
    )
    assert response.status_code == 422, response.text


def test_manifest_normalization_controls_replay_and_size_conflicts(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    question_id = _question(client, admin_auth_headers, project_id)
    note_ids: list[str] = []
    for index in range(2):
        note = client.post(
            "/notes",
            json={
                "project_id": project_id,
                "raw_content": f"Manifest normalization note {index}",
            },
            headers=admin_auth_headers,
        )
        assert note.status_code == 201, note.text
        note_ids.append(note.json()["data"]["note_id"])
    key = "normalized-manifest"
    first_manifest = {
        "files": [
            {
                "file_id": str(uuid4()),
                "path": " b.csv ",
                "checksum": " checksum-b ",
                "size_bytes": 20,
            },
            {"path": "a.csv", "checksum": "checksum-a", "size_bytes": 10},
        ],
        "metadata": {" beta ": " two ", "alpha": "one"},
        "note_ids": note_ids,
    }
    base = {
        "project_id": project_id,
        "primary_question_id": question_id,
        "dataset": {"kind": "create", "commit_manifest": first_manifest},
        "dry_run": False,
        "idempotency_key": key,
    }
    created = client.post(
        "/evidence-bundles",
        json=base,
        headers=admin_auth_headers,
    )
    assert created.status_code == 201, created.text
    dataset_id = created.json()["data"]["component_ids"]["dataset_id"]
    dataset = client.get(f"/datasets/{dataset_id}", headers=admin_auth_headers)
    commit_hash = dataset.json()["data"]["commit_hash"]

    equivalent = deepcopy(base)
    equivalent["dataset"] = {
        "kind": "create",
        "commit_hash": commit_hash,
        "commit_manifest": {
            "files": [
                {
                    "file_id": str(uuid4()),
                    "path": " a.csv ",
                    "checksum": " checksum-a ",
                    "size_bytes": 10,
                },
                {"path": "b.csv", "checksum": "checksum-b", "size_bytes": 20},
            ],
            "metadata": {"alpha": "one", "beta": "two"},
            "note_ids": list(reversed(note_ids)),
        },
    }
    replay = client.post(
        "/evidence-bundles",
        json=equivalent,
        headers=admin_auth_headers,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["component_ids"]["dataset_id"] == dataset_id

    conflicting_size = deepcopy(equivalent)
    conflicting_size["dataset"]["commit_manifest"]["files"][0]["size_bytes"] = 11
    conflict = client.post(
        "/evidence-bundles",
        json=conflicting_size,
        headers=admin_auth_headers,
    )
    assert conflict.status_code == 409, conflict.text


@pytest.mark.parametrize("target", ["dataset", "source_note"])
def test_reserved_idempotency_metadata_key_is_rejected(
    target: str,
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    question_id = _question(client, admin_auth_headers, project_id)
    reserved = "lab_tracker_evidence_bundle_idempotency_key"
    component = (
        {
            "dataset": {
                "kind": "create",
                "commit_manifest": {"metadata": {reserved: "must-not-leak"}},
            }
        }
        if target == "dataset"
        else {
            "source_note": {
                "kind": "create",
                "raw_content": "Attempted key leak",
                "metadata": {reserved: "must-not-leak"},
            }
        }
    )
    response = client.post(
        "/evidence-bundles",
        json={
            "project_id": project_id,
            "primary_question_id": question_id,
            **component,
            "dry_run": False,
            "idempotency_key": "must-not-leak",
        },
        headers=admin_auth_headers,
    )
    assert response.status_code == 422, response.text


def test_openapi_does_not_advertise_explicit_null_components(
    client: TestClient,
) -> None:
    schema = client.get("/openapi.json").json()["components"]["schemas"]["EvidenceBundleRequest"]
    for field_name in ("dataset", "analysis", "claim", "visualization", "source_note"):
        field_schema = schema["properties"][field_name]
        variants = [*field_schema.get("anyOf", []), *field_schema.get("oneOf", [])]
        assert not any(variant.get("type") == "null" for variant in variants)


def test_evidence_bundle_keys_are_scoped_to_the_authenticated_user(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    username = f"bundle-editor-{uuid4().hex[:8]}"
    password = "secret"
    editor = client.app.state.auth_service.register_user(
        username=username,
        password=password,
        role=Role.EDITOR,
    )
    login = client.post("/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200
    editor_headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
    membership = client.post(
        f"/projects/{project_id}/members",
        json={"user_id": str(editor.user_id), "role": "contributor"},
        headers=admin_auth_headers,
    )
    assert membership.status_code == 201, membership.text

    admin_result = client.post(
        "/evidence-bundles",
        json=_note_bundle(project_id, content="Admin evidence"),
        headers=admin_auth_headers,
    )
    editor_result = client.post(
        "/evidence-bundles",
        json=_note_bundle(project_id, content="Editor evidence"),
        headers=editor_headers,
    )
    assert admin_result.status_code == 201, admin_result.text
    assert editor_result.status_code == 201, editor_result.text
    assert (
        admin_result.json()["data"]["component_ids"]["source_note_id"]
        != editor_result.json()["data"]["component_ids"]["source_note_id"]
    )


def test_revoked_member_cannot_replay_or_probe_bundle_key(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    username = f"revoked-bundle-editor-{uuid4().hex[:8]}"
    password = "secret"
    editor = client.app.state.auth_service.register_user(
        username=username,
        password=password,
        role=Role.EDITOR,
    )
    login = client.post("/auth/login", json={"username": username, "password": password})
    editor_headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
    membership = client.post(
        f"/projects/{project_id}/members",
        json={"user_id": str(editor.user_id), "role": "contributor"},
        headers=admin_auth_headers,
    )
    assert membership.status_code == 201, membership.text
    created = client.post(
        "/evidence-bundles",
        json=_note_bundle(project_id, content="Soon inaccessible"),
        headers=editor_headers,
    )
    assert created.status_code == 201, created.text

    admin_user_id = client.get(
        "/auth/me",
        headers=admin_auth_headers,
    ).json()["data"]["user_id"]
    reassigned = client.post(
        "/ownership-reassignments",
        json={
            "from_user_id": str(editor.user_id),
            "to_user_id": admin_user_id,
            "reason": "Exercise replay authorization after offboarding.",
        },
        headers=admin_auth_headers,
    )
    assert reassigned.status_code == 201, reassigned.text

    revoked = client.delete(
        f"/projects/{project_id}/members/{editor.user_id}",
        headers=admin_auth_headers,
    )
    assert revoked.status_code == 200, revoked.text

    replay = client.post(
        "/evidence-bundles",
        json=_note_bundle(project_id, content="Soon inaccessible"),
        headers=editor_headers,
    )
    conflicting_probe = client.post(
        "/evidence-bundles",
        json=_note_bundle(project_id, content="Probe existing key"),
        headers=editor_headers,
    )
    assert replay.status_code == 401
    assert conflicting_probe.status_code == 401
    assert replay.json()["error"]["message"] == conflicting_probe.json()["error"]["message"]


def test_non_admin_bundle_member_cannot_probe_cross_project_component_ids(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    visible_project_id = _project(
        client,
        admin_auth_headers,
        name="Visible component project",
    )
    hidden_project_id = _project(
        client,
        admin_auth_headers,
        name="Hidden component project",
    )
    hidden_note_response = client.post(
        "/notes",
        json={
            "project_id": hidden_project_id,
            "raw_content": "Hidden component",
        },
        headers=admin_auth_headers,
    )
    assert hidden_note_response.status_code == 201, hidden_note_response.text
    hidden_note_id = hidden_note_response.json()["data"]["note_id"]

    username = f"bundle-component-member-{uuid4().hex[:8]}"
    password = "secret"
    member = client.app.state.auth_service.register_user(
        username=username,
        password=password,
        role=Role.EDITOR,
    )
    login = client.post(
        "/auth/login",
        json={"username": username, "password": password},
    )
    assert login.status_code == 200, login.text
    member_headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
    membership = client.post(
        f"/projects/{visible_project_id}/members",
        json={"user_id": str(member.user_id), "role": "contributor"},
        headers=admin_auth_headers,
    )
    assert membership.status_code == 201, membership.text

    responses = [
        client.post(
            "/evidence-bundles",
            json={
                "project_id": visible_project_id,
                "source_note": {"kind": "existing", "note_id": note_id},
            },
            headers=member_headers,
        )
        for note_id in (hidden_note_id, str(uuid4()))
    ]

    assert [response.status_code for response in responses] == [404, 404]
    assert responses[0].json()["error"] == responses[1].json()["error"]

    visible_question_id = _question(
        client,
        admin_auth_headers,
        visible_project_id,
    )
    manifest_note_responses = [
        client.post(
            "/evidence-bundles",
            json={
                "project_id": visible_project_id,
                "primary_question_id": visible_question_id,
                "dataset": {
                    "kind": "create",
                    "commit_manifest": {"note_ids": [note_id]},
                },
            },
            headers=member_headers,
        )
        for note_id in (hidden_note_id, str(uuid4()))
    ]

    assert [response.status_code for response in manifest_note_responses] == [404, 404]
    assert (
        manifest_note_responses[0].json()["error"]
        == manifest_note_responses[1].json()["error"]
    )
