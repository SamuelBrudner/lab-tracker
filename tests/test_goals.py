from __future__ import annotations

from uuid import uuid4

import pytest
from api_helpers import repository_backed_api
from fastapi.testclient import TestClient
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import IntegrityError

from lab_tracker.auth import AuthContext, Role
from lab_tracker.db_models import GoalLinkModel, QuestionModel
from lab_tracker.errors import ValidationError
from lab_tracker.goals_attributes import validate_goal_attributes
from lab_tracker.models import (
    AnalysisStatus,
    DatasetCommitManifestInput,
    DatasetFile,
    DatasetStatus,
    EntityRef,
    EntityType,
    GoalLinkStatus,
    GoalRelation,
    GoalType,
    GraphChangeOp,
    GraphChangeOperation,
    GraphChangeSet,
    GraphDraftSemanticType,
    QuestionStatus,
    QuestionType,
)
from lab_tracker.services import goal_link_cleanup
from lab_tracker.services.goal_service import GoalLinkSpec
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository


def _actor(role: Role = Role.ADMIN) -> AuthContext:
    return AuthContext(user_id=uuid4(), role=role)


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register_user(client: TestClient, username: str) -> tuple[dict[str, str], str]:
    response = client.post(
        "/auth/register",
        json={"username": username, "password": "secret"},
    )
    assert response.status_code == 201
    data = response.json()["data"]
    return _auth_headers(data["access_token"]), data["user"]["user_id"]


def _create_project(client: TestClient, headers: dict[str, str], name: str) -> str:
    response = client.post("/projects", json={"name": name}, headers=headers)
    assert response.status_code == 201
    return response.json()["data"]["project_id"]


def _add_project_member(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: str,
    user_id: str,
    role: str = "viewer",
) -> None:
    response = client.post(
        f"/projects/{project_id}/members",
        json={"user_id": user_id, "role": role},
        headers=headers,
    )
    assert response.status_code == 201


def _api_project_question():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Goal Project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Does goal tagging work?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    return api, actor, project, question


def test_goal_attribute_registry_known_types_and_other_escape_hatch():
    assert validate_goal_attributes(
        GoalType.PAPER,
        {"target_venue": "Neuron", "submission_deadline": "2026-08-01"},
    ) == {"target_venue": "Neuron", "submission_deadline": "2026-08-01"}

    with pytest.raises(ValueError):
        validate_goal_attributes(GoalType.PAPER, [])

    with pytest.raises(PydanticValidationError):
        validate_goal_attributes(GoalType.GRANT, {"typo_field": "nope"})

    assert validate_goal_attributes(GoalType.OTHER, {"custom": {"nested": True}}) == {
        "custom": {"nested": True}
    }


def test_goals_round_trip_links_and_reverse_lookup_through_repository():
    api, actor, project, question = _api_project_question()
    goal = api.create_goal(
        project.project_id,
        goal_type=GoalType.PAPER,
        title="Goal paper",
        attributes={"target_venue": "Cell"},
        actor=actor,
    )
    link = api.link_node_to_goal(
        goal.goal_id,
        target=EntityRef(entity_type=EntityType.QUESTION, entity_id=question.question_id),
        relation=GoalRelation.ADDRESSES,
        actor=actor,
    )

    reloaded = api.get_goal(goal.goal_id)
    assert reloaded.attributes == {"target_venue": "Cell"}
    assert reloaded.links[0].link_id == link.link_id
    assert reloaded.links[0].link_status == GoalLinkStatus.CANDIDATE
    assert reloaded.links[0].slot is None

    reverse = api.list_node_goals(
        project_id=project.project_id,
        target=EntityRef(entity_type=EntityType.QUESTION, entity_id=question.question_id),
    )
    assert [item.goal_id for item in reverse] == [goal.goal_id]


def test_query_goals_filters_distinct_goals_by_multiple_targets():
    api, actor, project, question = _api_project_question()
    other_question = api.create_question(
        project_id=project.project_id,
        text="Does the second target find goals?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    first_goal = api.create_goal(
        project.project_id,
        goal_type=GoalType.PAPER,
        title="Multi-linked paper",
        links=[
            GoalLinkSpec(
                target=EntityRef(
                    entity_type=EntityType.QUESTION,
                    entity_id=question.question_id,
                ),
                relation=GoalRelation.ADDRESSES,
            ),
            GoalLinkSpec(
                target=EntityRef(
                    entity_type=EntityType.QUESTION,
                    entity_id=other_question.question_id,
                ),
                relation=GoalRelation.CANDIDATE_FIGURE,
            ),
        ],
        actor=actor,
    )
    second_goal = api.create_goal(
        project.project_id,
        goal_type=GoalType.TALK,
        title="Second linked talk",
        links=[
            GoalLinkSpec(
                target=EntityRef(
                    entity_type=EntityType.QUESTION,
                    entity_id=other_question.question_id,
                ),
                relation=GoalRelation.ADDRESSES,
            )
        ],
        actor=actor,
    )
    api.create_goal(
        project.project_id,
        goal_type=GoalType.OTHER,
        title="Unmatched goal",
        actor=actor,
    )

    goals, total = api.goals.repository.query_goals(
        target_entity_keys={
            (EntityType.QUESTION.value, question.question_id),
            (EntityType.QUESTION.value, other_question.question_id),
        },
        limit=None,
        offset=0,
    )

    assert total == 2
    assert {goal.goal_id for goal in goals} == {first_goal.goal_id, second_goal.goal_id}


