from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from api_helpers import repository_backed_api
from fastapi.testclient import TestClient

from lab_tracker.auth import AuthContext, Role
from lab_tracker.db_models import (
    AnalysisModel,
    ClaimModel,
    DatasetModel,
    GoalLinkModel,
    GoalModel,
    GraphChangeSetModel,
    GraphDraftBatchRunModel,
    GroupMembershipModel,
    NoteModel,
    ProjectGroupModel,
    ProjectMembershipModel,
    ProjectModel,
    QuestionModel,
    QuestionRefactorModel,
    SessionModel,
    UserModel,
    VisualizationModel,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _user(user_id: UUID, username: str, role: Role = Role.ADMIN) -> UserModel:
    return UserModel(
        user_id=str(user_id),
        username=username,
        password_hash="unused",
        role=role.value,
        created_at=_now(),
    )


def _login_headers(client: TestClient, username: str, password: str = "secret") -> dict[str, str]:
    response = client.post(
        "/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def _register_user(
    client: TestClient,
    *,
    role: Role,
) -> tuple[dict[str, str], str]:
    username = f"ownership-{role.value}-{uuid4().hex[:8]}"
    password = "secret"
    user = client.app.state.auth_service.register_user(
        username=username,
        password=password,
        role=role,
    )
    return _login_headers(client, username, password), str(user.user_id)


def test_ownership_reassignment_moves_all_attribution_surfaces():
    api = repository_backed_api()
    _, session = api._test_resources  # type: ignore[attr-defined]
    from_user_id = uuid4()
    to_user_id = uuid4()
    admin_user_id = uuid4()
    project_group_id = uuid4()
    project_id = uuid4()
    source_question_id = uuid4()
    replacement_question_id = uuid4()
    refactor_id = uuid4()
    dataset_id = uuid4()
    note_id = uuid4()
    change_set_id = uuid4()
    batch_run_id = uuid4()
    session_id = uuid4()
    analysis_id = uuid4()
    claim_id = uuid4()
    visualization_id = uuid4()
    goal_id = uuid4()
    goal_link_id = uuid4()
    project_membership_id = uuid4()
    group_membership_id = uuid4()
    now = _now()
    from_user = str(from_user_id)
    to_user = str(to_user_id)

    session.add_all(
        [
            _user(from_user_id, "departing"),
            _user(to_user_id, "successor"),
            _user(admin_user_id, "reassignment-admin"),
        ]
    )
    session.flush()

    session.add(
        ProjectGroupModel(
            group_id=str(project_group_id),
            name="Lab group",
            description="",
            kind="lab",
            group_read_all=False,
            created_by=from_user,
            created_by_user_id=from_user,
            created_at=now,
            updated_at=now,
        )
    )
    session.flush()

    session.add(
        ProjectModel(
            project_id=str(project_id),
            group_id=str(project_group_id),
            name="Owned project",
            description="",
            status="active",
            created_by=from_user,
            created_by_user_id=from_user,
            created_at=now,
            updated_at=now,
        )
    )
    session.flush()

    session.add_all(
        [
            ProjectMembershipModel(
                membership_id=str(project_membership_id),
                project_id=str(project_id),
                user_id=to_user,
                role="viewer",
                created_by=from_user,
                created_by_user_id=from_user,
                created_at=now,
                updated_at=now,
            ),
            GroupMembershipModel(
                membership_id=str(group_membership_id),
                group_id=str(project_group_id),
                user_id=to_user,
                role="viewer",
                created_by=from_user,
                created_by_user_id=from_user,
                created_at=now,
                updated_at=now,
            ),
            QuestionModel(
                question_id=str(source_question_id),
                project_id=str(project_id),
                text="Departing scientist question",
                question_type="descriptive",
                hypothesis=None,
                status="active",
                created_by=from_user,
                created_by_user_id=from_user,
                created_at=now,
                updated_at=now,
            ),
            QuestionModel(
                question_id=str(replacement_question_id),
                project_id=str(project_id),
                text="Successor scientist question",
                question_type="descriptive",
                hypothesis=None,
                status="active",
                created_by=to_user,
                created_by_user_id=to_user,
                created_at=now,
                updated_at=now,
            ),
            NoteModel(
                note_id=str(note_id),
                project_id=str(project_id),
                raw_content="handoff note",
                note_metadata={},
                status="staged",
                created_by=from_user,
                created_by_user_id=from_user,
                created_at=now,
                updated_at=now,
            ),
            GraphDraftBatchRunModel(
                run_id=str(batch_run_id),
                project_id=str(project_id),
                trigger="manual",
                status="ready",
                window_start=now,
                window_end=now,
                note_count=1,
                batch_key=f"batch-{batch_run_id}",
                summary="",
                error_metadata={},
                created_by=from_user,
                created_by_user_id=from_user,
                created_at=now,
                updated_at=now,
                started_at=now,
                review_assignee=from_user,
                review_assignee_user_id=from_user,
            ),
        ]
    )
    session.flush()

    session.add_all(
        [
            QuestionRefactorModel(
                refactor_id=str(refactor_id),
                project_id=str(project_id),
                source_question_id=str(source_question_id),
                replacement_question_id=str(replacement_question_id),
                reason="handoff",
                source_snapshot={},
                replacement_snapshot={},
                relationship_changes={},
                created_by=from_user,
                created_by_user_id=from_user,
                created_at=now,
            ),
            DatasetModel(
                dataset_id=str(dataset_id),
                project_id=str(project_id),
                commit_hash="dataset-hash",
                primary_question_id=str(source_question_id),
                manifest_files=[],
                manifest_external_artifacts=[],
                manifest_metadata={},
                manifest_nwb_metadata={},
                manifest_bids_metadata={},
                manifest_note_ids=[],
                manifest_source_session_id=None,
                status="staged",
                created_by=from_user,
                created_by_user_id=from_user,
                created_at=now,
                updated_at=now,
            ),
            GraphChangeSetModel(
                change_set_id=str(change_set_id),
                project_id=str(project_id),
                source_note_id=str(note_id),
                source_note_ids=[],
                provider="openai",
                model="test-model",
                prompt_version="test",
                draft_mode="graph_context",
                context_packet={},
                summary="",
                uncertain_fields=[],
                clarification_requests=[],
                status="ready",
                error_metadata={},
                created_by=from_user,
                created_by_user_id=from_user,
                created_at=now,
                updated_at=now,
                review_assignee=from_user,
                review_assignee_user_id=from_user,
            ),
            SessionModel(
                session_id=str(session_id),
                project_id=str(project_id),
                session_type="operational",
                status="active",
                primary_question_id=str(source_question_id),
                started_at=now,
                ended_at=None,
                created_by=from_user,
                created_by_user_id=from_user,
                updated_at=now,
            ),
            AnalysisModel(
                analysis_id=str(analysis_id),
                project_id=str(project_id),
                method_hash="method",
                code_version="code",
                environment_hash=None,
                executed_by=from_user,
                executed_by_user_id=from_user,
                executed_at=now,
                status="staged",
                created_at=now,
                updated_at=now,
            ),
            ClaimModel(
                claim_id=str(claim_id),
                project_id=str(project_id),
                statement="Departing scientist claim",
                confidence=80.0,
                status="proposed",
                created_by=from_user,
                created_by_user_id=from_user,
                created_at=now,
                updated_at=now,
            ),
            GoalModel(
                goal_id=str(goal_id),
                project_id=str(project_id),
                goal_type="paper",
                title="Handoff paper",
                summary="",
                status="planned",
                attributes={},
                created_by=from_user,
                created_by_user_id=from_user,
                created_at=now,
                updated_at=now,
            ),
        ]
    )
    session.flush()

    session.add_all(
        [
            VisualizationModel(
                viz_id=str(visualization_id),
                analysis_id=str(analysis_id),
                viz_type="line",
                file_path="outputs/handoff.png",
                caption="Handoff figure",
                created_by=from_user,
                created_by_user_id=from_user,
                created_at=now,
                updated_at=now,
            ),
            GoalLinkModel(
                link_id=str(goal_link_id),
                goal_id=str(goal_id),
                entity_type="project",
                entity_id=str(project_id),
                relation="contributes_to",
                link_status="candidate",
                slot="",
                created_by=from_user,
                created_by_user_id=from_user,
                created_at=now,
            ),
        ]
    )
    session.commit()

    reassignment = api.reassign_ownership(
        from_user_id=from_user_id,
        to_user_id=to_user_id,
        reason="  trainee graduated  ",
        actor=AuthContext(user_id=admin_user_id, role=Role.ADMIN),
    )

    expected_counts = {
        "project_groups": 1,
        "projects": 1,
        "questions": 1,
        "question_refactors": 1,
        "datasets": 1,
        "notes": 1,
        "claims": 1,
        "visualizations": 1,
        "graph_change_sets": 1,
        "graph_change_sets_review_assignee": 1,
        "graph_draft_batch_runs": 1,
        "graph_draft_batch_runs_review_assignee": 1,
        # No settings row names the departing user as default reviewer in this
        # fixture; the surface is still swept (lab-tracker-ul0n.1).
        "graph_draft_batch_settings_default_reviewer": 0,
        "sessions": 1,
        "goals": 1,
        "goal_links": 1,
        "project_memberships": 1,
        "group_memberships": 1,
        "analyses": 1,
    }
    assert reassignment.from_user_id == from_user_id
    assert reassignment.to_user_id == to_user_id
    assert reassignment.reason == "trainee graduated"
    assert reassignment.record_counts == expected_counts
    assert reassignment.total_records == 18
    assert reassignment.created_by == str(admin_user_id)
    assert reassignment.created_by_user_id == admin_user_id

    created_by_rows = [
        session.get(ProjectGroupModel, str(project_group_id)),
        session.get(ProjectModel, str(project_id)),
        session.get(ProjectMembershipModel, str(project_membership_id)),
        session.get(GroupMembershipModel, str(group_membership_id)),
        session.get(QuestionModel, str(source_question_id)),
        session.get(QuestionRefactorModel, str(refactor_id)),
        session.get(DatasetModel, str(dataset_id)),
        session.get(NoteModel, str(note_id)),
        session.get(ClaimModel, str(claim_id)),
        session.get(VisualizationModel, str(visualization_id)),
        session.get(GraphChangeSetModel, str(change_set_id)),
        session.get(GraphDraftBatchRunModel, str(batch_run_id)),
        session.get(SessionModel, str(session_id)),
        session.get(GoalModel, str(goal_id)),
        session.get(GoalLinkModel, str(goal_link_id)),
    ]
    for row in created_by_rows:
        assert row is not None
        assert row.created_by == to_user
        # created_by_user_id is a GUID column on migrated models (returns UUID)
        # and a str on not-yet-migrated ones; compare by string form.
        assert str(row.created_by_user_id) == to_user

    analysis = session.get(AnalysisModel, str(analysis_id))
    assert analysis is not None
    assert analysis.executed_by == to_user
    assert str(analysis.executed_by_user_id) == to_user

    change_set = session.get(GraphChangeSetModel, str(change_set_id))
    assert change_set is not None
    assert change_set.review_assignee == to_user
    assert str(change_set.review_assignee_user_id) == to_user
    batch_run = session.get(GraphDraftBatchRunModel, str(batch_run_id))
    assert batch_run is not None
    assert batch_run.review_assignee == to_user
    assert str(batch_run.review_assignee_user_id) == to_user


def test_ownership_reassignment_route_records_audit_and_requires_admin(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    source_headers, source_user_id = _register_user(client, role=Role.ADMIN)
    _, successor_user_id = _register_user(client, role=Role.VIEWER)
    viewer_headers, _ = _register_user(client, role=Role.VIEWER)
    admin_user_id = client.get("/auth/me", headers=admin_auth_headers).json()["data"]["user_id"]

    project_id = client.post(
        "/projects",
        json={"name": "Ownership route"},
        headers=source_headers,
    ).json()["data"]["project_id"]
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Who owns this record after handoff?",
            "question_type": "descriptive",
        },
        headers=source_headers,
    ).json()["data"]["question_id"]
    dataset_id = client.post(
        "/datasets",
        json={"project_id": project_id, "primary_question_id": question_id},
        headers=source_headers,
    ).json()["data"]["dataset_id"]
    analysis_id = client.post(
        "/analyses",
        json={
            "project_id": project_id,
            "dataset_ids": [dataset_id],
            "method_hash": "method",
            "code_version": "code",
        },
        headers=source_headers,
    ).json()["data"]["analysis_id"]

    denied = client.post(
        "/ownership-reassignments",
        json={"from_user_id": source_user_id, "to_user_id": successor_user_id},
        headers=viewer_headers,
    )
    assert denied.status_code == 401

    self_reassignment = client.post(
        "/ownership-reassignments",
        json={"from_user_id": source_user_id, "to_user_id": source_user_id},
        headers=admin_auth_headers,
    )
    assert self_reassignment.status_code == 422

    missing_source = client.post(
        "/ownership-reassignments",
        json={"from_user_id": str(uuid4()), "to_user_id": successor_user_id},
        headers=admin_auth_headers,
    )
    assert missing_source.status_code == 404

    created = client.post(
        "/ownership-reassignments",
        json={
            "from_user_id": source_user_id,
            "to_user_id": successor_user_id,
            "reason": "Lab member left",
        },
        headers=admin_auth_headers,
    )
    assert created.status_code == 201, created.text
    payload = created.json()["data"]
    reassignment_id = payload["reassignment_id"]
    assert payload["from_user_id"] == source_user_id
    assert payload["to_user_id"] == successor_user_id
    assert payload["reason"] == "Lab member left"
    assert payload["created_by"] == admin_user_id
    assert payload["created_by_user_id"] == admin_user_id
    assert payload["record_counts"]["projects"] == 1
    assert payload["record_counts"]["questions"] == 1
    assert payload["record_counts"]["datasets"] == 1
    assert payload["record_counts"]["analyses"] == 1

    project = client.get(f"/projects/{project_id}", headers=admin_auth_headers)
    question = client.get(f"/questions/{question_id}", headers=admin_auth_headers)
    dataset = client.get(f"/datasets/{dataset_id}", headers=admin_auth_headers)
    analysis = client.get(f"/analyses/{analysis_id}", headers=admin_auth_headers)

    assert project.json()["data"]["created_by_user_id"] == successor_user_id
    assert question.json()["data"]["created_by_user_id"] == successor_user_id
    assert dataset.json()["data"]["created_by_user_id"] == successor_user_id
    assert analysis.json()["data"]["executed_by_user_id"] == successor_user_id

    listed = client.get(
        "/ownership-reassignments",
        params={"from_user_id": source_user_id},
        headers=admin_auth_headers,
    )
    fetched = client.get(
        f"/ownership-reassignments/{reassignment_id}",
        headers=admin_auth_headers,
    )

    assert listed.status_code == 200
    assert [item["reassignment_id"] for item in listed.json()["data"]] == [reassignment_id]
    assert fetched.status_code == 200
    assert fetched.json()["data"]["reassignment_id"] == reassignment_id
