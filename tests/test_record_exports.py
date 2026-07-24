from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from lab_tracker.auth import Role
from lab_tracker.db_models import (
    GoalLinkModel,
    QuestionModel,
    RecordExportEventModel,
    UsageEventModel,
)
from lab_tracker.errors import NotFoundError
from lab_tracker.services.goal_service import GoalService
from lab_tracker.services.record_export_service import RecordExportService


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register_user(
    client: TestClient,
    *,
    role: Role,
) -> tuple[dict[str, str], str]:
    username = f"record-export-{role.value}-{uuid4().hex[:8]}"
    user = client.app.state.auth_service.register_user(
        username=username,
        password="secret",
        role=role,
    )
    login_response = client.post(
        "/auth/login",
        json={"username": username, "password": "secret"},
    )
    assert login_response.status_code == 200
    return _auth_headers(login_response.json()["data"]["access_token"]), str(user.user_id)


def _create_project_bundle(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_name: str,
    group_id: str | None = None,
) -> dict[str, str]:
    project_payload = {"name": project_name}
    if group_id is not None:
        project_payload["group_id"] = group_id
    project_id = client.post(
        "/projects",
        json=project_payload,
        headers=headers,
    ).json()["data"]["project_id"]
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": f"{project_name} question",
            "question_type": "descriptive",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    dataset_id = client.post(
        "/datasets",
        json={"project_id": project_id, "primary_question_id": question_id},
        headers=headers,
    ).json()["data"]["dataset_id"]
    note_id = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": f"{project_name} note"},
        headers=headers,
    ).json()["data"]["note_id"]
    session_id = client.post(
        "/sessions",
        json={"project_id": project_id, "session_type": "operational"},
        headers=headers,
    ).json()["data"]["session_id"]
    analysis_id = client.post(
        "/analyses",
        json={
            "project_id": project_id,
            "dataset_ids": [dataset_id],
            "method_hash": f"{project_name}-method",
            "code_version": f"{project_name}-code",
        },
        headers=headers,
    ).json()["data"]["analysis_id"]
    claim_id = client.post(
        "/claims",
        json={
            "project_id": project_id,
            "statement": f"{project_name} claim",
            "confidence": 75,
            "supported_by_dataset_ids": [dataset_id],
            "supported_by_analysis_ids": [analysis_id],
        },
        headers=headers,
    ).json()["data"]["claim_id"]
    viz_id = client.post(
        "/visualizations",
        json={
            "analysis_id": analysis_id,
            "viz_type": "line",
            "file_path": f"figures/{project_name}.png",
            "caption": f"{project_name} figure",
            "related_claim_ids": [claim_id],
        },
        headers=headers,
    ).json()["data"]["viz_id"]
    return {
        "project_id": project_id,
        "question_id": question_id,
        "dataset_id": dataset_id,
        "note_id": note_id,
        "session_id": session_id,
        "analysis_id": analysis_id,
        "claim_id": claim_id,
        "viz_id": viz_id,
    }


def _graph_ids(export_payload: dict[str, object]) -> set[str]:
    graph = export_payload["provenance"]["@graph"]
    assert isinstance(graph, list)
    return {str(node["@id"]) for node in graph if isinstance(node, dict) and "@id" in node}


def _layer_graph_ids(layer_payload: dict[str, object]) -> set[str]:
    graph = layer_payload["@graph"]
    assert isinstance(graph, list)
    return {str(node["@id"]) for node in graph if isinstance(node, dict) and "@id" in node}


def _node_by_id(document: dict[str, object], node_id: str) -> dict[str, object]:
    graph = document["@graph"]
    assert isinstance(graph, list)
    for node in graph:
        assert isinstance(node, dict)
        if node.get("@id") == node_id:
            return node
    raise AssertionError(f"Node not found: {node_id}")


def _record_export_event_count(client: TestClient) -> int:
    with client.app.state.db_session_factory() as session:
        return len(list(session.scalars(select(RecordExportEventModel.export_id))))


def _usage_events(client: TestClient) -> list[UsageEventModel]:
    with client.app.state.db_session_factory() as session:
        return list(session.scalars(select(UsageEventModel)))


