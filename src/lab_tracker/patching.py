"""Shared omitted-versus-null semantics for application PATCH commands."""

from __future__ import annotations

from enum import Enum, auto
from typing import Any, Final, TypeAlias, TypeGuard, TypeVar

from pydantic import BaseModel


class NotProvided(Enum):
    """Typed singleton used when a PATCH field was omitted."""

    VALUE = auto()

    def __repr__(self) -> str:
        return "NOT_PROVIDED"


NOT_PROVIDED: Final = NotProvided.VALUE

T = TypeVar("T")
PatchValue: TypeAlias = T | NotProvided


def is_provided(value: PatchValue[T]) -> TypeGuard[T]:
    """Return whether a patch argument was supplied, including explicit null."""

    return value is not NOT_PROVIDED


def provided_fields(model: BaseModel) -> dict[str, Any]:
    """Return only fields present in the input without dumping nested models."""

    return {
        field_name: getattr(model, field_name)
        for field_name in model.model_fields_set
    }