def test_goal_links_are_removed_when_target_question_is_deleted():
    api, actor, project, question = _api_project_question()
    goal = api.create_goal(
        project.project_id,
        goal_type=GoalType.PAPER,
        title="Question-linked paper",
        actor=actor,
    )
    link = api.link_node_to_goal(
        goal.goal_id,
        target=EntityRef(entity_type=EntityType.QUESTION, entity_id=question.question_id),
        relation=GoalRelation.ADDRESSES,
        actor=actor,
    )
    _, session = api._test_resources  # type: ignore[attr-defined]

    api.delete_question(question.question_id, actor=actor)

    assert api.get_goal(goal.goal_id).links == []
    assert session.get(GoalLinkModel, str(link.link_id)) is None


def test_analysis_delete_removes_goal_links_to_cascaded_visualizations():
    api, actor, project, question = _api_project_question()
    dataset = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        status=DatasetStatus.COMMITTED,
        commit_manifest=DatasetCommitManifestInput(
            files=[DatasetFile(path="data.csv", checksum="abc123")]
        ),
        actor=actor,
    )
    analysis = api.create_analysis(
        project_id=project.project_id,
        dataset_ids=[dataset.dataset_id],
        method_hash="method-goal-cleanup",
        code_version="v1",
        status=AnalysisStatus.COMMITTED,
        actor=actor,
    )
    visualization = api.create_visualization(
        analysis_id=analysis.analysis_id,
        viz_type="line",
        file_path="figures/line.png",
        actor=actor,
    )
    goal = api.create_goal(
        project.project_id,
        goal_type=GoalType.PAPER,
        title="Visualization-linked paper",
        actor=actor,
    )
    link = api.link_node_to_goal(
        goal.goal_id,
        target=EntityRef(
            entity_type=EntityType.VISUALIZATION,
            entity_id=visualization.viz_id,
        ),
        relation=GoalRelation.CANDIDATE_FIGURE,
        actor=actor,
    )
    _, session = api._test_resources  # type: ignore[attr-defined]

    api.delete_analysis(analysis.analysis_id, actor=actor)

    assert api.get_goal(goal.goal_id).links == []
    assert session.get(GoalLinkModel, str(link.link_id)) is None


def test_project_delete_removes_projectless_goal_links_to_cascaded_entities():
    api = repository_backed_api()
    actor = _actor()
    removed_project = api.create_project("Deleted goal scope", actor=actor)
    retained_project = api.create_project("Retained goal scope", actor=actor)
    removed_question = api.create_question(
        project_id=removed_project.project_id,
        text="Will this project be removed?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    goal = api.create_goal(
        None,
        goal_type=GoalType.GRANT,
        title="Cross-project goal",
        links=[
            GoalLinkSpec(
                target=EntityRef(
                    entity_type=EntityType.PROJECT,
                    entity_id=removed_project.project_id,
                ),
                relation=GoalRelation.CONTRIBUTES_TO,
            ),
            GoalLinkSpec(
                target=EntityRef(
                    entity_type=EntityType.PROJECT,
                    entity_id=retained_project.project_id,
                ),
                relation=GoalRelation.CONTRIBUTES_TO,
            ),
            GoalLinkSpec(
                target=EntityRef(
                    entity_type=EntityType.QUESTION,
                    entity_id=removed_question.question_id,
                ),
                relation=GoalRelation.ADDRESSES,
            ),
        ],
        actor=actor,
    )
    removed_link_ids = {
        link.link_id
        for link in goal.links
        if link.target.entity_id in {removed_project.project_id, removed_question.question_id}
    }
    _, session = api._test_resources  # type: ignore[attr-defined]

    api.delete_project(removed_project.project_id, actor=actor)

    reloaded = api.get_goal(goal.goal_id)
    assert [(link.target.entity_type, link.target.entity_id) for link in reloaded.links] == [
        (EntityType.PROJECT, retained_project.project_id)
    ]
    assert all(
        session.get(GoalLinkModel, str(link_id)) is None for link_id in removed_link_ids
    )


