from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from lab_tracker.auth import Role
from lab_tracker.models import Question, QuestionStatus, QuestionType
from lab_tracker.project_graph import build_project_graph


def _ids(items: list[dict[str, object]]) -> set[str]:
    return {str(item["id"]) for item in items}


def _edge_ids(items: list[dict[str, object]]) -> set[str]:
    return {str(item["id"]) for item in items}


class _QuestionsOnlyGraphRepository:
    def __init__(self, question: Question) -> None:
        self.question = question
        self.calls: list[str] = []

    def query_questions(self, **_):
        self.calls.append("questions")
        return [self.question], 1

    def query_datasets(self, **_):
        raise AssertionError("questions view should not query datasets")

    def query_analyses(self, **_):
        raise AssertionError("questions view should not query analyses")

    def query_claims(self, **_):
        raise AssertionError("questions view should not query claims")

    def query_claim_edges(self, **_):
        raise AssertionError("questions view should not query claim edges")

    def query_exploration_nodes(self, **_):
        raise AssertionError("questions view should not query exploration nodes")

    def query_visualizations(self, **_):
        raise AssertionError("questions view should not query visualizations")

    def query_goals(self, **_):
        raise AssertionError("questions view should not query goals")

    def query_notes(self, **_):
        raise AssertionError("questions view should not query notes")

    def query_sessions(self, **_):
        raise AssertionError("questions view should not query sessions")


def _auth_headers(client: TestClient, *, role: Role = Role.VIEWER) -> dict[str, str]:
    username = f"graph-user-{uuid4().hex[:8]}"
    password = "secret"
    client.app.state.auth_service.register_user(
        username=username,
        password=password,
        role=role,
    )
    login = client.post("/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['data']['access_token']}"}


