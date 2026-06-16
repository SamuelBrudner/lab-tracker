"""Per-entity version history service."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel

from lab_tracker.auth import AuthContext
from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.models import EntityType, EntityVersion, EntityVersionDiff, utc_now
from lab_tracker.repository import LabTrackerRepository
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.shared import actor_user_fk, actor_user_id

VERSIONED_ENTITY_TYPES = {EntityType.QUESTION, EntityType.CLAIM}


class EntityVersionService(BaseService):
    def __init__(self, context: ServiceContext) -> None:
        super().__init__(context)

    def record_entity_version(
        self,
        repository: LabTrackerRepository,
        *,
        entity_type: EntityType,
        entity_id: UUID,
        entity: BaseModel,
        actor: AuthContext | None = None,
    ) -> EntityVersion:
        _ensure_versioned_entity_type(entity_type)
        versions, _ = repository.query_entity_versions(
            entity_type=entity_type.value,
            entity_id=entity_id,
            limit=None,
            offset=0,
        )
        snapshot = entity.model_dump(mode="json")
        if versions and versions[-1].snapshot == snapshot:
            return versions[-1]
        change_set_id = _entity_change_set_id(entity)
        committed_at = None
        if change_set_id is not None:
            change_set = repository.graph_change_sets.get(change_set_id)
            if change_set is not None:
                committed_at = change_set.committed_at
        version = EntityVersion(
            version_id=uuid4(),
            entity_type=entity_type,
            entity_id=entity_id,
            version_number=(versions[-1].version_number + 1 if versions else 1),
            snapshot=snapshot,
            change_set_id=change_set_id,
            committed_at=committed_at,
            created_at=utc_now(),
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, repository),
        )
        repository.entity_versions.save(version)
        return version

    def list_entity_versions(
        self,
        *,
        entity_type: EntityType,
        entity_id: UUID,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[EntityVersion]:
        _ensure_versioned_entity_type(entity_type)
        versions, _ = self.repository.query_entity_versions(
            entity_type=entity_type.value,
            entity_id=entity_id,
            limit=limit,
            offset=offset,
        )
        return versions

    def diff_entity_versions(
        self,
        *,
        entity_type: EntityType,
        entity_id: UUID,
        from_version: int,
        to_version: int,
    ) -> EntityVersionDiff:
        _ensure_versioned_entity_type(entity_type)
        if from_version < 1 or to_version < 1:
            raise ValidationError("Version numbers must be greater than zero.")
        versions = self.list_entity_versions(entity_type=entity_type, entity_id=entity_id)
        by_number = {version.version_number: version for version in versions}
        source = by_number.get(from_version)
        target = by_number.get(to_version)
        if source is None or target is None:
            raise NotFoundError("Entity version does not exist.")
        return EntityVersionDiff(
            entity_type=entity_type,
            entity_id=entity_id,
            from_version=from_version,
            to_version=to_version,
            changed_fields=_top_level_snapshot_diff(source.snapshot, target.snapshot),
        )

    def mark_change_set_committed(
        self,
        change_set_id: UUID,
        committed_at: datetime | None,
    ) -> None:
        versions = self.query_from_repository(
            loader=lambda repository: repository.query_entity_versions(
                change_set_id=change_set_id,
                limit=None,
                offset=0,
            )
        )
        if not versions:
            return
        with self.unit_of_work() as repository:
            for version in versions:
                if version.committed_at is None:
                    version.committed_at = committed_at
                    repository.entity_versions.save(version)


def _entity_change_set_id(entity: BaseModel) -> UUID | None:
    value = getattr(entity, "change_set_id", None)
    return value if isinstance(value, UUID) else None


def _ensure_versioned_entity_type(entity_type: EntityType) -> None:
    if entity_type not in VERSIONED_ENTITY_TYPES:
        raise ValidationError("Entity version history is available for questions and claims.")


def _top_level_snapshot_diff(
    source: dict[str, Any],
    target: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    changed: dict[str, dict[str, Any]] = {}
    for key in sorted(set(source) | set(target)):
        before = source.get(key)
        after = target.get(key)
        if before != after:
            changed[key] = {"before": before, "after": after}
    return changed
