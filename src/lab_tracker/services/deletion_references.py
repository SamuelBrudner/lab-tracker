"""Apply :mod:`lab_tracker.reference_registry` inside a delete transaction."""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from lab_tracker.reference_registry import (
    DeletableEntity,
    ReferenceProbe,
    ensure_no_blocking_references,
    references_for,
)
from lab_tracker.repository import LabTrackerRepository
from lab_tracker.services.goal_link_cleanup import remove_goal_links_to_targets


def ensure_entity_deletable(
    repository: LabTrackerRepository,
    entity: DeletableEntity,
    entity_id: UUID,
    *,
    project_id: UUID,
) -> None:
    """Refuse the delete, naming every blocking referrer, if any still exist.

    Callers hold ``repository.lock_project_references(project_id)`` so the
    answer stays true until their transaction commits.
    """

    ensure_no_blocking_references(
        entity,
        repository.find_blocking_references(entity, entity_id, project_id=project_id),
    )


def remove_cleanup_references(
    repository: LabTrackerRepository,
    entity: DeletableEntity,
    entity_ids: Iterable[UUID],
) -> None:
    """Remove every ``CLEANUP`` referrer of entities about to be deleted."""

    ids = sorted(set(entity_ids), key=str)
    if not ids:
        return
    for referrer in references_for(entity).cleanup:
        if referrer.probe is ReferenceProbe.GOAL_LINKS:
            entity_type = entity.entity_type
            if entity_type is None:
                raise ValueError(f"{entity.value} cannot be a goal-link target.")
            remove_goal_links_to_targets(
                repository,
                {(entity_type, entity_id) for entity_id in ids},
            )
        elif referrer.probe is ReferenceProbe.UNACCEPTED_PROVENANCE_LINKS:
            repository.remove_unaccepted_provenance_links(entity, ids)
        else:
            raise ValueError(f"No cleanup is implemented for {referrer.probe}.")


def prepare_entity_deletion(
    repository: LabTrackerRepository,
    entity: DeletableEntity,
    entity_id: UUID,
    *,
    project_id: UUID,
    cascaded: Iterable[tuple[DeletableEntity, UUID]] = (),
) -> None:
    """Guard one delete, then remove the cleanup referrers it would strand.

    ``cascaded`` lists child entities the delete cascades into (an analysis's
    visualizations); their blocking referrers are already part of the guard,
    and their cleanup referrers are removed here too. Call after
    ``repository.lock_project_references(project_id)``.
    """

    ensure_entity_deletable(repository, entity, entity_id, project_id=project_id)
    remove_cleanup_references(repository, entity, (entity_id,))
    children: dict[DeletableEntity, set[UUID]] = {}
    for child_entity, child_id in cascaded:
        children.setdefault(child_entity, set()).add(child_id)
    for child_entity, child_ids in children.items():
        remove_cleanup_references(repository, child_entity, child_ids)
