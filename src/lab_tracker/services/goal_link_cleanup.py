"""Helpers for pruning goal links when target entities are deleted."""

from __future__ import annotations

from uuid import UUID

from lab_tracker.models import EntityType, Goal, GoalLink, utc_now
from lab_tracker.repository import LabTrackerRepository

TargetKey = tuple[EntityType, UUID]


def remove_goal_links_to_entity(
    repository: LabTrackerRepository,
    *,
    entity_type: EntityType,
    entity_id: UUID,
) -> None:
    """Remove goal links whose target entity is about to be deleted."""
    remove_goal_links_to_targets(repository, {(entity_type, entity_id)})


def remove_goal_links_to_project_contents(
    repository: LabTrackerRepository,
    *,
    project_id: UUID,
) -> None:
    """Remove goal links to a project and entities that project deletion cascades."""
    targets: set[TargetKey] = {(EntityType.PROJECT, project_id)}

    questions, _ = repository.query_questions(project_id=project_id, limit=None, offset=0)
    datasets, _ = repository.query_datasets(project_id=project_id, limit=None, offset=0)
    notes, _ = repository.query_notes(project_id=project_id, limit=None, offset=0)
    sessions, _ = repository.query_sessions(project_id=project_id, limit=None, offset=0)
    analyses, _ = repository.query_analyses(project_id=project_id, limit=None, offset=0)
    claims, _ = repository.query_claims(project_id=project_id, limit=None, offset=0)
    visualizations, _ = repository.query_visualizations(
        project_id=project_id,
        limit=None,
        offset=0,
    )

    targets.update((EntityType.QUESTION, question.question_id) for question in questions)
    targets.update((EntityType.DATASET, dataset.dataset_id) for dataset in datasets)
    targets.update((EntityType.NOTE, note.note_id) for note in notes)
    targets.update((EntityType.SESSION, session.session_id) for session in sessions)
    targets.update((EntityType.ANALYSIS, analysis.analysis_id) for analysis in analyses)
    targets.update((EntityType.CLAIM, claim.claim_id) for claim in claims)
    targets.update(
        (EntityType.VISUALIZATION, visualization.viz_id)
        for visualization in visualizations
    )

    remove_goal_links_to_targets(repository, targets)


def remove_goal_links_to_targets(
    repository: LabTrackerRepository,
    targets: set[TargetKey],
) -> None:
    """Remove goal links for all target keys, preserving goal scope invariants."""
    if not targets:
        return

    target_keys = {(entity_type.value, entity_id) for entity_type, entity_id in targets}
    goals, _ = repository.query_goals(
        target_entity_keys=target_keys,
        limit=None,
        offset=0,
    )
    affected_goals = {goal.goal_id: goal for goal in goals}

    for goal in affected_goals.values():
        remaining_links = [
            link for link in goal.links if _target_key(link) not in targets
        ]
        if len(remaining_links) == len(goal.links):
            continue
        if _projectless_goal_would_lose_scope(goal, remaining_links):
            repository.goals.delete(goal.goal_id)
            continue
        goal.links = remaining_links
        goal.updated_at = utc_now()
        repository.goals.save(goal)


def _target_key(link: GoalLink) -> TargetKey:
    return link.target.entity_type, link.target.entity_id


def _projectless_goal_would_lose_scope(goal: Goal, links: list[GoalLink]) -> bool:
    return goal.project_id is None and not any(
        link.target.entity_type == EntityType.PROJECT for link in links
    )