def _assert_single_ara_usage_event(
    client: TestClient,
    *,
    resource_type: str,
    resource_id: str,
    outcome: str,
    actor_user_id: str | None = None,
) -> None:
    [event] = _usage_events(client)
    assert event.verb == "export"
    assert event.resource_type == resource_type
    assert str(event.resource_id) == resource_id
    assert event.outcome == outcome
    assert event.surface == "http"
    assert event.actor_role == "admin"
    assert event.principal_type == "user"
    if actor_user_id is not None:
        assert str(event.actor_user_id) == actor_user_id


def _create_ara_bundle(
    client: TestClient,
    headers: dict[str, str],
) -> dict[str, str]:
    project_id = client.post(
        "/projects",
        json={"name": "Ara export project"},
        headers=headers,
    ).json()["data"]["project_id"]
    root_question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "What mechanism supports the paper?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    child_question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Does the fitted analysis support the main claim?",
            "question_type": "hypothesis_driven",
            "hypothesis": "The fitted analysis supports the claim.",
            "status": "active",
            "parent_question_ids": [root_question_id],
        },
        headers=headers,
    ).json()["data"]["question_id"]
    note_id = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": "Figure note: use the fitted signal panel.",
            "status": "committed",
        },
        headers=headers,
    ).json()["data"]["note_id"]
    dataset_id = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": child_question_id,
            "status": "committed",
            "commit_manifest": {
                "files": [{"path": "raw/signal.csv", "checksum": "sha256:data"}],
                "note_ids": [note_id],
            },
        },
        headers=headers,
    ).json()["data"]["dataset_id"]
    run_pointer = {
        "kind": "activity",
        "source_system": "mlflow",
        "uri": "mlflow://experiments/ara/runs/run-001",
        "content_hash": "sha256:run001",
    }
    analysis_id = client.post(
        "/analyses",
        json={
            "project_id": project_id,
            "dataset_ids": [dataset_id],
            "method_hash": "method-fit",
            "code_version": "git:abc123",
            "environment_hash": "uv:lock123",
            "external_artifacts": [run_pointer],
            "status": "committed",
        },
        headers=headers,
    ).json()["data"]["analysis_id"]
    claim_id = client.post(
        "/claims",
        json={
            "project_id": project_id,
            "statement": "The fitted signal supports the paper mechanism.",
            "confidence": 88,
            "status": "supported",
            "supported_by_analysis_ids": [analysis_id],
            "answers_question_ids": [child_question_id],
        },
        headers=headers,
    ).json()["data"]["claim_id"]
    note_target_response = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": "Claim note: this is the narrative anchor.",
            "status": "committed",
            "targets": [{"entity_type": "claim", "entity_id": claim_id}],
        },
        headers=headers,
    )
    assert note_target_response.status_code == 201, note_target_response.text
    viz_id = client.post(
        "/visualizations",
        json={
            "analysis_id": analysis_id,
            "viz_type": "line",
            "file_path": "figures/signal.png",
            "caption": "Fitted signal",
            "related_claim_ids": [claim_id],
        },
        headers=headers,
    ).json()["data"]["viz_id"]
    goal_id = client.post(
        f"/projects/{project_id}/goals",
        json={
            "goal_type": "paper",
            "title": "Ara paper",
            "summary": "Compiled paper artifact.",
            "status": "in_progress",
        },
        headers=headers,
    ).json()["data"]["goal_id"]
    for entity_type, entity_id, relation in [
        ("question", root_question_id, "addresses"),
        ("claim", claim_id, "supporting_evidence"),
        ("visualization", viz_id, "candidate_figure"),
    ]:
        response = client.post(
            f"/goals/{goal_id}/links",
            json={
                "entity_type": entity_type,
                "entity_id": entity_id,
                "relation": relation,
                "link_status": "committed",
            },
            headers=headers,
        )
        assert response.status_code == 201, response.text
    return {
        "project_id": project_id,
        "root_question_id": root_question_id,
        "child_question_id": child_question_id,
        "dataset_id": dataset_id,
        "analysis_id": analysis_id,
        "claim_id": claim_id,
        "viz_id": viz_id,
        "goal_id": goal_id,
        "run_uri": run_pointer["uri"],
    }


