"""The reference registry stays complete as the schema grows."""

from __future__ import annotations

import pytest

import lab_tracker.collection_db_models  # noqa: F401 - registers collection tables
import lab_tracker.db_models  # noqa: F401 - registers core tables
from lab_tracker.db import Base
from lab_tracker.errors import ValidationError
from lab_tracker.models import EntityType
from lab_tracker.reference_registry import (
    REFERENCE_REGISTRY,
    BlockingReference,
    DeletableEntity,
    ReferencePolicy,
    ReferenceProbe,
    Referrer,
    deletion_blocked_message,
    ensure_no_blocking_references,
)
from lab_tracker.sqlalchemy_repository_parts.references import implemented_probes

_ENTITY_TABLES = {
    "questions": DeletableEntity.QUESTION,
    "datasets": DeletableEntity.DATASET,
    "notes": DeletableEntity.NOTE,
    "sessions": DeletableEntity.SESSION,
    "analyses": DeletableEntity.ANALYSIS,
    "claims": DeletableEntity.CLAIM,
    "visualizations": DeletableEntity.VISUALIZATION,
    "exploration_nodes": DeletableEntity.EXPLORATION_NODE,
}


def _classified_sources(entity: DeletableEntity) -> set[str]:
    references = REFERENCE_REGISTRY[entity]
    sources = {source for referrer in references.referrers for source in referrer.sources}
    sources.update(child.source for child in references.cascades_to)
    return sources


def test_every_deletable_entity_has_a_registry_entry() -> None:
    assert set(REFERENCE_REGISTRY) == set(DeletableEntity)
    assert set(_ENTITY_TABLES.values()) == set(DeletableEntity)


def test_every_foreign_key_into_a_deletable_table_is_classified() -> None:
    unclassified: list[str] = []
    for table in Base.metadata.tables.values():
        for foreign_key in table.foreign_keys:
            entity = _ENTITY_TABLES.get(foreign_key.column.table.name)
            if entity is None:
                continue
            source = f"{table.name}.{foreign_key.parent.name}"
            if source not in _classified_sources(entity):
                unclassified.append(f"{source} -> {foreign_key.column.table.name}")
    assert unclassified == []


def test_every_polymorphic_reference_table_is_covered_for_every_referencable_type() -> None:
    polymorphic_entities = [
        entity for entity in DeletableEntity if entity.entity_type is not None
    ]
    # Exploration nodes are draftable (EntityType.EXPLORATION_NODE) but no
    # polymorphic reference table may target one, so they are not referencable.
    assert {entity.entity_type for entity in polymorphic_entities} == set(EntityType) - {
        EntityType.PROJECT,
        EntityType.GOAL,
        EntityType.EXPLORATION_NODE,
    }
    for entity in polymorphic_entities:
        probes = {referrer.probe for referrer in REFERENCE_REGISTRY[entity].referrers}
        assert {
            ReferenceProbe.NOTE_TARGETS,
            ReferenceProbe.EXPLORATION_EVIDENCE,
            ReferenceProbe.ACCEPTED_PROVENANCE_LINKS,
            ReferenceProbe.UNACCEPTED_PROVENANCE_LINKS,
            ReferenceProbe.GOAL_LINKS,
        } <= probes, entity


def test_every_probe_is_implemented_and_used() -> None:
    used = {
        referrer.probe
        for references in REFERENCE_REGISTRY.values()
        for referrer in references.referrers
        if referrer.probe is not None
    }
    assert used == set(ReferenceProbe)
    assert implemented_probes() == frozenset(ReferenceProbe)


def test_referrer_policy_shape_is_enforced() -> None:
    with pytest.raises(ValueError):
        Referrer(sources=("t.c",), policy=ReferencePolicy.OWNED, probe=ReferenceProbe.GOAL_LINKS)
    with pytest.raises(ValueError):
        Referrer(sources=("t.c",), policy=ReferencePolicy.BLOCK, probe=ReferenceProbe.GOAL_LINKS)
    with pytest.raises(ValueError):
        Referrer(sources=("t.c",), policy=ReferencePolicy.CLEANUP)


def test_blocked_message_keeps_existing_single_referrer_wording() -> None:
    dataset = REFERENCE_REGISTRY[DeletableEntity.DATASET]
    claims = next(
        referrer
        for referrer in dataset.blocking
        if referrer.probe is ReferenceProbe.CLAIM_DATASETS
    )

    assert (
        deletion_blocked_message(DeletableEntity.DATASET, [BlockingReference(referrer=claims)])
        == "Dataset cannot be deleted while claims reference it."
    )
    ensure_no_blocking_references(DeletableEntity.DATASET, [])
    with pytest.raises(ValidationError, match="claims reference it"):
        ensure_no_blocking_references(
            DeletableEntity.DATASET,
            [BlockingReference(referrer=claims), BlockingReference(referrer=claims)],
        )
