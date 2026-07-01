"""Custom SQLAlchemy column types that move UUID / enum / UTC-datetime
adaptation into the storage layer.

Historically the domain<->ORM mapper (``sqlalchemy_mappers.py``) hand-converted
every crossing: ~222 ``UUID<->str``, ~86 ``enum<->str``, and ~38 tz
normalisations, purely because IDs and enums were stored as raw ``String`` and
timestamps as naive ``DateTime``. These ``TypeDecorator``s let the ORM attribute
carry the rich Python type (``UUID`` / ``Enum`` / aware ``datetime``) while the
on-disk representation stays byte-identical, so no data migration is needed.

Each decorator is deliberately *liberal on input* (accepts the rich type or its
raw form) and *typed on output*, so existing query sites that pass ``str(id)``
or ``SomeEnum.X.value`` keep working during and after the migration.

See ``docs/domain-orm-mapping-decision.md`` (bd ``lab-tracker-t4x0.2``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, String
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator


class GUID(TypeDecorator):
    """UUID stored as its 36-character string form.

    Storage is identical to ``String(36)`` (no migration). Accepts a ``UUID`` or
    a ``str`` when binding; always returns a ``UUID`` when loading.
    """

    impl = String(36)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, UUID):
            return str(value)
        return str(UUID(str(value)))

    def process_result_value(self, value: Any, dialect: Dialect) -> UUID | None:
        if value is None:
            return None
        if isinstance(value, UUID):
            return value
        return UUID(str(value))


class UtcDateTime(TypeDecorator):
    """Timezone-aware UTC datetime.

    Wraps ``DateTime(timezone=True)`` (no migration) and normalises values that
    come back naive (e.g. from SQLite) to aware UTC, so callers never handle a
    naive datetime and the mapper no longer needs per-field ``_as_utc`` calls.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    @staticmethod
    def _to_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_bind_param(self, value: Any, dialect: Dialect) -> datetime | None:
        return None if value is None else self._to_utc(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        return None if value is None else self._to_utc(value)


class EnumType(TypeDecorator):
    """A str-valued :class:`enum.Enum` stored by its ``.value``.

    Storage is identical to the current ``String`` column (no migration).
    Accepts the enum member, its raw value, or a plain ``str`` when binding
    (validating membership); always returns the enum member when loading.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_cls: type[Enum], *, length: int = 32, **kwargs: Any) -> None:
        # Attribute name matches the constructor parameter so SQLAlchemy's
        # cache-key machinery (cache_ok=True) distinguishes EnumType(A) from
        # EnumType(B) and never conflates their compiled statements.
        self.enum_cls = enum_cls
        super().__init__(length=length, **kwargs)

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, self.enum_cls):
            return value.value
        return self.enum_cls(value).value

    def process_result_value(self, value: Any, dialect: Dialect) -> Enum | None:
        if value is None:
            return None
        if isinstance(value, self.enum_cls):
            return value
        return self.enum_cls(value)
