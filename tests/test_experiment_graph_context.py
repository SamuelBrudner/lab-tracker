from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from lab_tracker.graph_drafting import GraphDraftingError
from lab_tracker.models import (
    EntityType,
    GraphChangeOp,
    GraphChangeSet,
    GraphDraftSemanticType,
)
from lab_tracker.services.graph_draft_validation import GraphPatchValidator


def _create_experiment_context(
    client: TestClient,
    headers: dict[str, str],
) -> dict[str, str]:
    project = client.post(
        "/projects",
        json={"name": "Experiment graph context"},
        headers=headers,
    )
    assert project.status_code == 201
    project_id = project.json()["data"]["project_id"]
    question = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Do repeated odor trials remain stable?",
            "question_type": "hypothesis_driven",
            "status": "active",
        },
        headers=headers,
    )
    assert question.status_code == 201
    question_id = question.json()["data"]["question_id"]
    experiment = client.post(
        "/experiments",
        json={
            "project_id": project_id,
            "name": "Olfactory stability trials",
            "description": "A thousand trial files treated as one run.",
            "primary_question_id": question_id,
        },
        headers=headers,
    )
    assert experiment.status_code == 201
    experiment_id = experiment.json()["data"]["experiment_id"]
    session = client.post(
        "/sessions",
        json={
            "project_id": project_id,
            "session_type": "scientific",
            "primary_question_id": question_id,
        },
        headers=headers,
    )
    assert session.status_code == 201
    session_id = session.json()["data"]["session_id"]
    dataset = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": question_id,
            "status": "staged",
        },
        headers=headers,
    )
    assert dataset.status_code == 201
    dataset_id = dataset.json()["data"]["dataset_id"]
    assert client.put(
        f"/experiments/{experiment_id}/sessions/{session_id}",
        headers=headers,
    ).status_code == 200
    assert client.put(
        f"/experiments/{experiment_id}/datasets/{dataset_id}",
        headers=headers,
    ).status_code == 200
    return {
        "project_id": project_id,
        "question_id": question_id,
        "experiment_id": experiment_id,
        "session_id": session_id,
        "dataset_id": dataset_id,
    }


