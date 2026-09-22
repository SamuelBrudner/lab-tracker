from __future__ import annotations

import re
from datetime import datetime, timezone
from uuid import UUID, uuid4

from api_helpers import repository_backed_api
from fastapi.testclient import TestClient
from sqlalchemy import func, or_, select

from lab_tracker.auth import AuthContext, Role
from lab_tracker.db_models import (
    AnalysisModel,
    Base,
    ClaimEdgeModel,
    ClaimModel,
    DatasetModel,
    DataStoreModel,
    EntityVersionModel,
    ExperimentDatasetModel,
    ExperimentModel,
    ExperimentSessionModel,
    ExplorationNodeModel,
    GoalLinkModel,
    GoalModel,
    GraphChangeSetModel,
    GraphDraftBatchRunModel,
    GroupMembershipModel,
    NoteModel,
    ProjectGroupModel,
    ProjectMembershipModel,
    ProjectModel,
    ProvenanceLinkModel,
    QuestionModel,
    QuestionRefactorModel,
    SessionModel,
    UserModel,
    VisualizationModel,
)
from lab_tracker.sqlalchemy_repository_parts.ownership import (
    ATTRIBUTION_REASSIGNMENT_TARGETS,
    USER_COLUMNS_NOT_REASSIGNED,
)

