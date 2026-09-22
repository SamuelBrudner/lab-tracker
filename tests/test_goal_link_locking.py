"""Goal-link writes take the project reference lock and re-read under it (M58).

A question delete removes the goal links naming it under
``lock_project_references``; goal writers must take the same lock, re-read the
goal and re-verify every link target before saving, or a link to a target
whose delete is in flight commits as a dangling row. The interleaving tests
patch the lock so the competing write lands exactly when a concurrent
transaction holding the lock would commit (see
``test_postgres_reference_locking.py`` for the real PostgreSQL races).
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID, uuid4

import pytest
from api_helpers import repository_backed_api

from lab_tracker.auth import AuthContext, Role
from lab_tracker.errors import ConflictError, NotFoundError
from lab_tracker.models import (
    EntityRef,
    EntityType,
    Goal,
    GoalLink,
    GoalLinkStatus,
    GoalRelation,
    GoalType,
    QuestionType,
)
from lab_tracker.services.goal_service import GoalLinkSpec


def _actor() -> AuthContext:
    return AuthContext(user_id=UUID(int=1), role=Role.ADMIN)


def _question_ref(question_id: UUID) -> EntityRef:
    return EntityRef(entity_type=EntityType.QUESTION, entity_id=question_id)


def _project_ref(project_id: UUID) -> EntityRef:
    return EntityRef(entity_type=EntityType.PROJECT, entity_id=project_id)


def _setup(api):  # noqa: ANN001, ANN202
    actor = _actor()
    project = api.create_project("Goal link locking", actor=actor)
    questions = [
        api.create_question(
            project.project_id,
            f"Question {index}?",
            QuestionType.DESCRIPTIVE,
            actor=actor,
        )
        for index in range(2)
    ]
    goal = api.create_goal(
        project.project_id,
        goal_type=GoalType.PAPER,
        title="Paper",
        actor=actor,
    )
    return actor, project, questions, goal


def _record_reference_locks(
    monkeypatch: pytest.MonkeyPatch,
    api,  # noqa: ANN001
) -> list[UUID]:
    repository = api.goals.repository
    original = repository.lock_project_references
    calls: list[UUID] = []

    def record(project_id: UUID) -> None:
        calls.append(project_id)
        original(project_id)

    monkeypatch.setattr(repository, "lock_project_references", record)
    return calls


def _on_first_reference_lock(
    monkeypatch: pytest.MonkeyPatch,
    api,  # noqa: ANN001
    competing_write: Callable[[], None],
) -> None:
    """Commit ``competing_write`` as the lock holder the writer waited for."""

    repository = api.goals.repository
    original = repository.lock_project_references
    fired: list[bool] = []

    def lock_after_competitor(project_id: UUID) -> None:
        original(project_id)
        if not fired:
            fired.append(True)
            competing_write()

    monkeypatch.setattr(repository, "lock_project_references", lock_after_competitor)


def test_goal_link_writers_take_the_project_reference_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = repository_backed_api()
    actor, project, (first, second), goal = _setup(api)
    linked = api.link_node_to_goal(
        goal.goal_id,
        target=_question_ref(first.question_id),
        relation=GoalRelation.ADDRESSES,
        actor=actor,
    )
    calls = _record_reference_locks(monkeypatch, api)

    for write in (
        lambda: api.create_goal(
            project.project_id,
            goal_type=GoalType.PAPER,
            title="Linked at creation",
            links=[
                GoalLinkSpec(
                    target=_question_ref(first.question_id),
                    relation=GoalRelation.ADDRESSES,
                )
            ],
            actor=actor,
        ),
        lambda: api.update_goal(
            goal.goal_id,
            links=[
                GoalLinkSpec(
                    target=_question_ref(second.question_id),
                    relation=GoalRelation.ADDRESSES,
                )
            ],
            actor=actor,
        ),
        lambda: api.link_node_to_goal(
            goal.goal_id,
            target=_question_ref(second.question_id),
            relation=GoalRelation.SUPPORTING_EVIDENCE,
            actor=actor,
        ),
        lambda: api.update_goal_link(
            goal.goal_id,
            linked.link_id,
            link_status=GoalLinkStatus.COMMITTED,
            actor=actor,
        ),
        lambda: api.delete_goal_link(goal.goal_id, linked.link_id, actor=actor),
    ):
        calls.clear()
        write()
        assert calls == [project.project_id]


@pytest.mark.parametrize("write", ["create_goal", "update_goal", "link_node_to_goal"])
def test_goal_link_to_a_target_deleted_while_waiting_for_the_lock_fails(
    monkeypatch: pytest.MonkeyPatch,
    write: str,
) -> None:
    api = repository_backed_api()
    actor, project, (question, _), goal = _setup(api)
    _on_first_reference_lock(
        monkeypatch,
        api,
        lambda: api.delete_question(question.question_id, actor=actor),
    )
    link = GoalLinkSpec(target=_question_ref(question.question_id), relation=GoalRelation.ADDRESSES)
    writes: dict[str, Callable[[], object]] = {
        "create_goal": lambda: api.create_goal(
            project.project_id,
            goal_type=GoalType.PAPER,
            title="Linked at creation",
            links=[link],
            actor=actor,
        ),
        "update_goal": lambda: api.update_goal(goal.goal_id, links=[link], actor=actor),
        "link_node_to_goal": lambda: api.link_node_to_goal(
            goal.goal_id,
            target=link.target,
            relation=link.relation,
            actor=actor,
        ),
    }

    with pytest.raises(NotFoundError, match="^Question does not exist.$"):
        writes[write]()

    goals, _ = api.goals.repository.query_goals(project_id=project.project_id)
    assert [goal.links for goal in goals] == [[]]


def test_goal_link_write_rereads_the_goal_under_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = repository_backed_api()
    actor, _project, (first, second), goal = _setup(api)
    concurrent_link = GoalLink(
        link_id=uuid4(),
        goal_id=goal.goal_id,
        target=_question_ref(first.question_id),
        relation=GoalRelation.ADDRESSES,
        link_status=GoalLinkStatus.CANDIDATE,
    )

    def concurrent_link_commits() -> None:
        latest = api.get_goal(goal.goal_id)
        latest.links.append(concurrent_link)
        api.goals.repository.goals.save(latest)

    _on_first_reference_lock(monkeypatch, api, concurrent_link_commits)

    added = api.link_node_to_goal(
        goal.goal_id,
        target=_question_ref(second.question_id),
        relation=GoalRelation.ADDRESSES,
        actor=actor,
    )

    links = api.get_goal(goal.goal_id).links
    assert {link.link_id for link in links} == {concurrent_link.link_id, added.link_id}


def test_goal_write_rejects_a_scope_that_grew_while_waiting_for_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = repository_backed_api()
    actor = _actor()
    home = api.create_project("Home", actor=actor)
    other = api.create_project("Other", actor=actor)
    goal = api.create_goal(
        None,
        goal_type=GoalType.PAPER,
        title="Cross-project paper",
        links=[
            GoalLinkSpec(target=_project_ref(home.project_id), relation=GoalRelation.CONTRIBUTES_TO)
        ],
        actor=actor,
    )

    def concurrent_link_to_other_project() -> None:
        latest: Goal = api.get_goal(goal.goal_id)
        latest.links.append(
            GoalLink(
                link_id=uuid4(),
                goal_id=goal.goal_id,
                target=_project_ref(other.project_id),
                relation=GoalRelation.CONTRIBUTES_TO,
                link_status=GoalLinkStatus.CANDIDATE,
            )
        )
        api.goals.repository.goals.save(latest)

    _on_first_reference_lock(monkeypatch, api, concurrent_link_to_other_project)

    with pytest.raises(ConflictError, match="changed while waiting"):
        api.update_goal(goal.goal_id, title="Renamed", actor=actor)