def test_experiment_round_trips_across_targets_search_context_and_project_graph(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    ids = _create_experiment_context(client, admin_auth_headers)
    experiment_ref = {
        "entity_type": "experiment",
        "entity_id": ids["experiment_id"],
    }

    note = client.post(
        "/notes",
        json={
            "project_id": ids["project_id"],
            "raw_content": "The olfactory run finished cleanly.",
            "targets": [experiment_ref],
        },
        headers=admin_auth_headers,
    )
    assert note.status_code == 201
    assert note.json()["data"]["targets"] == [experiment_ref]

    goal = client.post(
        f"/projects/{ids['project_id']}/goals",
        json={"goal_type": "paper", "title": "Stability manuscript"},
        headers=admin_auth_headers,
    )
    assert goal.status_code == 201
    goal_id = goal.json()["data"]["goal_id"]
    goal_link = client.post(
        f"/goals/{goal_id}/links",
        json={
            **experiment_ref,
            "relation": "supporting_evidence",
        },
        headers=admin_auth_headers,
    )
    assert goal_link.status_code == 201

    exploration = client.post(
        "/exploration-nodes",
        json={
            "project_id": ids["project_id"],
            "node_type": "decision",
            "title": "Keep the complete run",
            "target": experiment_ref,
            "choice": "Retain the collection as one Experiment.",
            "alternatives_considered": ["Create one entity per trial."],
            "rationale": "The scientific unit is the acquisition run.",
            "evidence_refs": [experiment_ref],
        },
        headers=admin_auth_headers,
    )
    assert exploration.status_code == 201
    assert exploration.json()["data"]["target"] == experiment_ref
    assert exploration.json()["data"]["evidence_refs"] == [experiment_ref]

    notes = client.get(
        "/notes",
        params={
            "project_id": ids["project_id"],
            "target_entity_type": "experiment",
            "target_entity_id": ids["experiment_id"],
        },
        headers=admin_auth_headers,
    )
    assert notes.status_code == 200
    assert notes.json()["meta"]["total"] == 1

    reverse_goals = client.get(
        (
            f"/projects/{ids['project_id']}/nodes/experiment/"
            f"{ids['experiment_id']}/goals"
        ),
        headers=admin_auth_headers,
    )
    assert reverse_goals.status_code == 200
    assert [item["goal_id"] for item in reverse_goals.json()["data"]] == [
        goal_id
    ]

    exploration_list = client.get(
        "/exploration-nodes",
        params={
            "project_id": ids["project_id"],
            "target_entity_type": "experiment",
            "target_entity_id": ids["experiment_id"],
        },
        headers=admin_auth_headers,
    )
    assert exploration_list.status_code == 200
    assert exploration_list.json()["meta"]["total"] == 1

    search = client.get(
        "/search",
        params={
            "q": "thousand trial files",
            "project_id": ids["project_id"],
        },
        headers=admin_auth_headers,
    )
    assert search.status_code == 200
    assert [
        item["experiment_id"]
        for item in search.json()["data"]["experiments"]
    ] == [ids["experiment_id"]]

    context = client.post(
        "/assistant/decision-context",
        json={
            "task_kind": "experiment_plan",
            "query": "olfactory stability",
            "experiment_id": ids["experiment_id"],
        },
        headers=admin_auth_headers,
    )
    assert context.status_code == 200
    context_data = context.json()["data"]
    assert context_data["scope"]["anchors"] == [
        {
            "entity_type": "experiment",
            "entity_id": ids["experiment_id"],
            "label": "Olfactory stability trials",
        }
    ]
    assert context_data["experiments"][0]["experiment_id"] == ids[
        "experiment_id"
    ]
    assert context_data["task_guidance"]["candidate_outputs"][0][
        "entity_type"
    ] == "experiment"

    graph = client.get(
        f"/projects/{ids['project_id']}/graph",
        params={"view": "full"},
        headers=admin_auth_headers,
    )
    assert graph.status_code == 200
    graph_data = graph.json()["data"]
    node_ids = {node["id"] for node in graph_data["nodes"]}
    edge_ids = {edge["id"] for edge in graph_data["edges"]}
    assert f"experiment:{ids['experiment_id']}" in node_ids
    assert {
        (
            "experiment_question_primary:"
            f"question:{ids['question_id']}->experiment:{ids['experiment_id']}"
        ),
        (
            "experiment_session:"
            f"experiment:{ids['experiment_id']}->session:{ids['session_id']}"
        ),
        (
            "experiment_dataset:"
            f"experiment:{ids['experiment_id']}->dataset:{ids['dataset_id']}"
        ),
        (
            "note_target_experiment:"
            f"note:{note.json()['data']['note_id']}->experiment:{ids['experiment_id']}"
        ),
        (
            "exploration_target:"
            f"experiment:{ids['experiment_id']}"
            f"->exploration_node:{exploration.json()['data']['node_id']}"
        ),
    }.issubset(edge_ids)


def test_graph_drafts_explicitly_reject_experiment_creation() -> None:
    project_id = uuid4()
    change_set = GraphChangeSet(
        change_set_id=uuid4(),
        project_id=project_id,
        source_note_id=uuid4(),
        model="test",
        prompt_version="test",
    )
    validator = GraphPatchValidator(
        get_graph_entity=lambda entity_type, entity_id: (
            entity_type,
            entity_id,
        )
    )
    operation = {
        "client_ref": "experiment-1",
        "op": GraphChangeOp.CREATE.value,
        "entity_type": EntityType.EXPERIMENT.value,
        "semantic_type": GraphDraftSemanticType.CREATE_ENTITY.value,
        "target_entity_id": None,
        "payload_json": json.dumps(
            {
                "project_id": str(project_id),
                "name": "Agent-drafted Experiment",
                "primary_question_id": str(UUID(int=1)),
            }
        ),
        "rationale": "Must remain deferred.",
        "confidence": 0.8,
        "source_refs": [],
    }

    with pytest.raises(GraphDraftingError, match="Unsupported entity type"):
        validator.operations_from_graph_patch(
            change_set,
            {
                "summary": "deferred",
                "uncertain_fields": [],
                "clarification_requests": [],
                "operations": [operation],
            },
        )
