from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from api_helpers import repository_backed_api
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from lab_tracker.auth import AuthContext, Role
from lab_tracker.db_models import ExperimentDatasetModel, ExperimentSessionModel
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    Experiment,
    QuestionLink,
    QuestionLinkRole,
    QuestionStatus,
    QuestionType,
    utc_now,
)
from lab_tracker.sqlalchemy_repository_parts.experiments import (
    SQLAlchemyExperimentRepository,
)


def test_membership_puts_ignore_a_concurrent_winner(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = client.post(
        "/projects",
        json={"name": "Experiment membership race"},
        headers=admin_auth_headers,
    )
    project_id = project.json()["data"]["project_id"]
    question = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Are membership PUTs idempotent under a stale read?",
            "question_type": "method_dev",
            "status": "active",
        },
        headers=admin_auth_headers,
    )
    question_id = question.json()["data"]["question_id"]
    experiment = client.post(
        "/experiments",
        json={
            "project_id": project_id,
            "name": "Concurrent membership",
            "primary_question_id": question_id,
        },
        headers=admin_auth_headers,
    ).json()["data"]
    session = client.post(
        "/sessions",
        json={"project_id": project_id, "session_type": "operational"},
        headers=admin_auth_headers,
    ).json()["data"]
    dataset = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": question_id,
        },
        headers=admin_auth_headers,
    ).json()["data"]

    session_path = f"/experiments/{experiment['experiment_id']}/sessions/{session['session_id']}"
    dataset_path = f"/experiments/{experiment['experiment_id']}/datasets/{dataset['dataset_id']}"
    assert client.put(session_path, headers=admin_auth_headers).status_code == 200
    assert client.put(dataset_path, headers=admin_auth_headers).status_code == 200

    monkeypatch.setattr(
        SQLAlchemyExperimentRepository,
        "has_session",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        SQLAlchemyExperimentRepository,
        "has_dataset",
        lambda *_args, **_kwargs: False,
    )
    assert client.put(session_path, headers=admin_auth_headers).status_code == 200
    assert client.put(dataset_path, headers=admin_auth_headers).status_code == 200

    with client.app.state.db_session_factory() as db_session:
        assert db_session.scalar(select(func.count()).select_from(ExperimentSessionModel)) == 1
        assert db_session.scalar(select(func.count()).select_from(ExperimentDatasetModel)) == 1


def _dataset_race_records():
    api = repository_backed_api()
    actor = AuthContext(user_id=UUID(int=1), role=Role.ADMIN)
    project = api.create_project("Dataset membership race", actor=actor)
    first_question = api.create_question(
        project.project_id,
        "Which question is initially linked?",
        QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    second_question = api.create_question(
        project.project_id,
        "Which question does the Experiment require?",
        QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    experiment = api.create_experiment(
        project_id=project.project_id,
        name="Concurrent Dataset parent",
        primary_question_id=second_question.question_id,
        actor=actor,
    )
    dataset = api.create_dataset(
        project.project_id,
        first_question.question_id,
        secondary_question_ids=[second_question.question_id],
        actor=actor,
    )
    return api, actor, project, first_question, second_question, experiment, dataset


def test_dataset_membership_add_reloads_dataset_after_its_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, actor, _project, _first_question, _second_question, experiment, dataset = (
        _dataset_race_records()
    )
    repository = api.experiments.repository
    original_lock = repository.lock_dataset_updates
    unrelated = api.create_question(
        experiment.project_id,
        "Which unrelated question wins the race?",
        QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )

    def concurrent_question_link_update(project_id, dataset_ids) -> None:
        original_lock(project_id, dataset_ids)
        changed = repository.datasets.get(dataset.dataset_id)
        assert changed is not None
        changed.primary_question_id = unrelated.question_id
        changed.question_links = [
            QuestionLink(
                question_id=unrelated.question_id,
                role=QuestionLinkRole.PRIMARY,
            )
        ]
        repository.datasets.save(changed)

    monkeypatch.setattr(
        repository,
        "lock_dataset_updates",
        concurrent_question_link_update,
    )

    with pytest.raises(
        ValidationError,
        match="Dataset must link the Experiment primary question",
    ):
        api.experiments.add_dataset(
            experiment.experiment_id,
            dataset.dataset_id,
            actor=actor,
        )


def test_dataset_question_update_rechecks_membership_after_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, actor, _project, first_question, _second_question, experiment, dataset = (
        _dataset_race_records()
    )
    repository = api.datasets.repository
    original_lock = repository.lock_dataset_updates

    def concurrent_membership_add(project_id, dataset_ids) -> None:
        repository.add_experiment_dataset(
            experiment_id=experiment.experiment_id,
            dataset_id=dataset.dataset_id,
            created_by=str(actor.user_id),
            created_by_user_id=None,
            created_at=utc_now(),
        )
        original_lock(project_id, dataset_ids)

    monkeypatch.setattr(
        repository,
        "lock_dataset_updates",
        concurrent_membership_add,
    )

    with pytest.raises(
        ValidationError,
        match="Dataset must retain every parent Experiment question",
    ):
        api.datasets.update_dataset(
            dataset.dataset_id,
            question_links=[
                QuestionLink(
                    question_id=first_question.question_id,
                    role=QuestionLinkRole.PRIMARY,
                )
            ],
            actor=actor,
        )


def test_dataset_delete_rechecks_membership_after_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, actor, _project, _first_question, _second_question, experiment, dataset = (
        _dataset_race_records()
    )
    repository = api.datasets.repository
    original_lock = repository.lock_dataset_updates

    def concurrent_membership_add(project_id, dataset_ids) -> None:
        repository.add_experiment_dataset(
            experiment_id=experiment.experiment_id,
            dataset_id=dataset.dataset_id,
            created_by=str(actor.user_id),
            created_by_user_id=None,
            created_at=utc_now(),
        )
        original_lock(project_id, dataset_ids)

    monkeypatch.setattr(
        repository,
        "lock_dataset_updates",
        concurrent_membership_add,
    )

    with pytest.raises(
        ValidationError,
        match="Dataset cannot be deleted while Experiments reference it",
    ):
        api.datasets.delete_dataset(dataset.dataset_id, actor=actor)


def test_question_delete_rechecks_experiments_after_project_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = repository_backed_api()
    actor = AuthContext(user_id=UUID(int=1), role=Role.ADMIN)
    project = api.create_project("Question delete race", actor=actor)
    question = api.create_question(
        project.project_id,
        "Can an Experiment appear while this question is deleted?",
        QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    repository = api.questions.repository
    original_lock = repository.lock_project_question_dag

    def concurrent_experiment_create(project_id: UUID) -> None:
        original_lock(project_id)
        repository.experiments.save(
            Experiment(
                experiment_id=uuid4(),
                project_id=project_id,
                name="Concurrent Experiment",
                primary_question_id=question.question_id,
            )
        )

    monkeypatch.setattr(
        repository,
        "lock_project_question_dag",
        concurrent_experiment_create,
    )

    with pytest.raises(
        ValidationError,
        match="Question cannot be deleted while Experiments use it",
    ):
        api.questions.delete_question(question.question_id, actor=actor)