# Column names that identify a user: foreign keys to users are found
# structurally; these catch free-text or un-keyed attribution columns.
_USER_REFERENCE_COLUMN_NAME = re.compile(r"(^|_)(by|user_id|assignee)$")


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
    successor_claim_id = uuid4()
    claim_edge_id = uuid4()
    experiment_id = uuid4()
    entity_version_id = uuid4()
    provenance_link_id = uuid4()
    exploration_node_id = uuid4()
    data_store_id = uuid4()
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
            ClaimModel(
                claim_id=str(successor_claim_id),
                project_id=str(project_id),
                statement="Successor scientist claim",
                confidence=60.0,
                status="proposed",
                created_by=to_user,
                created_by_user_id=to_user,
                created_at=now,
                updated_at=now,
            ),
            ExperimentModel(
                experiment_id=str(experiment_id),
                project_id=str(project_id),
                name="Handoff experiment",
                description="",
                primary_question_id=str(source_question_id),
                status="active",
                origin="user",
                created_by=from_user,
                created_by_user_id=from_user,
                created_at=now,
                updated_at=now,
            ),
            EntityVersionModel(
                version_id=str(entity_version_id),
                entity_type="question",
                entity_id=str(source_question_id),
                version_number=1,
                snapshot={},
                created_by=from_user,
                created_by_user_id=from_user,
                created_at=now,
            ),
            ProvenanceLinkModel(
                link_id=str(provenance_link_id),
                project_id=str(project_id),
                source_entity_type="dataset",
                source_entity_id=str(dataset_id),
                target_entity_type="question",
                target_entity_id=str(source_question_id),
                relation="used",
                basis="content_hash_match",
                status="proposed",
                origin="system_detected",
                created_by=from_user,
                created_by_user_id=from_user,
                created_at=now,
                updated_at=now,
            ),
            ExplorationNodeModel(
                node_id=str(exploration_node_id),
                project_id=str(project_id),
                node_type="decision",
                title="Chose the successor pipeline",
                target_entity_type="question",
                target_entity_id=str(source_question_id),
                status="staged",
                origin="user",
                created_by=from_user,
                created_by_user_id=from_user,
                created_at=now,
                updated_at=now,
            ),
            DataStoreModel(
                store_id=str(data_store_id),
                project_id=str(project_id),
                name="handoff-store",
                kind="local_fs",
                capabilities=[],
                root="/data/handoff",
                is_default=False,
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
            ClaimEdgeModel(
                edge_id=str(claim_edge_id),
                claim_id=str(claim_id),
                target_claim_id=str(successor_claim_id),
                relation="extends",
                created_by=from_user,
                created_by_user_id=from_user,
                created_at=now,
            ),
            ExperimentSessionModel(
                experiment_id=str(experiment_id),
                session_id=str(session_id),
                created_by=from_user,
                created_by_user_id=from_user,
                created_at=now,
            ),
            ExperimentDatasetModel(
                experiment_id=str(experiment_id),
                dataset_id=str(dataset_id),
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

    expected_counts = {target.label: 1 for target in ATTRIBUTION_REASSIGNMENT_TARGETS}
    assert {
        "experiments",
        "experiment_sessions",
        "experiment_datasets",
        "entity_versions",
        "claim_edges",
        "provenance_links",
        "exploration_nodes",
        "data_stores",
    } <= set(expected_counts)
    assert reassignment.from_user_id == from_user_id
    assert reassignment.to_user_id == to_user_id
    assert reassignment.reason == "trainee graduated"
    assert reassignment.record_counts == expected_counts
    assert reassignment.total_records == len(expected_counts)
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

    for target in ATTRIBUTION_REASSIGNMENT_TARGETS:
        remaining = session.scalar(
            select(func.count())
            .select_from(target.model)
            .where(
                or_(
                    target.text_column == from_user,
                    target.user_id_column == from_user,
                )
            )
        )
        assert remaining == 0, f"{target.label} still attributed to the departing user"


def _user_reference_columns() -> set[tuple[str, str]]:
    discovered: set[tuple[str, str]] = set()
    for table in Base.metadata.sorted_tables:
        if table.name == "users":
            continue
        for column in table.columns:
            references_users = any(
                foreign_key.column.table.name == "users" for foreign_key in column.foreign_keys
            )
            if references_users or _USER_REFERENCE_COLUMN_NAME.search(column.name):
                discovered.add((table.name, column.name))
    return discovered


def test_every_user_reference_column_is_classified_for_reassignment():
    """Adding a user/attribution column must decide whether reassignment moves it."""

    reassigned = {
        (target.label, column.key)
        for target in ATTRIBUTION_REASSIGNMENT_TARGETS
        for column in (target.text_column, target.user_id_column)
    }
    excluded = set(USER_COLUMNS_NOT_REASSIGNED)
    discovered = _user_reference_columns()

    assert not reassigned & excluded
    assert all(reason.strip() for reason in USER_COLUMNS_NOT_REASSIGNED.values())
    assert excluded - discovered == set(), "stale USER_COLUMNS_NOT_REASSIGNED entries"
    unclassified = discovered - reassigned - excluded
    assert unclassified == set(), (
        "Classify these user-reference columns: add them to an attribution pair "
        "reassigned by ownership reassignment or to USER_COLUMNS_NOT_REASSIGNED "
        f"with a reason: {sorted(unclassified)}"
    )
    assert {target.label for target in ATTRIBUTION_REASSIGNMENT_TARGETS} >= {
        "experiments",
        "experiment_sessions",
        "experiment_datasets",
        "entity_versions",
        "claim_edges",
        "provenance_links",
        "exploration_nodes",
        "data_stores",
        "analyses",
    }


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
    assert denied.status_code == 403

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


def test_ownership_reassignment_route_moves_experiments(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    source_headers, source_user_id = _register_user(client, role=Role.ADMIN)
    _, successor_user_id = _register_user(client, role=Role.VIEWER)
    project_id = client.post(
        "/projects",
        json={"name": "Experiment handoff"},
        headers=source_headers,
    ).json()["data"]["project_id"]
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Which experiment survives the handoff?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=source_headers,
    ).json()["data"]["question_id"]
    experiment = client.post(
        "/experiments",
        json={
            "project_id": project_id,
            "name": "Handoff experiment",
            "primary_question_id": question_id,
        },
        headers=source_headers,
    )
    assert experiment.status_code == 201, experiment.text
    experiment_id = experiment.json()["data"]["experiment_id"]

    created = client.post(
        "/ownership-reassignments",
        json={"from_user_id": source_user_id, "to_user_id": successor_user_id},
        headers=admin_auth_headers,
    )

    assert created.status_code == 201, created.text
    assert created.json()["data"]["record_counts"]["experiments"] == 1
    moved = client.get(f"/experiments/{experiment_id}", headers=admin_auth_headers)
    assert moved.status_code == 200, moved.text
    assert moved.json()["data"]["created_by"] == successor_user_id
    assert moved.json()["data"]["created_by_user_id"] == successor_user_id