def _create_graph_fixture(
    client: TestClient,
    headers: dict[str, str],
) -> dict[str, str]:
    project_id = client.post(
        "/projects",
        json={"name": "Project graph"},
        headers=headers,
    ).json()["data"]["project_id"]
    root_question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": 'Can "escape" survive?\nYes',
            "question_type": "descriptive",
            "status": "active",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    child_question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "What evidence is linked?",
            "question_type": "hypothesis_driven",
            "status": "active",
            "parent_question_ids": [root_question_id],
        },
        headers=headers,
    ).json()["data"]["question_id"]
    refactor = client.post(
        f"/questions/{root_question_id}/refactor",
        json={
            "replacement": {
                "text": "Can escaped labels be rendered?",
                "question_type": "descriptive",
                "status": "active",
            },
            "child_question_ids_to_reparent": [child_question_id],
            "reason": "Tighter wording.",
        },
        headers=headers,
    ).json()["data"]
    replacement_question_id = refactor["replacement_question"]["question_id"]
    scientific_session_id = client.post(
        "/sessions",
        json={
            "project_id": project_id,
            "session_type": "scientific",
            "primary_question_id": child_question_id,
        },
        headers=headers,
    ).json()["data"]["session_id"]
    source_session_id = client.post(
        "/sessions",
        json={
            "project_id": project_id,
            "session_type": "operational",
        },
        headers=headers,
    ).json()["data"]["session_id"]
    note_id = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": "Figure note",
            "targets": [{"entity_type": "question", "entity_id": child_question_id}],
        },
        headers=headers,
    ).json()["data"]["note_id"]
    dataset_id = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": child_question_id,
            "secondary_question_ids": [replacement_question_id],
            "status": "committed",
                "commit_manifest": {
                    "files": [{"path": "raw/data.csv", "checksum": "abc"}],
                    "source_session_id": source_session_id,
                },
            },
            headers=headers,
        ).json()["data"]["dataset_id"]
    analysis_id = client.post(
        "/analyses",
        json={
            "project_id": project_id,
            "dataset_ids": [dataset_id],
            "method_hash": "method-1",
            "code_version": "code-1",
            "status": "committed",
        },
        headers=headers,
    ).json()["data"]["analysis_id"]
    claim_id = client.post(
        "/claims",
        json={
            "project_id": project_id,
            "statement": "Evidence supports the claim.",
            "confidence": 77,
            "status": "supported",
            "supported_by_dataset_ids": [dataset_id],
            "supported_by_analysis_ids": [analysis_id],
            "answers_question_ids": [child_question_id],
        },
        headers=headers,
    ).json()["data"]["claim_id"]
    decision_node_id = client.post(
        "/exploration-nodes",
        json={
            "project_id": project_id,
            "node_type": "decision",
            "title": "Trust the linked evidence chain",
            "target": {"entity_type": "claim", "entity_id": claim_id},
            "choice": "Use the committed analysis",
            "alternatives_considered": ["Wait for more data"],
            "rationale": "The dataset and analysis are already linked.",
        },
        headers=headers,
    ).json()["data"]["node_id"]
    dead_end_node_id = client.post(
        "/exploration-nodes",
        json={
            "project_id": project_id,
            "node_type": "dead_end",
            "title": "Discard unlinked side analysis",
            "target": {"entity_type": "dataset", "entity_id": dataset_id},
            "evidence_refs": [{"entity_type": "claim", "entity_id": claim_id}],
            "hypothesis": "The side analysis would strengthen the claim.",
            "failure_mode": "It did not reuse the committed dataset.",
            "lesson": "Keep the graph spine intact before interpreting figures.",
            "parent_node_ids": [decision_node_id],
        },
        headers=headers,
    ).json()["data"]["node_id"]
    pivot_node_id = client.post(
        "/exploration-nodes",
        json={
            "project_id": project_id,
            "node_type": "pivot",
            "title": "Pivot back to the linked analysis",
            "target": {"entity_type": "claim", "entity_id": claim_id},
            "trigger": "Dead-end side analysis",
            "rationale": "The retained graph has a complete support path.",
            "invalidates_claim_id": claim_id,
            "parent_node_ids": [dead_end_node_id],
            "also_depends_on_node_ids": [decision_node_id],
        },
        headers=headers,
    ).json()["data"]["node_id"]
    viz_id = client.post(
        "/visualizations",
        json={
            "analysis_id": analysis_id,
            "viz_type": "line plot",
            "file_path": "figures/plot.png",
            "caption": "Main figure",
            "related_claim_ids": [claim_id],
        },
        headers=headers,
    ).json()["data"]["viz_id"]
    goal_id = client.post(
        f"/projects/{project_id}/goals",
        json={
            "goal_type": "paper",
            "title": "Graph paper",
            "attributes": {"target_venue": "Neuron"},
        },
        headers=headers,
    ).json()["data"]["goal_id"]
    client.post(
        f"/goals/{goal_id}/links",
        json={
            "entity_type": "visualization",
            "entity_id": viz_id,
            "relation": "candidate_figure",
            "slot": "Figure 3",
        },
        headers=headers,
    )
    return {
        "analysis_id": analysis_id,
        "child_question_id": child_question_id,
        "claim_id": claim_id,
        "dataset_id": dataset_id,
        "dead_end_node_id": dead_end_node_id,
        "decision_node_id": decision_node_id,
        "goal_id": goal_id,
        "note_id": note_id,
        "project_id": project_id,
        "pivot_node_id": pivot_node_id,
        "replacement_question_id": replacement_question_id,
        "root_question_id": root_question_id,
        "scientific_session_id": scientific_session_id,
        "source_session_id": source_session_id,
        "viz_id": viz_id,
    }


def test_project_graph_questions_view_only_queries_questions():
    project_id = uuid4()
    question = Question(
        question_id=uuid4(),
        project_id=project_id,
        text="Which question edges are needed?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
    )
    repository = _QuestionsOnlyGraphRepository(question)

    graph = build_project_graph(repository, project_id, view="questions")

    assert repository.calls == ["questions"]
    assert [node.id for node in graph.nodes] == [f"question:{question.question_id}"]
    assert graph.edges == []


