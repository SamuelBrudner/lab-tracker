from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from lab_tracker.auth import Role
from lab_tracker.models import (
    ClaimRelation,
    EntityType,
    GoalLinkStatus,
    GoalRelation,
    Question,
    QuestionLinkRole,
    QuestionStatus,
    QuestionType,
)
from lab_tracker.project_graph import (
    PROJECT_GRAPH_CONDITIONAL_NODE_CLASS_IRIS,
    PROJECT_GRAPH_DIRECT_RELATIONSHIP_SEMANTICS,
    PROJECT_GRAPH_NODE_CLASS_IRIS,
    PROJECT_GRAPH_PRESENTATION_ONLY_NODE_TYPES,
    PROJECT_GRAPH_QUALIFIED_RELATIONSHIP_SEMANTICS,
    PROJECT_GRAPH_SUPPRESSED_RELATIONSHIP_TOKENS,
    build_project_graph,
)
from lab_tracker.vocabulary import TERMS


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
                    "note_ids": [note_id],
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
    goal_link_response = client.post(
        f"/goals/{goal_id}/links",
        json={
            "entity_type": "visualization",
            "entity_id": viz_id,
            "relation": "candidate_figure",
            "slot": "Figure 3",
        },
        headers=headers,
    )
    assert goal_link_response.status_code == 201
    goal_link_id = goal_link_response.json()["data"]["link_id"]
    return {
        "analysis_id": analysis_id,
        "child_question_id": child_question_id,
        "claim_id": claim_id,
        "dataset_id": dataset_id,
        "dead_end_node_id": dead_end_node_id,
        "decision_node_id": decision_node_id,
        "goal_id": goal_id,
        "goal_link_id": goal_link_id,
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


def test_project_graph_node_tokens_have_explicit_semantic_classes_or_exclusions():
    assert dict(PROJECT_GRAPH_NODE_CLASS_IRIS) == {
        "question": "lab:ResearchQuestion",
        "dataset": "lab:Dataset",
        "analysis": "lab:Analysis",
        "claim": "lab:Claim",
        "exploration_node": "lab:ExplorationNode",
        "visualization": "lab:Visualization",
        "goal": "lab:Goal",
        "note": "lab:Note",
        "session": "lab:AcquisitionSession",
    }
    assert {
        token: dict(classes)
        for token, classes in PROJECT_GRAPH_CONDITIONAL_NODE_CLASS_IRIS.items()
    } == {
        "external_artifact": {
            "entity": "prov:Entity",
            "activity": "prov:Activity",
        }
    }
    assert set(PROJECT_GRAPH_NODE_CLASS_IRIS) | set(
        PROJECT_GRAPH_CONDITIONAL_NODE_CLASS_IRIS
    ) == {
        "question",
        "session",
        "note",
        "dataset",
        "analysis",
        "claim",
        "exploration_node",
        "external_artifact",
        "visualization",
        "goal",
    }
    assert set(PROJECT_GRAPH_PRESENTATION_ONLY_NODE_TYPES) == {"project"}
    assert "first-class export" in PROJECT_GRAPH_PRESENTATION_ONLY_NODE_TYPES["project"]


def test_project_graph_direct_relationship_tokens_have_exhaustive_semantic_mappings():
    expected = {
        "question_parent": ("prov:wasDerivedFrom", "target_to_source"),
        "question_superseded_by": ("prov:wasRevisionOf", "target_to_source"),
        "question_supersedes": ("prov:wasRevisionOf", "source_to_target"),
        "analysis_dataset": ("prov:used", "target_to_source"),
        "claim_dataset_support": ("lab:supportsDataset", "target_to_source"),
        "claim_analysis_support": ("lab:supportsAnalysis", "target_to_source"),
        "claim_question_answers": ("lab:answersQuestion", "source_to_target"),
        "claim_cites": ("lab:cites", "source_to_target"),
        "exploration_target": ("lab:target", "target_to_source"),
        "exploration_evidence": ("lab:evidence", "target_to_source"),
        "exploration_parent": ("prov:wasDerivedFrom", "target_to_source"),
        "exploration_dependency": ("lab:alsoDependsOn", "target_to_source"),
        "exploration_invalidates_node": ("lab:invalidates", "source_to_target"),
        "exploration_invalidates_claim": ("lab:invalidates", "source_to_target"),
        "visualization_analysis": ("prov:wasGeneratedBy", "target_to_source"),
        "visualization_dataset": ("prov:wasDerivedFrom", "target_to_source"),
        "visualization_claim": ("lab:relatedClaim", "target_to_source"),
        "session_question": ("lab:primaryQuestion", "target_to_source"),
        "dataset_source_session": ("lab:sourceSession", "target_to_source"),
        "dataset_manifest_note": ("lab:note", "target_to_source"),
        "note_was_derived_from": ("prov:wasDerivedFrom", "source_to_target"),
    }
    expected.update(
        {
            f"note_target_{entity_type.value}": ("lab:target", "source_to_target")
            for entity_type in EntityType
            if entity_type is not EntityType.PROJECT
        }
    )

    assert {
        token: (mapping.predicate_iri, mapping.direction)
        for token, mapping in PROJECT_GRAPH_DIRECT_RELATIONSHIP_SEMANTICS.items()
    } == expected
    assert set(PROJECT_GRAPH_SUPPRESSED_RELATIONSHIP_TOKENS) == {
        "note_target_project"
    }
    assert "no project node" in PROJECT_GRAPH_SUPPRESSED_RELATIONSHIP_TOKENS[
        "note_target_project"
    ]


def test_project_graph_composite_relationship_tokens_map_to_qualified_resources():
    expected: dict[
        str,
        tuple[str, str, tuple[str, ...], tuple[str, ...], str],
    ] = {}
    expected.update(
        {
            f"dataset_question_{role.value}": (
                "lab:QuestionLink",
                "target_to_source",
                (f"lab:questionLinkRole/{role.value}",),
                ("outcomeStatus",),
                "dcterms:type",
            )
            for role in QuestionLinkRole
        }
    )
    expected.update(
        {
            f"claim_relation_{relation.value}": (
                "lab:ClaimRelation",
                "source_to_target",
                (f"lab:claimRelation/{relation.value}",),
                (),
                "dcterms:type",
            )
            for relation in ClaimRelation
        }
    )
    expected.update(
        {
            f"goal_{relation.value}_{status.value}": (
                "lab:GoalLink",
                "target_to_source",
                (
                    f"lab:goalRelation/{relation.value}",
                    f"lab:goalLinkStatus/{status.value}",
                ),
                (),
                "dcterms:type",
            )
            for relation in GoalRelation
            for status in GoalLinkStatus
        }
    )

    assert not (
        set(PROJECT_GRAPH_DIRECT_RELATIONSHIP_SEMANTICS)
        & set(PROJECT_GRAPH_QUALIFIED_RELATIONSHIP_SEMANTICS)
    )
    assert {
        token: (
            mapping.class_iri,
            mapping.direction,
            mapping.concept_iris,
            mapping.additional_concept_schemes,
            mapping.classification_predicate_iri,
        )
        for token, mapping in PROJECT_GRAPH_QUALIFIED_RELATIONSHIP_SEMANTICS.items()
    } == expected


def test_project_graph_custom_semantics_share_the_public_vocabulary_registry():
    semantic_iris = set(PROJECT_GRAPH_NODE_CLASS_IRIS.values())
    semantic_iris.update(
        mapping.predicate_iri
        for mapping in PROJECT_GRAPH_DIRECT_RELATIONSHIP_SEMANTICS.values()
    )
    for mapping in PROJECT_GRAPH_QUALIFIED_RELATIONSHIP_SEMANTICS.values():
        semantic_iris.add(mapping.class_iri)
        semantic_iris.add(mapping.classification_predicate_iri)
        semantic_iris.update(mapping.concept_iris)

    registered_iris = {term.iri for term in TERMS}
    assert {iri for iri in semantic_iris if iri.startswith("lab:")} <= registered_iris
    assert "lab:Project" not in registered_iris


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
        (
            "goal_candidate_figure_candidate:"
            f"visualization:{ids['viz_id']}->goal:{ids['goal_id']}"
            f"#goal-link={ids['goal_link_id']}"
        ),
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


def test_project_graph_goal_link_edge_ids_preserve_slot_distinct_links(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    ids = _create_graph_fixture(client, admin_auth_headers)
    second_slotted = client.post(
        f"/goals/{ids['goal_id']}/links",
        json={
            "entity_type": "visualization",
            "entity_id": ids["viz_id"],
            "relation": "candidate_figure",
            "slot": "Figure 4",
        },
        headers=admin_auth_headers,
    )
    unslotted = client.post(
        f"/goals/{ids['goal_id']}/links",
        json={
            "entity_type": "visualization",
            "entity_id": ids["viz_id"],
            "relation": "candidate_figure",
        },
        headers=admin_auth_headers,
    )
    assert second_slotted.status_code == 201
    assert unslotted.status_code == 201

    first_graph = client.get(
        f"/projects/{ids['project_id']}/graph",
        headers=admin_auth_headers,
    ).json()["data"]
    second_graph = client.get(
        f"/projects/{ids['project_id']}/graph",
        headers=admin_auth_headers,
    ).json()["data"]

    assert first_graph == second_graph
    goal_edges = [
        edge
        for edge in first_graph["edges"]
        if edge["relationship"] == "goal_candidate_figure_candidate"
        and edge["source"] == f"visualization:{ids['viz_id']}"
        and edge["target"] == f"goal:{ids['goal_id']}"
    ]
    base_id = (
        "goal_candidate_figure_candidate:"
        f"visualization:{ids['viz_id']}->goal:{ids['goal_id']}"
    )
    assert _edge_ids(goal_edges) == {
        base_id,
        f"{base_id}#goal-link={ids['goal_link_id']}",
        f"{base_id}#goal-link={second_slotted.json()['data']['link_id']}",
    }
    assert {edge["label"] for edge in goal_edges} == {
        "candidate figure",
        "candidate figure: Figure 3",
        "candidate figure: Figure 4",
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
    question_text = "Question " + ("long clause " * 30)
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": question_text,
            "question_type": "descriptive",
            "status": "active",
        },
        headers=admin_auth_headers,
    ).json()["data"]["question_id"]
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
    assert nodes[f"question:{question_id}"]["label"] == question_text[:180]
    assert len(nodes[f"question:{question_id}"]["label"]) == 180


def test_project_graph_dataset_label_prefers_manifest_name_over_hash(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = client.post(
        "/projects",
        json={"name": "Dataset labels"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Does the rig record cleanly?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=admin_auth_headers,
    ).json()["data"]["question_id"]

    # commit_hash is content-addressed (computed from the manifest), so we let
    # the service assign it rather than passing one.
    def make_dataset(manifest: dict, status: str = "committed") -> str:
        response = client.post(
            "/datasets",
            json={
                "project_id": project_id,
                "primary_question_id": question_id,
                "status": status,
                "commit_manifest": manifest,
            },
            headers=admin_auth_headers,
        )
        body = response.json()
        assert "data" in body, response.text
        return body["data"]["dataset_id"]

    named_id = make_dataset(
        {
            "files": [{"path": "raw/ignored.nwb", "checksum": "c1"}],
            "metadata": {"dataset_name": "2025_12_10_Rig2_session001.nwb"},
        },
    )
    filed_id = make_dataset(
        {"files": [{"path": "sessions/2025-12-10/scan_A.tif", "checksum": "c2"}]},
    )
    # Neither a name nor files: a staged dataset (committing requires a file),
    # so the label falls back to the existing "Dataset <id>" form.
    bare_id = make_dataset({}, status="staged")

    response = client.get(
        f"/projects/{project_id}/graph?view=evidence",
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    nodes = {item["id"]: item for item in response.json()["data"]["nodes"]}
    # metadata dataset_name wins over the commit hash
    assert nodes[f"dataset:{named_id}"]["label"] == "2025_12_10_Rig2_session001.nwb"
    # else the first committed file's basename (POSIX or Windows separators)
    assert nodes[f"dataset:{filed_id}"]["label"] == "scan_A.tif"
    # else fall back to the existing hash/id-based label
    bare_label = nodes[f"dataset:{bare_id}"]["label"]
    assert bare_label.startswith("Dataset ")
    assert bare_label not in {"2025_12_10_Rig2_session001.nwb", "scan_A.tif"}
    # the commit hash stays available on the node detail for named datasets
    assert nodes[f"dataset:{named_id}"]["detail"]


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


def test_project_graph_routes_are_opaque_to_outsiders_but_require_authentication(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    ids = _create_graph_fixture(client, admin_auth_headers)
    viewer_headers = _auth_headers(client, role=Role.VIEWER)
    missing_project_id = uuid4()

    graph_response = client.get(
        f"/projects/{ids['project_id']}/graph",
        headers=viewer_headers,
    )
    mermaid_response = client.get(
        f"/projects/{ids['project_id']}/graph/mermaid",
        headers=viewer_headers,
    )
    missing_graph = client.get(
        f"/projects/{missing_project_id}/graph",
        headers=viewer_headers,
    )
    missing_mermaid = client.get(
        f"/projects/{missing_project_id}/graph/mermaid",
        headers=viewer_headers,
    )
    unauthenticated_response = client.get(f"/projects/{ids['project_id']}/graph")

    assert graph_response.status_code == missing_graph.status_code == 404
    assert mermaid_response.status_code == missing_mermaid.status_code == 404
    assert graph_response.json() == missing_graph.json() == {
        "error": {
            "code": "not_found",
            "message": "Project does not exist.",
            "issues": None,
        }
    }
    assert mermaid_response.json() == missing_mermaid.json() == graph_response.json()
    assert unauthenticated_response.status_code == 401


def test_graph_overview_is_bounded_and_counts_persisted_types(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    ids = _create_graph_fixture(client, admin_auth_headers)

    response = client.get(
        f"/projects/{ids['project_id']}/graph/overview",
        headers=admin_auth_headers,
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["project"]["project_id"] == ids["project_id"]
    assert data["counts"]["question"]["total"] == 3
    assert data["counts"]["session"]["total"] == 2
    assert data["counts"]["exploration_node"]["total"] == 3
    assert data["counts"]["visualization"] == {"total": 1, "by_status": {}}
    assert len(data["open_goals"]) <= 5
    assert len(data["open_questions"]) <= 5
    assert len(data["recent_nodes"]) <= 10


def test_graph_search_ranks_and_filters_cross_entity_hits(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    ids = _create_graph_fixture(client, admin_auth_headers)

    exact = client.get(
        f"/projects/{ids['project_id']}/graph/search",
        params={"q": "Graph paper"},
        headers=admin_auth_headers,
    )
    wildcard = client.get(
        f"/projects/{ids['project_id']}/graph/search",
        params={"q": "%_", "entity_types": "question"},
        headers=admin_auth_headers,
    )
    filtered = client.get(
        f"/projects/{ids['project_id']}/graph/search",
        params={
            "q": "Evidence",
            "entity_types": ["claim", "exploration_node"],
            "statuses": ["supported"],
        },
        headers=admin_auth_headers,
    )

    assert exact.status_code == wildcard.status_code == filtered.status_code == 200
    exact_item = exact.json()["data"]["items"][0]
    assert exact_item["node"]["id"] == f"goal:{ids['goal_id']}"
    assert exact_item["match_reasons"] == ["exact_title", "field:title"]
    assert wildcard.json()["data"]["items"] == []
    assert [item["node"]["entity_type"] for item in filtered.json()["data"]["items"]] == ["claim"]


def test_graph_neighborhood_traverses_typed_edges_without_full_graph_materialization(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    ids = _create_graph_fixture(client, admin_auth_headers)

    response = client.get(
        f"/projects/{ids['project_id']}/graph/neighborhood/claim/{ids['claim_id']}",
        params={
            "direction": "both",
            "depth": 2,
            "max_nodes": 50,
            "max_edges": 100,
            "include_anchor_content": True,
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["anchor"]["id"] == f"claim:{ids['claim_id']}"
    assert "Evidence supports the claim" in data["anchor_content"]
    assert data["anchor_content_truncated"] is False
    relationships = {edge["relationship"] for edge in data["edges"]}
    assert {
        "claim_dataset_support",
        "claim_analysis_support",
        "claim_question_answers",
        "exploration_target",
        "exploration_evidence",
        "visualization_claim",
    }.issubset(relationships)
    semantic_edge = next(
        edge for edge in data["edges"] if edge["relationship"] == "claim_dataset_support"
    )
    assert semantic_edge["semantics"]["kind"] == "direct"
    assert semantic_edge["semantics"]["predicate_iri"]


def test_dataset_manifest_note_edge_matches_full_graph_and_neighborhood(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    ids = _create_graph_fixture(client, admin_auth_headers)
    full = client.get(
        f"/projects/{ids['project_id']}/graph?view=full",
        headers=admin_auth_headers,
    ).json()["data"]
    neighborhood = client.get(
        f"/projects/{ids['project_id']}/graph/neighborhood/dataset/{ids['dataset_id']}",
        params={"depth": 1},
        headers=admin_auth_headers,
    ).json()["data"]
    full_edge = next(
        edge
        for edge in full["edges"]
        if edge["relationship"] == "dataset_manifest_note"
    )
    bounded_edge = next(
        edge
        for edge in neighborhood["edges"]
        if edge["relationship"] == "dataset_manifest_note"
    )
    assert full_edge["source"] == bounded_edge["source"] == f"note:{ids['note_id']}"
    assert full_edge["target"] == bounded_edge["target"] == f"dataset:{ids['dataset_id']}"
    assert bounded_edge["semantics"]["direction"] == "target_to_source"


def test_graph_neighborhood_project_mismatch_is_opaque(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    ids = _create_graph_fixture(client, admin_auth_headers)
    other_project_id = client.post(
        "/projects",
        json={"name": "Other graph"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]

    response = client.get(
        f"/projects/{other_project_id}/graph/neighborhood/claim/{ids['claim_id']}",
        headers=admin_auth_headers,
    )

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Project does not exist."


def test_graph_overview_empty_project_and_entry_points_are_deterministic(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    empty_project_id = client.post(
        "/projects",
        json={"name": "Empty graph"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    empty = client.get(
        f"/projects/{empty_project_id}/graph/overview",
        headers=admin_auth_headers,
    )

    assert empty.status_code == 200
    empty_data = empty.json()["data"]
    assert all(count["total"] == 0 for count in empty_data["counts"].values())
    assert empty_data["open_goals"] == []
    assert empty_data["open_questions"] == []
    assert empty_data["recent_nodes"] == []

    ids = _create_graph_fixture(client, admin_auth_headers)
    for index in range(7):
        question = client.post(
            "/questions",
            json={
                "project_id": ids["project_id"],
                "text": f"Bounded overview question {index}",
                "question_type": "descriptive",
                "status": "active",
            },
            headers=admin_auth_headers,
        )
        assert question.status_code == 201
        goal = client.post(
            f"/projects/{ids['project_id']}/goals",
            json={"goal_type": "paper", "title": f"Bounded overview goal {index}"},
            headers=admin_auth_headers,
        )
        assert goal.status_code == 201

    first = client.get(
        f"/projects/{ids['project_id']}/graph/overview",
        headers=admin_auth_headers,
    ).json()["data"]
    second = client.get(
        f"/projects/{ids['project_id']}/graph/overview",
        headers=admin_auth_headers,
    ).json()["data"]

    assert len(first["open_goals"]) == 5
    assert len(first["open_questions"]) == 5
    assert len(first["recent_nodes"]) == 10
    assert first == second


def test_graph_search_covers_all_types_ranking_pagination_unicode_and_snippets(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    ids = _create_graph_fixture(client, admin_auth_headers)
    dataset = client.get(
        f"/datasets/{ids['dataset_id']}",
        headers=admin_auth_headers,
    ).json()["data"]
    queries = {
        "question": "linked",
        "session": ids["scientific_session_id"],
        "note": "Figure note",
        "dataset": dataset["commit_hash"],
        "analysis": "code-1",
        "claim": "supports the claim",
        "exploration_node": "committed analysis",
        "visualization": "Main figure",
        "goal": "Graph paper",
    }
    for entity_type, query in queries.items():
        response = client.get(
            f"/projects/{ids['project_id']}/graph/search",
            params={"q": query, "entity_types": entity_type},
            headers=admin_auth_headers,
        )
        assert response.status_code == 200, (entity_type, response.text)
        assert response.json()["data"]["items"][0]["node"]["entity_type"] == entity_type

    ranking_ids = []
    for text in (
        "Rank target",
        "Rank target prefix extension",
        "Before rank target substring",
    ):
        response = client.post(
            "/questions",
            json={
                "project_id": ids["project_id"],
                "text": text,
                "question_type": "descriptive",
                "status": "active",
            },
            headers=admin_auth_headers,
        )
        assert response.status_code == 201
        ranking_ids.append(response.json()["data"]["question_id"])
    ranked = client.get(
        f"/projects/{ids['project_id']}/graph/search",
        params={"q": "Rank target", "entity_types": "question"},
        headers=admin_auth_headers,
    ).json()["data"]["items"]
    assert [item["node"]["entity_id"] for item in ranked[:3]] == ranking_ids
    assert [item["match_reasons"][0] for item in ranked[:3]] == [
        "exact_title",
        "prefix",
        "substring",
    ]

    long_text = "α" * 500 + " Δresponse " + "β" * 500
    note = client.post(
        "/notes",
        json={"project_id": ids["project_id"], "raw_content": long_text},
        headers=admin_auth_headers,
    )
    assert note.status_code == 201
    unicode_result = client.get(
        f"/projects/{ids['project_id']}/graph/search",
        params={"q": "Δresponse", "entity_types": "note"},
        headers=admin_auth_headers,
    ).json()["data"]["items"][0]
    assert "Δresponse" in unicode_result["snippet"]
    assert len(unicode_result["snippet"]) <= 280

    first_page = client.get(
        f"/projects/{ids['project_id']}/graph/search",
        params={"q": "linked", "limit": 1},
        headers=admin_auth_headers,
    ).json()["data"]
    assert first_page["has_more"] is True
    assert first_page["next_offset"] == 1
    second_page = client.get(
        f"/projects/{ids['project_id']}/graph/search",
        params={"q": "linked", "limit": 1, "offset": first_page["next_offset"]},
        headers=admin_auth_headers,
    ).json()["data"]
    assert second_page["items"][0]["node"]["id"] != first_page["items"][0]["node"]["id"]


def test_graph_neighborhood_direction_filters_cycles_and_truncation_are_deterministic(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    ids = _create_graph_fixture(client, admin_auth_headers)
    base_path = (
        f"/projects/{ids['project_id']}/graph/neighborhood/claim/{ids['claim_id']}"
    )
    outgoing = client.get(
        base_path,
        params={
            "direction": "outgoing",
            "relationships": "claim_question_answers",
            "node_types": "question",
        },
        headers=admin_auth_headers,
    ).json()["data"]
    incoming = client.get(
        base_path,
        params={
            "direction": "incoming",
            "relationships": "claim_analysis_support",
            "node_types": "analysis",
        },
        headers=admin_auth_headers,
    ).json()["data"]
    assert {edge["relationship"] for edge in outgoing["edges"]} == {
        "claim_question_answers"
    }
    assert {node["entity_type"] for node in outgoing["nodes"]} == {"question"}
    assert {edge["relationship"] for edge in incoming["edges"]} == {
        "claim_analysis_support"
    }
    assert {node["entity_type"] for node in incoming["nodes"]} == {"analysis"}

    params = {"depth": 2, "max_nodes": 2, "max_edges": 100}
    first = client.get(base_path, params=params, headers=admin_auth_headers).json()["data"]
    second = client.get(base_path, params=params, headers=admin_auth_headers).json()["data"]
    assert first == second
    assert first["truncation"]["truncated"] is True
    assert first["truncation"]["node_limit_reached"] is True
    assert len(first["nodes"]) <= 1
    assert len({node["id"] for node in first["nodes"]}) == len(first["nodes"])
    assert len({edge["id"] for edge in first["edges"]}) == len(first["edges"])

    invalid_requests = (
        (base_path, {"depth": 3}),
        (base_path, {"max_nodes": 201}),
        (base_path, {"max_edges": 501}),
        (base_path, {"node_types": "project"}),
        (base_path, {"relationships": "not_a_relationship"}),
        (
            f"/projects/{ids['project_id']}/graph/neighborhood/external_artifact/"
            f"{ids['claim_id']}",
            {},
        ),
    )
    for path, invalid_params in invalid_requests:
        response = client.get(path, params=invalid_params, headers=admin_auth_headers)
        assert response.status_code == 422, response.text


def test_graph_neighborhood_bounds_anchor_content_and_keeps_external_artifacts_as_leaves(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = client.post(
        "/projects",
        json={"name": "Bounded content graph"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    long_note = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": "x" * 8_001},
        headers=admin_auth_headers,
    )
    assert long_note.status_code == 201
    note_id = long_note.json()["data"]["note_id"]
    content = client.get(
        f"/projects/{project_id}/graph/neighborhood/note/{note_id}",
        params={"include_anchor_content": True},
        headers=admin_auth_headers,
    ).json()["data"]
    assert len(content["anchor_content"]) == 8_000
    assert content["anchor_content_truncated"] is True

    citation = {
        "source_system": "doi",
        "uri": "doi:10.1101/bounded-graph",
        "content_hash": "sha256:bounded",
    }
    claim = client.post(
        "/claims",
        json={
            "project_id": project_id,
            "statement": "External evidence is a leaf.",
            "confidence": 50,
            "external_citations": [citation],
        },
        headers=admin_auth_headers,
    )
    assert claim.status_code == 201
    claim_id = claim.json()["data"]["claim_id"]
    neighborhood = client.get(
        f"/projects/{project_id}/graph/neighborhood/claim/{claim_id}",
        params={"direction": "outgoing", "depth": 2, "relationships": "claim_cites"},
        headers=admin_auth_headers,
    ).json()["data"]
    assert [node["entity_type"] for node in neighborhood["nodes"]] == [
        "external_artifact"
    ]
    assert [edge["relationship"] for edge in neighborhood["edges"]] == ["claim_cites"]
    assert not any(
        edge["source"].startswith("external_artifact:")
        for edge in neighborhood["edges"]
    )