def test_record_export_returns_scoped_dump_and_provenance_for_user_and_group(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    source_headers, source_user_id = _register_user(client, role=Role.ADMIN)
    group_owner_headers, group_owner_id = _register_user(client, role=Role.VIEWER)
    viewer_headers, _ = _register_user(client, role=Role.VIEWER)

    group_response = client.post(
        "/groups",
        json={"name": "Export lab group"},
        headers=admin_auth_headers,
    )
    assert group_response.status_code == 201
    group_id = group_response.json()["data"]["group_id"]
    owner_response = client.post(
        f"/groups/{group_id}/members",
        json={"user_id": group_owner_id, "role": "owner"},
        headers=admin_auth_headers,
    )
    assert owner_response.status_code == 201

    group_records = _create_project_bundle(
        client,
        source_headers,
        project_name="Grouped export",
        group_id=group_id,
    )
    outside_records = _create_project_bundle(
        client,
        source_headers,
        project_name="Outside export",
    )

    global_denied = client.post(
        f"/record-exports/users/{source_user_id}",
        headers=group_owner_headers,
    )
    group_denied = client.post(
        f"/groups/{group_id}/record-exports/users/{source_user_id}",
        headers=viewer_headers,
    )
    assert global_denied.status_code == 401
    assert group_denied.status_code == 401

    unsafe_read = client.get(
        f"/record-exports/users/{source_user_id}",
        headers=admin_auth_headers,
    )
    assert unsafe_read.status_code == 405
    assert _record_export_event_count(client) == 0

    global_export = client.post(
        f"/record-exports/users/{source_user_id}",
        headers=admin_auth_headers,
    )
    assert global_export.status_code == 200, global_export.text
    global_payload = global_export.json()["data"]
    assert global_payload["user_id"] == source_user_id
    assert global_payload["group_id"] is None
    assert {item["question_id"] for item in global_payload["records"]["questions"]} == {
        group_records["question_id"],
        outside_records["question_id"],
    }
    assert {item["dataset_id"] for item in global_payload["records"]["datasets"]} == {
        group_records["dataset_id"],
        outside_records["dataset_id"],
    }
    assert {item["session_id"] for item in global_payload["records"]["sessions"]} == {
        group_records["session_id"],
        outside_records["session_id"],
    }
    assert {item["analysis_id"] for item in global_payload["records"]["analyses"]} == {
        group_records["analysis_id"],
        outside_records["analysis_id"],
    }
    assert {item["claim_id"] for item in global_payload["records"]["claims"]} == {
        group_records["claim_id"],
        outside_records["claim_id"],
    }
    assert {item["note_id"] for item in global_payload["records"]["notes"]} == {
        group_records["note_id"],
        outside_records["note_id"],
    }
    assert {item["viz_id"] for item in global_payload["records"]["visualizations"]} == {
        group_records["viz_id"],
        outside_records["viz_id"],
    }

    graph_ids = _graph_ids(global_payload)
    assert f"http://testserver/agents/{source_user_id}" in graph_ids
    assert f"http://testserver/datasets/{group_records['dataset_id']}" in graph_ids
    assert f"http://testserver/sessions/{group_records['session_id']}" in graph_ids
    assert f"http://testserver/analyses/{outside_records['analysis_id']}" in graph_ids
    assert f"http://testserver/claims/{group_records['claim_id']}" in graph_ids
    assert f"http://testserver/notes/{outside_records['note_id']}" in graph_ids
    assert f"http://testserver/visualizations/{outside_records['viz_id']}" in graph_ids

    group_export = client.post(
        f"/groups/{group_id}/record-exports/users/{source_user_id}",
        headers=group_owner_headers,
    )
    assert group_export.status_code == 200, group_export.text
    group_payload = group_export.json()["data"]
    assert group_payload["user_id"] == source_user_id
    assert group_payload["group_id"] == group_id
    assert group_payload["project_ids"] == [group_records["project_id"]]
    assert [item["question_id"] for item in group_payload["records"]["questions"]] == [
        group_records["question_id"]
    ]
    assert [item["dataset_id"] for item in group_payload["records"]["datasets"]] == [
        group_records["dataset_id"]
    ]
    assert [item["session_id"] for item in group_payload["records"]["sessions"]] == [
        group_records["session_id"]
    ]
    assert [item["analysis_id"] for item in group_payload["records"]["analyses"]] == [
        group_records["analysis_id"]
    ]
    assert [item["claim_id"] for item in group_payload["records"]["claims"]] == [
        group_records["claim_id"]
    ]
    assert [item["note_id"] for item in group_payload["records"]["notes"]] == [
        group_records["note_id"]
    ]
    assert [item["viz_id"] for item in group_payload["records"]["visualizations"]] == [
        group_records["viz_id"]
    ]

    group_graph_ids = _graph_ids(group_payload)
    assert f"http://testserver/datasets/{group_records['dataset_id']}" in group_graph_ids
    assert f"http://testserver/datasets/{outside_records['dataset_id']}" not in group_graph_ids
    assert f"http://testserver/sessions/{group_records['session_id']}" in group_graph_ids
    assert f"http://testserver/visualizations/{outside_records['viz_id']}" not in group_graph_ids


def test_record_export_returns_not_found_for_missing_user(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    response = client.post(
        f"/record-exports/users/{uuid4()}",
        headers=admin_auth_headers,
    )

    assert response.status_code == 404


def test_goal_ara_artifact_exposes_four_independently_retrievable_layers(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    records = _create_ara_bundle(client, admin_auth_headers)

    response = client.get(
        f"/goals/{records['goal_id']}/ara-artifact",
        headers=admin_auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/ld+json")
    artifact = response.json()

    assert artifact["@id"] == f"http://testserver/goals/{records['goal_id']}/ara-artifact"
    assert set(artifact["layers"]) == {"logic", "src", "trace", "evidence"}
    for layer_name in ("logic", "src", "trace", "evidence"):
        layer = artifact["layers"][layer_name]
        assert layer["@id"] == (
            f"http://testserver/goals/{records['goal_id']}/ara-artifact/{layer_name}"
        )
        layer_response = client.get(
            f"/goals/{records['goal_id']}/ara-artifact/{layer_name}",
            headers=admin_auth_headers,
        )
        assert layer_response.status_code == 200, layer_response.text
        assert layer_response.headers["content-type"].startswith("application/ld+json")
        assert layer_response.json()["@id"] == layer["@id"]

    logic_ids = _layer_graph_ids(artifact["layers"]["logic"])
    src_ids = _layer_graph_ids(artifact["layers"]["src"])
    trace_ids = _layer_graph_ids(artifact["layers"]["trace"])
    evidence_ids = _layer_graph_ids(artifact["layers"]["evidence"])

    claim_iri = f"http://testserver/claims/{records['claim_id']}"
    analysis_iri = f"http://testserver/analyses/{records['analysis_id']}"
    dataset_iri = f"http://testserver/datasets/{records['dataset_id']}"
    question_iri = f"http://testserver/questions/{records['child_question_id']}"
    viz_iri = f"http://testserver/visualizations/{records['viz_id']}"

    assert claim_iri in logic_ids
    assert analysis_iri in src_ids
    assert question_iri in trace_ids
    assert dataset_iri in evidence_ids
    assert viz_iri in evidence_ids
    assert records["run_uri"] in src_ids

    binding = artifact["crossLayerBindings"][0]
    assert binding["claim"] == {"@id": claim_iri}
    assert binding["analysis"] == [{"@id": analysis_iri}]
    assert binding["dataset"] == [{"@id": dataset_iri}]
    assert {"@id": question_iri} in binding["question"]
    assert {"@id": viz_iri} in binding["evidence"]
    assert binding["codeEnvironment"] == [
        {
            "@id": analysis_iri,
            "codeVersion": "git:abc123",
            "methodHash": "method-fit",
            "environmentHash": "uv:lock123",
        }
    ]


def test_opaque_ara_target_misses_do_not_record_usage_events(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    records = _create_ara_bundle(client, admin_auth_headers)
    outsider_headers, _ = _register_user(client, role=Role.VIEWER)
    missing_goal_id = str(uuid4())
    missing_question_id = str(uuid4())
    client.app.state.settings.usage_events = True

    cases = (
        (
            f"/goals/{records['goal_id']}/ara-artifact",
            f"/goals/{missing_goal_id}/ara-artifact",
            "Goal",
        ),
        (
            f"/goals/{records['goal_id']}/ara-artifact/src",
            f"/goals/{missing_goal_id}/ara-artifact/src",
            "Goal",
        ),
        (
            f"/questions/{records['root_question_id']}/ara-artifact",
            f"/questions/{missing_question_id}/ara-artifact",
            "Question",
        ),
        (
            f"/questions/{records['root_question_id']}/ara-artifact/src",
            f"/questions/{missing_question_id}/ara-artifact/src",
            "Question",
        ),
    )
    for existing_path, missing_path, entity_name in cases:
        existing = client.get(existing_path, headers=outsider_headers)
        missing = client.get(missing_path, headers=outsider_headers)

        assert existing.status_code == missing.status_code == 404
        assert existing.json() == missing.json() == {
            "error": {
                "code": "not_found",
                "message": f"{entity_name} does not exist.",
                "issues": None,
            }
        }

    assert _usage_events(client) == []


def test_outsider_dangling_goal_link_is_opaque_without_usage_events(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_response = client.post(
        "/projects",
        json={"name": "Dangling Ara target project"},
        headers=admin_auth_headers,
    )
    assert project_response.status_code == 201, project_response.text
    project_id = project_response.json()["data"]["project_id"]
    question_response = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Question deleted after goal linking",
            "question_type": "descriptive",
        },
        headers=admin_auth_headers,
    )
    assert question_response.status_code == 201, question_response.text
    question_id = question_response.json()["data"]["question_id"]
    goal_response = client.post(
        f"/projects/{project_id}/goals",
        json={"goal_type": "paper", "title": "Goal with dangling question"},
        headers=admin_auth_headers,
    )
    assert goal_response.status_code == 201, goal_response.text
    goal_id = goal_response.json()["data"]["goal_id"]
    link_response = client.post(
        f"/goals/{goal_id}/links",
        json={
            "entity_type": "question",
            "entity_id": question_id,
            "relation": "addresses",
        },
        headers=admin_auth_headers,
    )
    assert link_response.status_code == 201, link_response.text
    link_id = link_response.json()["data"]["link_id"]
    outsider_headers, _ = _register_user(client, role=Role.VIEWER)

    with client.app.state.db_session_factory() as session:
        question_row = session.get(QuestionModel, question_id)
        assert question_row is not None
        session.delete(question_row)
        session.commit()
        assert session.get(GoalLinkModel, link_id) is not None

    client.app.state.settings.usage_events = True
    missing_goal_id = str(uuid4())
    for suffix in ("", "/src"):
        existing = client.get(
            f"/goals/{goal_id}/ara-artifact{suffix}",
            headers=outsider_headers,
        )
        missing = client.get(
            f"/goals/{missing_goal_id}/ara-artifact{suffix}",
            headers=outsider_headers,
        )

        assert existing.status_code == missing.status_code == 404
        assert existing.json() == missing.json() == {
            "error": {
                "code": "not_found",
                "message": "Goal does not exist.",
                "issues": None,
            }
        }

    assert _usage_events(client) == []


@pytest.mark.parametrize(
    ("entity_type", "resource_id_key", "path_prefix", "suffix"),
    [
        pytest.param("goal", "goal_id", "goals", "", id="goal-whole"),
        pytest.param("goal", "goal_id", "goals", "/src", id="goal-layer"),
        pytest.param(
            "question",
            "root_question_id",
            "questions",
            "",
            id="question-whole",
        ),
        pytest.param(
            "question",
            "root_question_id",
            "questions",
            "/src",
            id="question-layer",
        ),
    ],
)
def test_authorized_ara_export_records_one_attributed_usage_event(
    client: TestClient,
    entity_type: str,
    resource_id_key: str,
    path_prefix: str,
    suffix: str,
) -> None:
    admin_headers, admin_user_id = _register_user(client, role=Role.ADMIN)
    records = _create_ara_bundle(client, admin_headers)
    client.app.state.settings.usage_events = True

    response = client.get(
        f"/{path_prefix}/{records[resource_id_key]}/ara-artifact{suffix}",
        headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    _assert_single_ara_usage_event(
        client,
        resource_type=entity_type,
        resource_id=records[resource_id_key],
        outcome="ok",
        actor_user_id=admin_user_id,
    )


def test_spanning_goal_ara_artifact_is_opaque_until_full_scope_is_authorized(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_a = client.post(
        "/projects",
        json={"name": "Ara spanning A"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    project_b = client.post(
        "/projects",
        json={"name": "Ara spanning B"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    goal = client.post(
        "/goals",
        json={
            "project_id": None,
            "goal_type": "paper",
            "title": "Ara spanning goal",
            "links": [
                {
                    "entity_type": "project",
                    "entity_id": project_a,
                    "relation": "contributes_to",
                },
                {
                    "entity_type": "project",
                    "entity_id": project_b,
                    "relation": "contributes_to",
                },
            ],
        },
        headers=admin_auth_headers,
    )
    assert goal.status_code == 201, goal.text
    goal_id = goal.json()["data"]["goal_id"]
    missing_goal_id = str(uuid4())
    viewer_headers, viewer_user_id = _register_user(client, role=Role.VIEWER)

    for suffix in ("", "/src"):
        existing = client.get(
            f"/goals/{goal_id}/ara-artifact{suffix}",
            headers=viewer_headers,
        )
        missing = client.get(
            f"/goals/{missing_goal_id}/ara-artifact{suffix}",
            headers=viewer_headers,
        )
        assert existing.status_code == missing.status_code == 404
        assert existing.json() == missing.json()
        assert existing.json()["error"] == {
            "code": "not_found",
            "message": "Goal does not exist.",
            "issues": None,
        }

    membership = client.post(
        f"/projects/{project_a}/members",
        json={"user_id": viewer_user_id, "role": "viewer"},
        headers=admin_auth_headers,
    )
    assert membership.status_code == 201, membership.text

    partial = client.get(
        f"/goals/{goal_id}/ara-artifact/evidence",
        headers=viewer_headers,
    )
    partial_missing = client.get(
        f"/goals/{missing_goal_id}/ara-artifact/evidence",
        headers=viewer_headers,
    )
    assert partial.status_code == partial_missing.status_code == 404
    assert partial.json() == partial_missing.json()

    invalid_existing = client.get(
        f"/goals/{goal_id}/ara-artifact/not-a-layer",
        headers=viewer_headers,
    )
    invalid_missing = client.get(
        f"/goals/{missing_goal_id}/ara-artifact/not-a-layer",
        headers=viewer_headers,
    )
    assert invalid_existing.status_code == invalid_missing.status_code == 422
    assert invalid_existing.json() == invalid_missing.json()

    membership = client.post(
        f"/projects/{project_b}/members",
        json={"user_id": viewer_user_id, "role": "viewer"},
        headers=admin_auth_headers,
    )
    assert membership.status_code == 201, membership.text

    artifact = client.get(
        f"/goals/{goal_id}/ara-artifact",
        headers=viewer_headers,
    )
    layer = client.get(
        f"/goals/{goal_id}/ara-artifact/src",
        headers=viewer_headers,
    )
    assert artifact.status_code == layer.status_code == 200
    assert artifact.headers["content-type"].startswith("application/ld+json")
    assert layer.headers["content-type"].startswith("application/ld+json")


def test_goal_ara_artifact_hides_target_deleted_after_authorization(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch,
):
    records = _create_ara_bundle(client, admin_auth_headers)
    client.app.state.settings.usage_events = True
    missing = client.get(
        f"/goals/{uuid4()}/ara-artifact",
        headers=admin_auth_headers,
    )
    collection_started = False

    def target_deleted_after_authorization(
        service,
        goal,
        scope_project_ids,
    ):
        nonlocal collection_started
        collection_started = True
        del service, goal, scope_project_ids
        raise NotFoundError("Question does not exist.")

    monkeypatch.setattr(
        RecordExportService,
        "_collect_goal_artifact_records",
        target_deleted_after_authorization,
    )
    existing = client.get(
        f"/goals/{records['goal_id']}/ara-artifact",
        headers=admin_auth_headers,
    )
    assert collection_started is True

    assert existing.status_code == missing.status_code == 404
    assert existing.json() == missing.json()
    assert existing.json()["error"] == {
        "code": "not_found",
        "message": "Goal does not exist.",
        "issues": None,
    }
    _assert_single_ara_usage_event(
        client,
        resource_type="goal",
        resource_id=records["goal_id"],
        outcome="error",
    )


def test_goal_ara_scope_failure_remains_canonical_and_records_error_usage(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch,
) -> None:
    records = _create_ara_bundle(client, admin_auth_headers)
    client.app.state.settings.usage_events = True

    def linked_target_deleted(
        service,
        goal,
        *,
        actor=None,
    ):
        del service, goal, actor
        raise NotFoundError("Question does not exist.")

    monkeypatch.setattr(GoalService, "_require_goal_read", linked_target_deleted)

    response = client.get(
        f"/goals/{records['goal_id']}/ara-artifact",
        headers=admin_auth_headers,
    )

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "not_found",
            "message": "Goal does not exist.",
            "issues": None,
        }
    }
    _assert_single_ara_usage_event(
        client,
        resource_type="goal",
        resource_id=records["goal_id"],
        outcome="error",
    )


def test_question_subtree_ara_artifact_scopes_to_descendants(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    records = _create_ara_bundle(client, admin_auth_headers)

    response = client.get(
        f"/questions/{records['root_question_id']}/ara-artifact/evidence",
        headers=admin_auth_headers,
    )
    assert response.status_code == 200, response.text
    layer = response.json()

    assert layer["@id"] == (
        f"http://testserver/questions/{records['root_question_id']}/ara-artifact/evidence"
    )
    graph_ids = _layer_graph_ids(layer)
    assert f"http://testserver/datasets/{records['dataset_id']}" in graph_ids
    assert f"http://testserver/visualizations/{records['viz_id']}" in graph_ids
    assert layer["crossLayerBindings"][0]["claim"] == {
        "@id": f"http://testserver/claims/{records['claim_id']}"
    }


def test_question_subtree_ara_authorizes_before_collecting_records(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch,
):
    records = _create_ara_bundle(client, admin_auth_headers)
    viewer_headers, _ = _register_user(client, role=Role.VIEWER)
    missing_question_id = uuid4()
    collection_calls: list[object] = []

    def fail_if_collected(service, root):  # noqa: ANN001
        collection_calls.append((service, root))
        raise AssertionError("question records must not be collected before authorization")

    monkeypatch.setattr(
        RecordExportService,
        "_collect_question_subtree_records",
        fail_if_collected,
    )

    existing = client.get(
        f"/questions/{records['root_question_id']}/ara-artifact",
        headers=viewer_headers,
    )
    missing = client.get(
        f"/questions/{missing_question_id}/ara-artifact",
        headers=viewer_headers,
    )

    assert existing.status_code == missing.status_code == 404
    assert existing.json() == missing.json() == {
        "error": {
            "code": "not_found",
            "message": "Question does not exist.",
            "issues": None,
        }
    }
    assert collection_calls == []


def test_question_subtree_ara_preserves_descendant_not_found_error(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch,
):
    records = _create_ara_bundle(client, admin_auth_headers)
    client.app.state.settings.usage_events = True

    def descendant_deleted(service, root):  # noqa: ANN001
        del service, root
        raise NotFoundError("Dataset does not exist.")

    monkeypatch.setattr(
        RecordExportService,
        "_collect_question_subtree_records",
        descendant_deleted,
    )

    response = client.get(
        f"/questions/{records['root_question_id']}/ara-artifact",
        headers=admin_auth_headers,
    )

    assert response.status_code == 404
    assert response.json()["error"] == {
        "code": "not_found",
        "message": "Dataset does not exist.",
        "issues": None,
    }
    _assert_single_ara_usage_event(
        client,
        resource_type="question",
        resource_id=records["root_question_id"],
        outcome="error",
    )