def test_project_delete_batches_goal_reverse_lookup_for_cascaded_entities():
    api = repository_backed_api()
    actor = _actor()
    removed_project = api.create_project("Deleted batched goal lookup", actor=actor)
    questions = [
        api.create_question(
            project_id=removed_project.project_id,
            text=f"Batched cleanup question {index}",
            question_type=QuestionType.DESCRIPTIVE,
            status=QuestionStatus.ACTIVE,
            actor=actor,
        )
        for index in range(3)
    ]
    for question in questions:
        api.create_goal(
            None,
            goal_type=GoalType.GRANT,
            title=f"Goal for {question.text}",
            links=[
                GoalLinkSpec(
                    target=EntityRef(
                        entity_type=EntityType.PROJECT,
                        entity_id=removed_project.project_id,
                    ),
                    relation=GoalRelation.CONTRIBUTES_TO,
                ),
                GoalLinkSpec(
                    target=EntityRef(
                        entity_type=EntityType.QUESTION,
                        entity_id=question.question_id,
                    ),
                    relation=GoalRelation.ADDRESSES,
                ),
            ],
            actor=actor,
        )
    repository = api.goals.repository
    original_query_goals = repository.query_goals
    reverse_lookup_calls: list[dict[str, object]] = []

    def counting_query_goals(**kwargs):
        if kwargs.get("target_entity_keys") is not None or (
            kwargs.get("target_entity_type") is not None
            and kwargs.get("target_entity_id") is not None
        ):
            reverse_lookup_calls.append(kwargs)
        return original_query_goals(**kwargs)

    repository.query_goals = counting_query_goals  # type: ignore[method-assign]

    api.delete_project(removed_project.project_id, actor=actor)

    assert len(reverse_lookup_calls) == 1
    target_keys = reverse_lookup_calls[0]["target_entity_keys"]
    assert isinstance(target_keys, set)
    assert (EntityType.PROJECT.value, removed_project.project_id) in target_keys
    assert {
        (EntityType.QUESTION.value, question.question_id) for question in questions
    }.issubset(target_keys)


def test_project_delete_chunks_goal_reverse_lookup_for_many_cascaded_entities(
    monkeypatch: pytest.MonkeyPatch,
):
    api = repository_backed_api()
    actor = _actor()
    removed_project = api.create_project("Deleted chunked goal lookup", actor=actor)
    retained_project = api.create_project("Retained chunked goal scope", actor=actor)
    questions = [
        api.create_question(
            project_id=removed_project.project_id,
            text=f"Chunked cleanup question {index}",
            question_type=QuestionType.DESCRIPTIVE,
            status=QuestionStatus.ACTIVE,
            actor=actor,
        )
        for index in range(3)
    ]
    goal = api.create_goal(
        None,
        goal_type=GoalType.GRANT,
        title="Chunked cleanup grant",
        links=[
            GoalLinkSpec(
                target=EntityRef(
                    entity_type=EntityType.PROJECT,
                    entity_id=retained_project.project_id,
                ),
                relation=GoalRelation.CONTRIBUTES_TO,
            ),
            GoalLinkSpec(
                target=EntityRef(
                    entity_type=EntityType.PROJECT,
                    entity_id=removed_project.project_id,
                ),
                relation=GoalRelation.CONTRIBUTES_TO,
            ),
            *[
                GoalLinkSpec(
                    target=EntityRef(
                        entity_type=EntityType.QUESTION,
                        entity_id=question.question_id,
                    ),
                    relation=GoalRelation.ADDRESSES,
                )
                for question in questions
            ],
        ],
        actor=actor,
    )
    monkeypatch.setattr(goal_link_cleanup, "_GOAL_TARGET_QUERY_CHUNK_SIZE", 2)
    repository = api.goals.repository
    original_query_goals = repository.query_goals
    reverse_lookup_calls: list[set[tuple[str, object]]] = []

    def counting_query_goals(**kwargs):
        if kwargs.get("target_entity_keys") is not None:
            reverse_lookup_calls.append(set(kwargs["target_entity_keys"]))
        return original_query_goals(**kwargs)

    repository.query_goals = counting_query_goals  # type: ignore[method-assign]

    api.delete_project(removed_project.project_id, actor=actor)

    assert len(reverse_lookup_calls) == 2
    assert all(len(target_keys) <= 2 for target_keys in reverse_lookup_calls)
    all_target_keys = set().union(*reverse_lookup_calls)
    assert (EntityType.PROJECT.value, removed_project.project_id) in all_target_keys
    assert {
        (EntityType.QUESTION.value, question.question_id) for question in questions
    }.issubset(all_target_keys)
    reloaded = api.get_goal(goal.goal_id)
    assert [(link.target.entity_type, link.target.entity_id) for link in reloaded.links] == [
        (EntityType.PROJECT, retained_project.project_id)
    ]


def test_projectless_goal_requires_project_link_scope():
    api, actor, _project, _question = _api_project_question()

    with pytest.raises(ValidationError, match="Projectless goals"):
        api.create_goal(
            None,
            goal_type=GoalType.GRANT,
            title="Unscoped grant",
            actor=actor,
        )