def test_project_graph_questions_view_contains_question_edges(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    ids = _create_graph_fixture(client, admin_auth_headers)

    response = client.get(
        f"/projects/{ids['project_id']}/graph?view=questions",
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["project_id"] == ids["project_id"]
    assert payload["view"] == "questions"
    assert _ids(payload["nodes"]) == {
        f"question:{ids['child_question_id']}",
        f"question:{ids['replacement_question_id']}",
        f"question:{ids['root_question_id']}",
    }
    assert _edge_ids(payload["edges"]) == {
        (
            "question_parent:"
            f"question:{ids['replacement_question_id']}->question:{ids['child_question_id']}"
        ),
        (
            "question_superseded_by:"
            f"question:{ids['root_question_id']}->question:{ids['replacement_question_id']}"
        ),
        (
            "question_supersedes:"
            f"question:{ids['replacement_question_id']}->question:{ids['root_question_id']}"
        ),
    }


def test_project_graph_evidence_and_full_views_include_expected_links(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    ids = _create_graph_fixture(client, admin_auth_headers)

    evidence = client.get(
        f"/projects/{ids['project_id']}/graph",
        headers=admin_auth_headers,
    ).json()["data"]
    full = client.get(
        f"/projects/{ids['project_id']}/graph?view=full",
        headers=admin_auth_headers,
    ).json()["data"]

    assert evidence["view"] == "evidence"
    assert f"note:{ids['note_id']}" not in _ids(evidence["nodes"])
    assert f"session:{ids['scientific_session_id']}" not in _ids(evidence["nodes"])
    assert f"session:{ids['source_session_id']}" not in _ids(evidence["nodes"])
    assert {
        (
            "dataset_question_primary:"
            f"question:{ids['child_question_id']}->dataset:{ids['dataset_id']}"
        ),
        (
            "dataset_question_secondary:"
            f"question:{ids['replacement_question_id']}->dataset:{ids['dataset_id']}"
        ),
        f"analysis_dataset:dataset:{ids['dataset_id']}->analysis:{ids['analysis_id']}",
        f"claim_dataset_support:dataset:{ids['dataset_id']}->claim:{ids['claim_id']}",
        f"claim_analysis_support:analysis:{ids['analysis_id']}->claim:{ids['claim_id']}",
        f"claim_question_answers:claim:{ids['claim_id']}->question:{ids['child_question_id']}",
        (
            "exploration_target:"
            f"claim:{ids['claim_id']}->exploration_node:{ids['decision_node_id']}"
        ),
        (
            "exploration_evidence:"
            f"claim:{ids['claim_id']}->exploration_node:{ids['dead_end_node_id']}"
        ),
        (
            "exploration_parent:"
            f"exploration_node:{ids['decision_node_id']}"
            f"->exploration_node:{ids['dead_end_node_id']}"
        ),
        (
            "exploration_dependency:"
            f"exploration_node:{ids['decision_node_id']}"
            f"->exploration_node:{ids['pivot_node_id']}"
        ),
        (
            "exploration_invalidates_claim:"
            f"exploration_node:{ids['pivot_node_id']}->claim:{ids['claim_id']}"
        ),
        f"visualization_analysis:analysis:{ids['analysis_id']}->visualization:{ids['viz_id']}",
        f"visualization_dataset:dataset:{ids['dataset_id']}->visualization:{ids['viz_id']}",
        f"visualization_claim:claim:{ids['claim_id']}->visualization:{ids['viz_id']}",
        f"goal_candidate_figure_candidate:visualization:{ids['viz_id']}->goal:{ids['goal_id']}",
    }.issubset(_edge_ids(evidence["edges"]))

    assert f"goal:{ids['goal_id']}" in _ids(evidence["nodes"])
    assert f"exploration_node:{ids['decision_node_id']}" in _ids(evidence["nodes"])
    assert f"note:{ids['note_id']}" in _ids(full["nodes"])
    assert f"session:{ids['scientific_session_id']}" in _ids(full["nodes"])
    assert f"session:{ids['source_session_id']}" in _ids(full["nodes"])
    assert {
        f"note_target_question:note:{ids['note_id']}->question:{ids['child_question_id']}",
        (
            "session_question:"
            f"question:{ids['child_question_id']}->session:{ids['scientific_session_id']}"
        ),
        (
            "dataset_source_session:"
            f"session:{ids['source_session_id']}->dataset:{ids['dataset_id']}"
        ),
    }.issubset(_edge_ids(full["edges"]))


def test_project_graph_visualization_nodes_include_managed_image_asset_metadata(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    ids = _create_graph_fixture(client, admin_auth_headers)
    upload = client.post(
        f"/visualizations/{ids['viz_id']}/file",
        files={"file": ("figure-thumb.png", b"\x89PNG\r\n\x1a\nfigure", "image/png")},
        headers=admin_auth_headers,
    )
    assert upload.status_code == 201

    response = client.get(
        f"/projects/{ids['project_id']}/graph",
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    nodes = {item["id"]: item for item in response.json()["data"]["nodes"]}
    metadata = nodes[f"visualization:{ids['viz_id']}"]["metadata"]
    assert metadata["asset_download_path"] == f"/visualizations/{ids['viz_id']}/file/download"
    assert metadata["asset"] == {
        "checksum": upload.json()["data"]["asset"]["checksum"],
        "content_type": "image/png",
        "filename": "figure-thumb.png",
    }


def test_project_graph_full_view_truncates_long_note_and_claim_labels(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = client.post(
        "/projects",
        json={"name": "Bound graph labels"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    transcript = "Transcript " + ("long text " * 30)
    note_id = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": "raw seed",
            "transcribed_text": transcript,
        },
        headers=admin_auth_headers,
    ).json()["data"]["note_id"]
    claim_statement = "Claim " + ("long statement " * 30)
    claim_id = client.post(
        "/claims",
        json={
            "project_id": project_id,
            "statement": claim_statement,
            "confidence": 75,
        },
        headers=admin_auth_headers,
    ).json()["data"]["claim_id"]

    response = client.get(
        f"/projects/{project_id}/graph?view=full",
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    nodes = {item["id"]: item for item in response.json()["data"]["nodes"]}
    assert nodes[f"note:{note_id}"]["label"] == transcript[:180]
    assert len(nodes[f"note:{note_id}"]["label"]) == 180
    assert nodes[f"claim:{claim_id}"]["label"] == claim_statement[:180]
    assert len(nodes[f"claim:{claim_id}"]["label"]) == 180


def test_project_graph_includes_claim_relations_and_external_citations(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = client.post(
        "/projects",
        json={"name": "Claim logic graph"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    citation = {
        "source_system": "doi",
        "uri": "doi:10.1101/example-preprint",
        "content_hash": "sha256:paper",
    }
    source_id = client.post(
        "/claims",
        json={
            "project_id": project_id,
            "statement": "Perturbation reduces activity.",
            "confidence": 80,
            "external_citations": [citation],
        },
        headers=admin_auth_headers,
    ).json()["data"]["claim_id"]
    target_id = client.post(
        "/claims",
        json={
            "project_id": project_id,
            "statement": "Perturbation does not affect activity.",
            "confidence": 25,
        },
        headers=admin_auth_headers,
    ).json()["data"]["claim_id"]

    edge_response = client.post(
        f"/claims/{source_id}/edges",
        json={"target_claim_id": target_id, "relation": "refutes"},
        headers=admin_auth_headers,
    )
    assert edge_response.status_code == 201
    listed_edges = client.get(
        f"/claims/{source_id}/edges",
        headers=admin_auth_headers,
    ).json()["data"]
    assert [item["relation"] for item in listed_edges] == ["refutes"]

    graph = client.get(
        f"/projects/{project_id}/graph",
        headers=admin_auth_headers,
    ).json()["data"]

    assert f"external_artifact:{citation['uri']}" in _ids(graph["nodes"])
    assert {
        f"claim_relation_refutes:claim:{source_id}->claim:{target_id}",
        f"claim_cites:claim:{source_id}->external_artifact:{citation['uri']}",
    }.issubset(_edge_ids(graph["edges"]))


def test_project_graph_mermaid_export_is_stable_and_escaped(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    ids = _create_graph_fixture(client, admin_auth_headers)

    response = client.get(
        f"/projects/{ids['project_id']}/graph/mermaid?view=questions",
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/vnd.mermaid")
    first = response.text
    second = client.get(
        f"/projects/{ids['project_id']}/graph/mermaid?view=questions",
        headers=admin_auth_headers,
    ).text
    assert first == second
    assert first.startswith("graph LR\n")
    assert 'Can \\"escape\\" survive? Yes' in first
    assert "\nYes" not in first


def test_project_graph_routes_require_project_access(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    ids = _create_graph_fixture(client, admin_auth_headers)
    viewer_headers = _auth_headers(client, role=Role.VIEWER)

    graph_response = client.get(
        f"/projects/{ids['project_id']}/graph",
        headers=viewer_headers,
    )
    mermaid_response = client.get(
        f"/projects/{ids['project_id']}/graph/mermaid",
        headers=viewer_headers,
    )
    unauthenticated_response = client.get(f"/projects/{ids['project_id']}/graph")

    assert graph_response.status_code == 401
    assert mermaid_response.status_code == 401
    assert unauthenticated_response.status_code == 401
