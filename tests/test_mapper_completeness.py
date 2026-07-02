"""Completeness guard for the domain<->ORM mapper.

When an ORM column is added but its mapper (``sqlalchemy_mappers.py``) is not
updated, data silently fails to round-trip. This test asserts that every column
of every mapper-handled ORM model is referenced by that model's mapper
functions.

It underpins the TypeDecorator migration recorded in
``docs/domain-orm-mapping-decision.md`` (bd ``lab-tracker-t4x0.2``): as manual
``_uuid``/``.value``/``_as_utc`` conversions are removed, a dropped column would
surface here rather than as silent data loss, and it guards the ongoing
domain/ORM/schema field-add fan-out.
"""

from __future__ import annotations

import inspect
import re

import pytest

import lab_tracker.db_models as db_models
from lab_tracker import sqlalchemy_mappers

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