def test_require_goal_read_returns_the_complete_authorized_scope():
    api = repository_backed_api()
    actor = _actor()
    project_a = api.create_project("Goal scope A", actor=actor)
    project_b = api.create_project("Goal scope B", actor=actor)
    question_b = api.create_question(
        project_id=project_b.project_id,
        text="Does the authorization scope include linked entity projects?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    goal = api.create_goal(
        None,
        goal_type=GoalType.GRANT,
        title="Cross-project authorization scope",
        links=[
            GoalLinkSpec(
                target=EntityRef(
                    entity_type=EntityType.PROJECT,
                    entity_id=project_a.project_id,
                ),
                relation=GoalRelation.CONTRIBUTES_TO,
            ),
            GoalLinkSpec(
                target=EntityRef(
                    entity_type=EntityType.QUESTION,
                    entity_id=question_b.question_id,
                ),
                relation=GoalRelation.ADDRESSES,
            ),
        ],
        actor=actor,
    )

    assert api.require_goal_read(goal, actor=actor) == {
        project_a.project_id,
        project_b.project_id,
    }


def test_goal_links_use_empty_db_slot_to_enforce_unslotted_uniqueness():
    api, actor, project, question = _api_project_question()
    goal = api.create_goal(
        project.project_id,
        goal_type=GoalType.PAPER,
        title="Unique link paper",
        actor=actor,
    )
    api.link_node_to_goal(
        goal.goal_id,
        target=EntityRef(entity_type=EntityType.QUESTION, entity_id=question.question_id),
        relation=GoalRelation.ADDRESSES,
        actor=actor,
    )
    _, session = api._test_resources  # type: ignore[attr-defined]
    session.add(
        GoalLinkModel(
            link_id=str(uuid4()),
            goal_id=str(goal.goal_id),
            entity_type=EntityType.QUESTION.value,
            entity_id=str(question.question_id),
            relation=GoalRelation.ADDRESSES.value,
            link_status=GoalLinkStatus.CANDIDATE.value,
            slot="",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_goal_routes_create_link_reverse_lookup_and_goal_scoped_search(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = client.post(
        "/projects",
        json={"name": "Goal routes"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Does baseline evidence support Figure 3?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=admin_auth_headers,
    ).json()["data"]["question_id"]

    invalid = client.post(
        f"/projects/{project_id}/goals",
        json={
            "goal_type": "paper",
            "title": "Invalid goal",
            "attributes": {"unknown_key": "blocked"},
        },
        headers=admin_auth_headers,
    )
    assert invalid.status_code == 422

    created = client.post(
        f"/projects/{project_id}/goals",
        json={
            "goal_type": "paper",
            "title": "Baseline manuscript",
            "attributes": {"target_venue": "eLife"},
        },
        headers=admin_auth_headers,
    )
    assert created.status_code == 201
    goal_id = created.json()["data"]["goal_id"]

    link = client.post(
        f"/goals/{goal_id}/links",
        json={
            "entity_type": "question",
            "entity_id": question_id,
            "relation": "candidate_figure",
            "slot": "Figure 3",
        },
        headers=admin_auth_headers,
    )
    assert link.status_code == 201
    link_payload = link.json()["data"]
    assert link_payload["link_status"] == "candidate"

    promoted = client.patch(
        f"/goals/{goal_id}/links/{link_payload['link_id']}",
        json={"link_status": "committed"},
        headers=admin_auth_headers,
    )
    assert promoted.status_code == 200
    assert promoted.json()["data"]["link_status"] == "committed"

    reverse = client.get(
        f"/projects/{project_id}/nodes/question/{question_id}/goals",
        headers=admin_auth_headers,
    )
    assert reverse.status_code == 200
    assert [item["goal_id"] for item in reverse.json()["data"]] == [goal_id]

    search = client.get(
        "/search",
        params={"q": "baseline", "project_id": project_id, "goal_id": goal_id},
        headers=admin_auth_headers,
    )
    assert search.status_code == 200
    assert [item["question_id"] for item in search.json()["data"]["questions"]] == [question_id]


def test_spanning_goal_index_requires_access_to_all_linked_projects(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_a = _create_project(client, admin_auth_headers, "Spanning A")
    project_b = _create_project(client, admin_auth_headers, "Spanning B")
    project_c = _create_project(client, admin_auth_headers, "Hidden C")
    viewer_headers, viewer_user_id = _register_user(
        client,
        f"goal-viewer-{uuid4().hex[:8]}",
    )

    hidden_goal = client.post(
        f"/projects/{project_c}/goals",
        json={"goal_type": "paper", "title": "Hidden manuscript"},
        headers=admin_auth_headers,
    )
    assert hidden_goal.status_code == 201
    hidden_goal_id = hidden_goal.json()["data"]["goal_id"]
    readable_project_goal = client.get(
        f"/goals/{hidden_goal_id}",
        headers=admin_auth_headers,
    )
    assert readable_project_goal.status_code == 200
    assert readable_project_goal.json()["data"]["goal_id"] == hidden_goal_id
    created = client.post(
        "/goals",
        json={
            "project_id": None,
            "goal_type": "grant",
            "title": "Cross-project grant",
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
    assert created.status_code == 201
    goal = created.json()["data"]
    goal_id = goal["goal_id"]
    assert goal["project_id"] is None
    assert {link["target"]["entity_id"] for link in goal["links"]} == {project_a, project_b}

    missing_goal_id = str(uuid4())
    missing = client.get(f"/goals/{missing_goal_id}", headers=viewer_headers)
    zero_scope_spanning = client.get(f"/goals/{goal_id}", headers=viewer_headers)
    zero_scope_project = client.get(f"/goals/{hidden_goal_id}", headers=viewer_headers)

    assert missing.status_code == zero_scope_spanning.status_code == 404
    assert missing.status_code == zero_scope_project.status_code
    assert zero_scope_spanning.json() == zero_scope_project.json() == missing.json()
    assert missing.json()["error"] == {
        "code": "not_found",
        "message": "Goal does not exist.",
        "issues": None,
    }
    assert client.get(f"/goals/{goal_id}").status_code == 401
    invalid_credentials = client.get(
        f"/goals/{goal_id}",
        headers={"Authorization": "Bearer invalid-token"},
    )
    assert invalid_credentials.status_code == 401

    _add_project_member(
        client,
        admin_auth_headers,
        project_id=project_a,
        user_id=viewer_user_id,
    )
    partially_scoped = client.get("/goals", headers=viewer_headers)
    partially_scoped_project = client.get(
        f"/projects/{project_a}/goals",
        headers=viewer_headers,
    )
    partially_scoped_get = client.get(f"/goals/{goal_id}", headers=viewer_headers)
    partial_missing = client.get(f"/goals/{missing_goal_id}", headers=viewer_headers)

    assert partially_scoped.status_code == 200
    assert partially_scoped.json()["data"] == []
    assert partially_scoped_project.status_code == 200
    assert partially_scoped_project.json()["data"] == []
    assert partially_scoped_get.status_code == partial_missing.status_code == 404
    assert partially_scoped_get.json() == partial_missing.json()

    unauthorized_patch = client.patch(
        f"/goals/{goal_id}",
        json={"title": "Must remain forbidden"},
        headers=viewer_headers,
    )
    unauthorized_delete = client.delete(
        f"/goals/{goal_id}",
        headers=viewer_headers,
    )
    assert unauthorized_patch.status_code == unauthorized_delete.status_code == 401

    _add_project_member(
        client,
        admin_auth_headers,
        project_id=project_b,
        user_id=viewer_user_id,
    )

    global_index = client.get("/goals", headers=viewer_headers)
    project_index = client.get(f"/projects/{project_a}/goals", headers=viewer_headers)
    direct_get = client.get(f"/goals/{goal_id}", headers=viewer_headers)

    assert global_index.status_code == 200
    assert [item["goal_id"] for item in global_index.json()["data"]] == [goal_id]
    assert project_index.status_code == 200
    assert [item["goal_id"] for item in project_index.json()["data"]] == [goal_id]
    assert direct_get.status_code == 200
    assert direct_get.json()["data"]["goal_id"] == goal_id


def test_spanning_goal_search_is_opaque_until_the_full_scope_is_authorized(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_a = _create_project(client, admin_auth_headers, "Search scope A")
    project_b = _create_project(client, admin_auth_headers, "Search scope B")
    project_c = _create_project(client, admin_auth_headers, "Search scope C")

    linked_question = client.post(
        "/questions",
        json={
            "project_id": project_a,
            "text": "Spanning needle linked question",
            "question_type": "descriptive",
        },
        headers=admin_auth_headers,
    )
    assert linked_question.status_code == 201
    linked_question_id = linked_question.json()["data"]["question_id"]
    unlinked_question = client.post(
        "/questions",
        json={
            "project_id": project_a,
            "text": "Spanning needle unlinked question",
            "question_type": "descriptive",
        },
        headers=admin_auth_headers,
    )
    assert unlinked_question.status_code == 201
    outside_question = client.post(
        "/questions",
        json={
            "project_id": project_c,
            "text": "Spanning needle outside question",
            "question_type": "descriptive",
        },
        headers=admin_auth_headers,
    )
    assert outside_question.status_code == 201

    linked_note = client.post(
        "/notes",
        json={
            "project_id": project_b,
            "raw_content": "Spanning needle linked note",
        },
        headers=admin_auth_headers,
    )
    assert linked_note.status_code == 201
    linked_note_id = linked_note.json()["data"]["note_id"]
    unlinked_note = client.post(
        "/notes",
        json={
            "project_id": project_b,
            "raw_content": "Spanning needle unlinked note",
        },
        headers=admin_auth_headers,
    )
    assert unlinked_note.status_code == 201

    created = client.post(
        "/goals",
        json={
            "project_id": None,
            "goal_type": "grant",
            "title": "Search across projects",
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
                {
                    "entity_type": "question",
                    "entity_id": linked_question_id,
                    "relation": "addresses",
                },
                {
                    "entity_type": "note",
                    "entity_id": linked_note_id,
                    "relation": "supporting_evidence",
                },
            ],
        },
        headers=admin_auth_headers,
    )
    assert created.status_code == 201
    goal_id = created.json()["data"]["goal_id"]
    missing_goal_id = str(uuid4())
    viewer_headers, viewer_user_id = _register_user(
        client,
        f"spanning-search-viewer-{uuid4().hex[:8]}",
    )

    unaffiliated = client.get(
        "/search",
        params={"q": "spanning needle", "goal_id": goal_id},
        headers=viewer_headers,
    )
    missing = client.get(
        "/search",
        params={"q": "spanning needle", "goal_id": missing_goal_id},
        headers=viewer_headers,
    )

    assert unaffiliated.status_code == missing.status_code == 404
    assert unaffiliated.json() == missing.json()
    assert unaffiliated.json()["error"] == {
        "code": "not_found",
        "message": "Goal does not exist.",
        "issues": None,
    }

    _add_project_member(
        client,
        admin_auth_headers,
        project_id=project_a,
        user_id=viewer_user_id,
    )
    partially_authorized = client.get(
        "/search",
        params={
            "q": "spanning needle",
            "goal_id": goal_id,
            "project_id": project_a,
        },
        headers=viewer_headers,
    )
    partial_missing = client.get(
        "/search",
        params={
            "q": "spanning needle",
            "goal_id": missing_goal_id,
            "project_id": project_a,
        },
        headers=viewer_headers,
    )

    assert partially_authorized.status_code == partial_missing.status_code == 404
    assert partially_authorized.json() == partial_missing.json()

    partial_unreadable_project = client.get(
        "/search",
        params={
            "q": "spanning needle",
            "goal_id": goal_id,
            "project_id": project_c,
        },
        headers=viewer_headers,
    )
    partial_unreadable_project_missing = client.get(
        "/search",
        params={
            "q": "spanning needle",
            "goal_id": missing_goal_id,
            "project_id": project_c,
        },
        headers=viewer_headers,
    )

    assert (
        partial_unreadable_project.status_code
        == partial_unreadable_project_missing.status_code
        == 404
    )
    assert partial_unreadable_project.json() == partial_unreadable_project_missing.json()

    _add_project_member(
        client,
        admin_auth_headers,
        project_id=project_b,
        user_id=viewer_user_id,
    )
    unreadable_mismatch = client.get(
        "/search",
        params={
            "q": "spanning needle",
            "goal_id": goal_id,
            "project_id": project_c,
        },
        headers=viewer_headers,
    )

    assert unreadable_mismatch.status_code == 401
    assert unreadable_mismatch.json()["error"] == {
        "code": "auth_error",
        "message": "Project access required.",
        "issues": None,
    }

    _add_project_member(
        client,
        admin_auth_headers,
        project_id=project_c,
        user_id=viewer_user_id,
    )

    spanning = client.get(
        "/search",
        params={"q": "spanning needle", "goal_id": goal_id},
        headers=viewer_headers,
    )
    project_a_only = client.get(
        "/search",
        params={
            "q": "spanning needle",
            "goal_id": goal_id,
            "project_id": project_a,
        },
        headers=viewer_headers,
    )
    mismatch = client.get(
        "/search",
        params={
            "q": "spanning needle",
            "goal_id": goal_id,
            "project_id": project_c,
        },
        headers=viewer_headers,
    )

    assert spanning.status_code == 200
    spanning_data = spanning.json()["data"]
    assert [item["question_id"] for item in spanning_data["questions"]] == [linked_question_id]
    assert [item["note_id"] for item in spanning_data["notes"]] == [linked_note_id]
    assert project_a_only.status_code == 200
    project_a_data = project_a_only.json()["data"]
    assert [item["question_id"] for item in project_a_data["questions"]] == [linked_question_id]
    assert project_a_data["notes"] == []
    assert mismatch.status_code == 422
    assert mismatch.json()["error"]["message"] == "goal_id must belong to project_id."


def test_goal_search_hides_a_dangling_link_target_as_a_missing_goal(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers, "Dangling search target")
    question = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Dangling target needle",
            "question_type": "descriptive",
        },
        headers=admin_auth_headers,
    )
    assert question.status_code == 201
    question_id = question.json()["data"]["question_id"]
    goal = client.post(
        f"/projects/{project_id}/goals",
        json={"goal_type": "paper", "title": "Dangling target goal"},
        headers=admin_auth_headers,
    )
    assert goal.status_code == 201
    goal_id = goal.json()["data"]["goal_id"]
    link = client.post(
        f"/goals/{goal_id}/links",
        json={
            "entity_type": "question",
            "entity_id": question_id,
            "relation": "addresses",
        },
        headers=admin_auth_headers,
    )
    assert link.status_code == 201
    link_id = link.json()["data"]["link_id"]

    with client.app.state.db_session_factory() as session:
        question_row = session.get(QuestionModel, question_id)
        assert question_row is not None
        session.delete(question_row)
        session.commit()
        assert session.get(GoalLinkModel, link_id) is not None

    direct_existing = client.get(
        f"/goals/{goal_id}",
        headers=admin_auth_headers,
    )
    direct_missing = client.get(
        f"/goals/{uuid4()}",
        headers=admin_auth_headers,
    )
    assert direct_existing.status_code == direct_missing.status_code == 404
    assert direct_existing.json() == direct_missing.json()
    assert direct_existing.json()["error"] == {
        "code": "not_found",
        "message": "Goal does not exist.",
        "issues": None,
    }

    viewer_headers, _ = _register_user(
        client,
        f"dangling-search-viewer-{uuid4().hex[:8]}",
    )
    existing = client.get(
        "/search",
        params={"q": "needle", "goal_id": goal_id},
        headers=viewer_headers,
    )
    missing = client.get(
        "/search",
        params={"q": "needle", "goal_id": str(uuid4())},
        headers=viewer_headers,
    )

    assert existing.status_code == missing.status_code == 404
    assert existing.json() == missing.json()
    assert existing.json()["error"] == {
        "code": "not_found",
        "message": "Goal does not exist.",
        "issues": None,
    }


def test_project_scoped_goal_search_preserves_opaque_auth_and_exact_project_match(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    goal_project_id = _create_project(client, admin_auth_headers, "Scoped search goal")
    other_project_id = _create_project(client, admin_auth_headers, "Scoped search other")
    question = client.post(
        "/questions",
        json={
            "project_id": goal_project_id,
            "text": "Project scoped needle",
            "question_type": "descriptive",
        },
        headers=admin_auth_headers,
    )
    assert question.status_code == 201
    question_id = question.json()["data"]["question_id"]
    created = client.post(
        f"/projects/{goal_project_id}/goals",
        json={"goal_type": "paper", "title": "Project scoped search"},
        headers=admin_auth_headers,
    )
    assert created.status_code == 201
    goal_id = created.json()["data"]["goal_id"]
    linked = client.post(
        f"/goals/{goal_id}/links",
        json={
            "entity_type": "question",
            "entity_id": question_id,
            "relation": "addresses",
        },
        headers=admin_auth_headers,
    )
    assert linked.status_code == 201

    viewer_headers, viewer_user_id = _register_user(
        client,
        f"project-search-viewer-{uuid4().hex[:8]}",
    )
    unauthorized = client.get(
        "/search",
        params={"q": "project scoped needle", "goal_id": goal_id},
        headers=viewer_headers,
    )
    missing = client.get(
        "/search",
        params={"q": "project scoped needle", "goal_id": str(uuid4())},
        headers=viewer_headers,
    )

    assert unauthorized.status_code == missing.status_code == 404
    assert unauthorized.json() == missing.json()

    for project_id in (goal_project_id, other_project_id):
        _add_project_member(
            client,
            admin_auth_headers,
            project_id=project_id,
            user_id=viewer_user_id,
        )

    matching = client.get(
        "/search",
        params={
            "q": "project scoped needle",
            "goal_id": goal_id,
            "project_id": goal_project_id,
        },
        headers=viewer_headers,
    )
    mismatch = client.get(
        "/search",
        params={
            "q": "project scoped needle",
            "goal_id": goal_id,
            "project_id": other_project_id,
        },
        headers=viewer_headers,
    )

    assert matching.status_code == 200
    assert [item["question_id"] for item in matching.json()["data"]["questions"]] == [question_id]
    assert mismatch.status_code == 422
    assert mismatch.json()["error"]["message"] == "goal_id must belong to project_id."


def test_project_only_spanning_goal_search_pushes_empty_link_filters_and_page_to_database(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
):
    project_a = _create_project(client, admin_auth_headers, "Project-only scope A")
    project_b = _create_project(client, admin_auth_headers, "Project-only scope B")
    for index in range(8):
        project_id = project_a if index % 2 == 0 else project_b
        question = client.post(
            "/questions",
            json={
                "project_id": project_id,
                "text": f"Unrelated spanning question {index}",
                "question_type": "descriptive",
            },
            headers=admin_auth_headers,
        )
        note = client.post(
            "/notes",
            json={
                "project_id": project_id,
                "raw_content": f"Unrelated spanning note {index}",
            },
            headers=admin_auth_headers,
        )
        assert question.status_code == note.status_code == 201

    goal = client.post(
        "/goals",
        json={
            "project_id": None,
            "goal_type": "grant",
            "title": "Project-only spanning goal",
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
    assert goal.status_code == 201
    goal_id = goal.json()["data"]["goal_id"]

    question_calls: list[dict[str, object]] = []
    note_calls: list[dict[str, object]] = []
    original_query_questions = SQLAlchemyLabTrackerRepository.query_questions
    original_query_notes = SQLAlchemyLabTrackerRepository.query_notes

    def track_questions(self, **kwargs):  # noqa: ANN001, ANN003, ANN202
        question_calls.append(dict(kwargs))
        return original_query_questions(self, **kwargs)

    def track_notes(self, **kwargs):  # noqa: ANN001, ANN003, ANN202
        note_calls.append(dict(kwargs))
        return original_query_notes(self, **kwargs)

    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "query_questions",
        track_questions,
    )
    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "query_notes",
        track_notes,
    )

    response = client.get(
        "/search",
        params={"q": "", "goal_id": goal_id, "limit": 1, "offset": 3},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["data"] == {"questions": [], "notes": []}
    assert len(question_calls) == len(note_calls) == 1
    assert question_calls[0]["question_ids"] == set()
    assert note_calls[0]["note_ids"] == set()
    for call in (*question_calls, *note_calls):
        assert call["limit"] == 1
        assert call["offset"] == 3


def test_graph_draft_goal_operations_validate_and_apply_committed_links():
    api, actor, project, question = _api_project_question()
    goal = api.create_goal(
        project.project_id,
        goal_type=GoalType.PAPER,
        title="Draftable paper",
        actor=actor,
    )
    invalid_create = GraphChangeOperation(
        operation_id=uuid4(),
        change_set_id=uuid4(),
        sequence=1,
        op=GraphChangeOp.CREATE,
        entity_type=EntityType.GOAL,
        semantic_type=GraphDraftSemanticType.SUGGEST_NEW_GOAL,
        payload={
            "project_id": str(project.project_id),
            "goal_type": "paper",
            "title": "Bad attributes",
            "attributes": {"typo": "reject"},
        },
    )
    with pytest.raises(ValidationError):
        api.graph_drafts.generation.patch_validator.validate_operation(
            invalid_create,
            invalid_create.payload,
        )

    link_operation = GraphChangeOperation(
        operation_id=uuid4(),
        change_set_id=uuid4(),
        sequence=2,
        op=GraphChangeOp.UPDATE,
        entity_type=EntityType.GOAL,
        semantic_type=GraphDraftSemanticType.LINK_NODE_TO_GOAL,
        target_entity_id=goal.goal_id,
        payload={
            "links": [
                {
                    "entity_type": "question",
                    "entity_id": str(question.question_id),
                    "relation": "addresses",
                }
            ]
        },
    )
    api.graph_drafts.generation.patch_validator.validate_operation(
        link_operation,
        link_operation.payload,
    )
    source_note = api.create_note(
        project.project_id,
        "Goal draft source",
        actor=actor,
    )
    change_set = GraphChangeSet(
        change_set_id=link_operation.change_set_id,
        project_id=project.project_id,
        source_note_id=source_note.note_id,
        model="fake-gpt",
        prompt_version="test",
    )
    api.graph_drafts.records.save_graph_change_set(change_set)
    api.graph_drafts.commit.patch_applier.apply_graph_operation(
        link_operation,
        ref_map={},
        actor=actor,
        change_set=change_set,
    )

    reloaded = api.get_goal(goal.goal_id)
    assert reloaded.links[0].link_status == GoalLinkStatus.COMMITTED
    assert reloaded.links[0].target.entity_id == question.question_id


def test_graph_draft_goal_update_validation_uses_existing_goal_type():
    api, actor, project, _question = _api_project_question()
    goal = api.create_goal(
        project.project_id,
        goal_type=GoalType.PAPER,
        title="Draftable paper",
        attributes={"target_venue": "Cell"},
        actor=actor,
    )
    invalid_attributes = GraphChangeOperation(
        operation_id=uuid4(),
        change_set_id=uuid4(),
        sequence=1,
        op=GraphChangeOp.UPDATE,
        entity_type=EntityType.GOAL,
        semantic_type=GraphDraftSemanticType.UPDATE_GOAL,
        target_entity_id=goal.goal_id,
        payload={"attributes": {"typo_field": "blocked"}},
    )
    with pytest.raises(ValidationError, match="Goal attributes are invalid"):
        api.graph_drafts.generation.patch_validator.validate_operation(
            invalid_attributes,
            invalid_attributes.payload,
        )

    invalid_type_change = GraphChangeOperation(
        operation_id=uuid4(),
        change_set_id=uuid4(),
        sequence=2,
        op=GraphChangeOp.UPDATE,
        entity_type=EntityType.GOAL,
        semantic_type=GraphDraftSemanticType.UPDATE_GOAL,
        target_entity_id=goal.goal_id,
        payload={"goal_type": "grant"},
    )
    with pytest.raises(ValidationError, match="Goal attributes are invalid"):
        api.graph_drafts.generation.patch_validator.validate_operation(
            invalid_type_change,
            invalid_type_change.payload,
        )

    valid_type_change = GraphChangeOperation(
        operation_id=uuid4(),
        change_set_id=uuid4(),
        sequence=3,
        op=GraphChangeOp.UPDATE,
        entity_type=EntityType.GOAL,
        semantic_type=GraphDraftSemanticType.UPDATE_GOAL,
        target_entity_id=goal.goal_id,
        payload={"goal_type": "grant", "attributes": {"funding_agency": "NIH"}},
    )
    api.graph_drafts.generation.patch_validator.validate_operation(
        valid_type_change,
        valid_type_change.payload,
    )
