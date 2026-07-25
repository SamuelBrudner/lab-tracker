"""Immutable object IDs and portable paths for registered Git stores."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, TypeAlias

from lab_tracker.local_store_locator import (
    PortableStorePath,
    canonical_store_authority,
)

GitObjectFormat: TypeAlias = Literal["sha1", "sha256"]

_GIT_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z", re.ASCII)


@dataclass(frozen=True, slots=True)
class GitObjectId:
    """One full, lowercase, nonzero SHA-1 or SHA-256 Git object ID."""

    value: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.value, str)
            or _GIT_OBJECT_ID.fullmatch(self.value) is None
            or not any(character != "0" for character in self.value)
        ):
            raise ValueError(
                "Git object ID must be a full lowercase nonzero SHA-1 or "
                "SHA-256 hexadecimal value."
            )

    @classmethod
    def parse(cls, value: object) -> GitObjectId | None:
        """Return one exact immutable object ID, without repairing its input."""

        if not isinstance(value, str):
            return None
        try:
            return cls(value)
        except ValueError:
            return None

    @property
    def object_format(self) -> GitObjectFormat:
        """Return the Git repository object format implied by the ID length."""

        return "sha1" if len(self.value) == 40 else "sha256"


@dataclass(frozen=True, slots=True)
class PinnedGitPath:
    """A portable repository path paired with one immutable Git object ID."""

    path: PortableStorePath
    object_id: GitObjectId

    def __post_init__(self) -> None:
        if not isinstance(self.path, PortableStorePath) or not isinstance(
            self.object_id, GitObjectId
        ):
            raise ValueError(
                "Pinned Git paths require a portable path and immutable object ID."
            )

    @classmethod
    def parse_decoded(cls, value: object) -> PinnedGitPath | None:
        """Parse a decoded ``<repository-path>@<object-id>`` locator exactly."""

        return cls._parse(value, uri_path=False)

    @classmethod
    def parse_uri_path(cls, value: object) -> PinnedGitPath | None:
        """Parse a URI-path locator, splitting at ``@`` before percent decoding."""

        return cls._parse(value, uri_path=True)

    @classmethod
    def _parse(cls, value: object, *, uri_path: bool) -> PinnedGitPath | None:
        if not isinstance(value, str):
            return None
        raw_path, separator, raw_object_id = value.rpartition("@")
        if not separator:
            return None
        path = (
            PortableStorePath.parse_uri_path(raw_path)
            if uri_path
            else PortableStorePath.parse_decoded(raw_path)
        )
        object_id = GitObjectId.parse(raw_object_id)
        if path is None or object_id is None:
            return None
        return cls(path=path, object_id=object_id)

    @property
    def locator(self) -> str:
        """Return the canonical decoded ``path@object-id`` locator."""

        return f"{self.path.path}@{self.object_id.value}"

    @property
    def uri_path(self) -> str:
        """Return the canonical encoded ``path@object-id`` URI-path value."""

        return f"{self.path.uri_path}@{self.object_id.value}"


def canonical_git_store_uri(
    store_name: str,
    pin: PinnedGitPath,
) -> str | None:
    """Return the canonical logical URI for one registered Git-store pin."""

    authority = canonical_store_authority(store_name)
    if authority is None or not isinstance(pin, PinnedGitPath):
        return None
    return f"store://{authority}/{pin.uri_path}"
