"""Transport-neutral results returned by application handlers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

EntityT = TypeVar("EntityT")


@dataclass(frozen=True)
class Page(Generic[EntityT]):
    """A database-paged result with the unsliced total."""

    items: list[EntityT]
    total: int


@dataclass(frozen=True)
class FileDownload:
    """Metadata and a lazy byte stream for an HTTP download adapter."""

    chunks: Iterable[bytes]
    filename: str
    content_type: str
    size_bytes: int
    nosniff: bool = False


@dataclass(frozen=True)
class AssetMutationResult(Generic[EntityT]):
    """Outcome of an idempotent or replacing asset write."""

    entity: EntityT
    outcome: Literal["created", "replaced", "reused"]

    @property
    def reused(self) -> bool:
        return self.outcome == "reused"
