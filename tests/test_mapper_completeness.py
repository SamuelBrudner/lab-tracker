"""Completeness guard for the domain<->ORM mapper.

When an ORM column is added but its mapper (``sqlalchemy_mappers.py``) is not
updated, data silently fails to round-trip. This test asserts that every column
of every mapper-handled ORM model is referenced by that model's mapper
functions. Models mapped outside ``sqlalchemy_mappers.py`` are listed in
``_EXTERNAL_MAPPERS``; every ORM model must be guarded or explicitly exempt.

It underpins the TypeDecorator migration recorded in
``docs/domain-orm-mapping-decision.md`` (bd ``lab-tracker-t4x0.2``): as manual
``_uuid``/``.value``/``_as_utc`` conversions are removed, a dropped column would
surface here rather than as silent data loss, and it guards the ongoing
domain/ORM/schema field-add fan-out.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from types import ModuleType
from typing import Any

import pytest

import lab_tracker.collection_db_models as collection_db_models
import lab_tracker.db_models as db_models
from lab_tracker import sqlalchemy_mappers
from lab_tracker.sqlalchemy_mapper_parts import projects as project_mappers
from lab_tracker.sqlalchemy_repository_parts import collections as collection_repository
from lab_tracker.sqlalchemy_repository_parts import (
    graph_batches,
    graph_drafts,
    ownership,
    supervision,
    usage,
)
from lab_tracker.sqlalchemy_repository_parts.core import SQLAlchemyGroupMembershipRepository

_MAPPER_SRC = inspect.getsource(sqlalchemy_mappers)

# ORM model classes the mapper module references (constructs or type-hints).
_HANDLED = sorted(set(re.findall(r"\b([A-Z][A-Za-z]+Model)\b", _MAPPER_SRC)))


def _camel_to_snake(name: str) -> str:
    # AcquisitionOutputModel -> acquisition_output ; NoteTargetModel -> note_target
    name = name[:-5] if name.endswith("Model") else name
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


_MODULE_FUNCS = {
    name: inspect.getsource(obj)
    for name, obj in inspect.getmembers(sqlalchemy_mappers, inspect.isfunction)
    if obj.__module__ == sqlalchemy_mappers.__name__
}


def _mapper_functions_for(model_name: str) -> list[str]:
    """Source of the entity's mapper functions plus any helpers they call.

    Shared columns (e.g. the ``origin*`` provenance fields) are handled by
    common helpers like ``_origin_model_kwargs``; include one level of called
    helpers so those columns count as covered.
    """
    token = _camel_to_snake(model_name)
    selected = {name for name in _MODULE_FUNCS if token in name}
    blob = "\n".join(_MODULE_FUNCS[name] for name in selected)
    for name in _MODULE_FUNCS:
        if name not in selected and re.search(rf"\b{re.escape(name)}\s*\(", blob):
            selected.add(name)
    return [_MODULE_FUNCS[name] for name in selected]


def _handled_models() -> list[type]:
    models = []
    for name in _HANDLED:
        model = getattr(db_models, name, None)
        if model is not None and hasattr(model, "__table__"):
            models.append(model)
    return models


# Columns intentionally not round-tripped by their entity's mapper functions.
# Keyed "ModelName.column" with the reason, so exceptions are explicit and reviewed.
_ALLOW: dict[str, str] = {
    # Link/junction rows are assembled inside their parent entity's mapper
    # (e.g. dataset question links), so they have no dedicated function slice.
}

# Junction/link models whose columns are handled inside a parent entity's
# functions rather than a same-named function slice; skip the per-slice check.
_JUNCTION_MODELS = {
    "AnalysisDatasetModel",
    "ClaimAnalysisModel",
    "ClaimDatasetModel",
    "ClaimQuestionModel",
    "VisualizationClaimModel",
    "QuestionParentModel",
    "ExplorationNodeEdgeModel",
    "NoteTargetModel",
    "DatasetQuestionLinkModel",
}


@pytest.mark.parametrize(
    "model",
    [m for m in _handled_models() if m.__name__ not in _JUNCTION_MODELS],
    ids=lambda m: m.__name__,
)
def test_every_orm_column_is_referenced_by_its_mapper(model: type) -> None:
    funcs = _mapper_functions_for(model.__name__)
    assert funcs, f"no mapper functions found for {model.__name__}"
    blob = "\n".join(funcs)
    missing = [
        col.key
        for col in model.__table__.columns
        if f"{model.__name__}.{col.key}" not in _ALLOW
        and re.search(rf"\b{re.escape(col.key)}\b", blob) is None
    ]
    assert not missing, f"{model.__name__}: columns not referenced by its mapper: {missing}"


# Models whose domain<->ORM mapping lives outside ``sqlalchemy_mappers.py``.
# Each entry lists the functions that build the row and/or rebuild the domain
# object, so a column added to the model but not to these functions fails here.
_EXTERNAL_MAPPERS: dict[str, tuple[Callable[..., Any], ...]] = {
    "ProjectModel": (
        project_mappers.project_to_model,
        project_mappers.project_from_model,
        project_mappers.apply_project_to_model,
    ),
    "ProjectGroupModel": (
        project_mappers.project_group_to_model,
        project_mappers.project_group_from_model,
        project_mappers.apply_project_group_to_model,
    ),
    "GroupMembershipModel": (
        SQLAlchemyGroupMembershipRepository._from_row,
        SQLAlchemyGroupMembershipRepository._to_model,
        SQLAlchemyGroupMembershipRepository._apply_to_model,
    ),
    "GraphDraftBatchSettingsModel": (
        graph_batches.settings_to_model,
        graph_batches.apply_settings_to_model,
        graph_batches.settings_from_model,
    ),
    "GraphDraftBatchRunModel": (
        graph_batches.run_to_model,
        graph_batches.apply_run_to_model,
        graph_batches.run_from_model,
    ),
    "ReviewEmailOutboxModel": (
        graph_batches.email_delivery_to_model,
        graph_batches.apply_email_delivery_to_model,
        graph_batches.email_delivery_from_model,
    ),
    "GraphChangeOperationModel": (
        graph_drafts.operation_to_model,
        graph_drafts.operation_from_model,
    ),
    "GraphChangeSetModel": (
        graph_drafts.change_set_to_model,
        graph_drafts.apply_change_set_to_model,
        graph_drafts.change_set_from_model,
    ),
    "SupervisionEdgeModel": (
        supervision.supervision_edge_to_model,
        supervision.supervision_edge_from_model,
        supervision.apply_supervision_edge_to_model,
    ),
    "UsageEventModel": (usage.usage_event_to_model, usage.usage_event_from_model),
    "UsageEventRollupModel": (
        usage.usage_event_rollup_to_model,
        usage.usage_event_rollup_from_model,
    ),
    "OwnershipReassignmentModel": (ownership.ownership_reassignment_from_model,),
    "RecordExportEventModel": (ownership.record_export_event_from_model,),
    "AcquisitionCollectionModel": (collection_repository._collection_from_row,),
    "AcquisitionCollectionSnapshotModel": (collection_repository._snapshot_from_row,),
    "AcquisitionCollectionCaptureModel": (collection_repository._capture_from_row,),
}

# ORM models with no domain<->ORM mapper to guard. Keyed by model name with the
# reason, so a new model cannot silently join this list.
_UNGUARDED_MODELS: dict[str, str] = {
    "UserModel": "auth.py uses the ORM row directly; there is no domain dataclass.",
    "PersonalAccessTokenModel": "auth.py uses the ORM row directly; no domain dataclass.",
    "DeviceTokenModel": "auth.py uses the ORM row directly; no domain dataclass.",
    "DeviceEnrollmentModel": "auth.py uses the ORM row directly; no domain dataclass.",
    "InvitationModel": "auth.py uses the ORM row directly; no domain dataclass.",
    "DatasetFileModel": (
        "DatasetFile is a deliberate projection (file_id/path/checksum/size) of the row; "
        "file_commands.py owns the row lifecycle."
    ),
    "ExperimentDatasetModel": "Junction row written and read inline by the experiments repository.",
    "ExperimentSessionModel": "Junction row written and read inline by the experiments repository.",
    "AcquisitionCollectionManifestModel": (
        "Immutable manifest blob stored and read column-by-column in save_manifest/get_manifest."
    ),
}


def _module_helpers(module: ModuleType) -> dict[str, str]:
    return {
        name: inspect.getsource(obj)
        for name, obj in inspect.getmembers(module, inspect.isfunction)
        if obj.__module__ == module.__name__
    }


def _external_mapper_source(functions: tuple[Callable[..., Any], ...]) -> str:
    """Source of the listed functions plus one level of same-module helpers."""
    sources = [inspect.getsource(function) for function in functions]
    blob = "\n".join(sources)
    for function in functions:
        module = inspect.getmodule(function)
        assert module is not None
        for name, source in _module_helpers(module).items():
            if source not in sources and re.search(rf"\b{re.escape(name)}\s*\(", blob):
                sources.append(source)
    return "\n".join(sources)


def _all_orm_models() -> dict[str, type]:
    return {mapper.class_.__name__: mapper.class_ for mapper in db_models.Base.registry.mappers}


def test_every_orm_model_is_guarded_or_explicitly_unguarded() -> None:
    # Import side effect: collection models register on the shared Base.
    assert collection_db_models.AcquisitionCollectionModel.__name__ in _all_orm_models()
    guarded = {model.__name__ for model in _handled_models()} | set(_EXTERNAL_MAPPERS)
    unaccounted = sorted(set(_all_orm_models()) - guarded - set(_UNGUARDED_MODELS))
    assert not unaccounted, (
        "ORM models without a mapper-completeness guard; add them to _EXTERNAL_MAPPERS "
        f"or, with a reason, to _UNGUARDED_MODELS: {unaccounted}"
    )
    stale = sorted((set(_EXTERNAL_MAPPERS) | set(_UNGUARDED_MODELS)) - set(_all_orm_models()))
    assert not stale, f"Registry names models that no longer exist: {stale}"
    overlap = sorted(set(_UNGUARDED_MODELS) & guarded)
    assert not overlap, f"Models both guarded and marked unguarded: {overlap}"


@pytest.mark.parametrize("model_name", sorted(_EXTERNAL_MAPPERS))
def test_every_orm_column_is_referenced_by_its_external_mapper(model_name: str) -> None:
    model = _all_orm_models()[model_name]
    blob = _external_mapper_source(_EXTERNAL_MAPPERS[model_name])
    missing = [
        col.key
        for col in model.__table__.columns
        if f"{model_name}.{col.key}" not in _ALLOW
        and re.search(rf"\b{re.escape(col.key)}\b", blob) is None
    ]
    assert not missing, f"{model_name}: columns not referenced by its mapper: {missing}"
