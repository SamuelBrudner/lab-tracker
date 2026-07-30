"""Structural addresses for artifacts beneath registered rclone roots.

Rclone distinguishes paths relative to a remote's home from paths rooted at
the backend root.  These values retain that distinction until the final argv
target is composed, without routing decoded path data through URI helpers or
native filesystem normalization.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from lab_tracker.local_store_locator import PortableStorePath

_RCLONE_REMOTE_PUNCTUATION = frozenset("_-.+@ ")


def _remote_character_is_allowed(character: str) -> bool:
    return (
        character in _RCLONE_REMOTE_PUNCTUATION
        or unicodedata.category(character)[:1] in {"L", "N"}
    )


def _remote_name_has_valid_grammar(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if value[0] in {"-", " "} or value.endswith(" "):
        return False
    if len(value) == 1 and value.isascii() and value.isalpha():
        return False
    if not all(_remote_character_is_allowed(character) for character in value):
        return False
    try:
        value.encode("utf-8", errors="strict")
        compatibility_form = unicodedata.normalize("NFKC", value)
        compatibility_form.encode("utf-8", errors="strict")
    except UnicodeError:
        return False
    return (
        compatibility_form == value
        or _remote_name_has_valid_grammar_without_compatibility(compatibility_form)
    )


def _remote_name_has_valid_grammar_without_compatibility(value: str) -> bool:
    """Validate one already-normalized form without recursing through NFKC."""

    return (
        bool(value)
        and value[0] not in {"-", " "}
        and not value.endswith(" ")
        and not (len(value) == 1 and value.isascii() and value.isalpha())
        and all(_remote_character_is_allowed(character) for character in value)
    )


@dataclass(frozen=True, slots=True)
class RcloneRemoteName:
    """One exact configured rclone remote name.

    The stored spelling is never normalized.  Its NFKC form must nevertheless
    remain inside the same grammar so compatibility normalization cannot turn a
    valid-looking authority into option or connection-string syntax.
    """

    value: str

    def __post_init__(self) -> None:
        if not _remote_name_has_valid_grammar(self.value):
            raise ValueError("Rclone remote name is invalid.")

    @classmethod
    def parse(cls, value: object) -> RcloneRemoteName | None:
        """Return an exact validated remote name, or ``None``."""

        try:
            return cls(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True, slots=True)
class RegisteredRcloneRoot:
    """A decoded registered rclone root with explicit rootedness."""

    rooted: bool
    components: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.rooted) is not bool or not isinstance(self.components, tuple):
            raise ValueError("Registered rclone root is invalid.")
        if not self.components:
            if not self.rooted:
                raise ValueError("Registered rclone root is invalid.")
            return
        try:
            PortableStorePath(self.components)
        except (TypeError, ValueError) as exc:
            raise ValueError("Registered rclone root is invalid.") from exc

    @classmethod
    def parse_decoded(cls, value: object) -> RegisteredRcloneRoot | None:
        """Parse a decoded root while preserving one leading slash."""

        if not isinstance(value, str) or not value:
            return None
        rooted = value.startswith("/")
        decoded_path = value[1:] if rooted else value
        if rooted and not decoded_path:
            return cls(rooted=True, components=())
        path = PortableStorePath.parse_decoded(decoded_path)
        if path is None:
            return None
        try:
            return cls(rooted=rooted, components=path.components)
        except (TypeError, ValueError):
            return None

    def compose(
        self,
        remote: RcloneRemoteName,
        locator: PortableStorePath,
    ) -> str | None:
        """Compose one exact rclone argv target beneath this root."""

        if not isinstance(remote, RcloneRemoteName) or not isinstance(
            locator, PortableStorePath
        ):
            return None
        try:
            combined = PortableStorePath((*self.components, *locator.components))
        except (TypeError, ValueError):
            return None
        rooted_prefix = "/" if self.rooted else ""
        return f"{remote.value}:{rooted_prefix}{combined.path}"

    def compose_root(self, remote: RcloneRemoteName) -> str | None:
        """Compose this registered root itself as one exact argv target."""

        if not isinstance(remote, RcloneRemoteName):
            return None
        if not self.components:
            return f"{remote.value}:/"
        rooted_prefix = "/" if self.rooted else ""
        path = PortableStorePath(self.components).path
        return f"{remote.value}:{rooted_prefix}{path}"
