"""Canonical, portable locators for registered local filesystem stores.

Local-store locators are deliberately stricter than native filesystem paths.
They are slash-separated logical names that can be interpreted consistently on
POSIX and Windows without accepting aliases for traversal or separators.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import quote, unquote_to_bytes, urlsplit

from lab_tracker.local_path_policy import is_reserved_windows_component

_STORE_NAME_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62})\Z")
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_URI_AMBIGUOUS_COMPONENT_CHARACTERS = frozenset("#")
_MAX_COMPONENT_BYTES = 255
_MAX_URI_PATH_BYTES = 4096
_URI_PATH_SAFE_CHARACTERS = "-._~@"


def is_valid_local_store_name(value: object) -> bool:
    """Return whether ``value`` is one canonical local-store name."""

    return isinstance(value, str) and _STORE_NAME_RE.fullmatch(value) is not None


def _component_is_valid(component: object) -> bool:
    if not isinstance(component, str) or not component:
        return False
    if component in {".", ".."}:
        return False
    if "%" in component or "/" in component or "\\" in component:
        return False
    if any(
        character in _URI_AMBIGUOUS_COMPONENT_CHARACTERS for character in component
    ):
        return False
    if any(unicodedata.category(character) == "Cc" for character in component):
        return False
    try:
        encoded = component.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    if len(encoded) > _MAX_COMPONENT_BYTES:
        return False
    return not is_reserved_windows_component(component)


def _canonical_uri_path(components: tuple[str, ...]) -> str:
    return "/".join(
        quote(component, safe=_URI_PATH_SAFE_CHARACTERS, encoding="utf-8", errors="strict")
        for component in components
    )


def _components_are_valid(components: tuple[str, ...]) -> bool:
    if not components or not all(_component_is_valid(component) for component in components):
        return False
    return len(_canonical_uri_path(components).encode("ascii")) <= _MAX_URI_PATH_BYTES


def _percent_escapes_are_well_formed(value: str) -> bool:
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        if (
            index + 2 >= len(value)
            or value[index + 1] not in _HEX_DIGITS
            or value[index + 2] not in _HEX_DIGITS
        ):
            return False
        index += 3
    return True


@dataclass(frozen=True, slots=True)
class LocalStoreLocator:
    """A validated slash-separated locator relative to a local store."""

    components: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.components, tuple) or not _components_are_valid(self.components):
            raise ValueError("Store locator components are invalid.")

    @classmethod
    def parse_decoded(cls, value: str) -> LocalStoreLocator | None:
        """Parse a decoded slash-separated locator.

        Percent signs are not accepted here: callers with a URI path must use
        :meth:`parse_uri_path` so encoded aliases are decoded exactly once.
        """

        if not isinstance(value, str):
            return None
        components = tuple(value.split("/"))
        try:
            return cls(components)
        except (TypeError, ValueError):
            return None

    @classmethod
    def parse_uri_path(cls, raw_path: str) -> LocalStoreLocator | None:
        """Parse the locator portion of a URI path, without its leading slash."""

        if not isinstance(raw_path, str) or not _percent_escapes_are_well_formed(raw_path):
            return None
        raw_components = raw_path.split("/")
        if not raw_components or any(not component for component in raw_components):
            return None

        decoded_components: list[str] = []
        try:
            for raw_component in raw_components:
                decoded_components.append(
                    unquote_to_bytes(raw_component).decode("utf-8", errors="strict")
                )
        except (UnicodeDecodeError, UnicodeEncodeError):
            return None

        try:
            return cls(tuple(decoded_components))
        except (TypeError, ValueError):
            return None

    @property
    def path(self) -> str:
        """Return the decoded slash-separated form."""

        return "/".join(self.components)

    @property
    def uri_path(self) -> str:
        """Return the canonical, once-percent-encoded URI-path form."""

        return _canonical_uri_path(self.components)


def canonical_local_store_uri(
    store_name: str, locator: LocalStoreLocator
) -> str | None:
    """Return the canonical URI for a validated local-store name and locator."""

    if not is_valid_local_store_name(store_name) or not isinstance(
        locator, LocalStoreLocator
    ):
        return None
    return f"store://{store_name}/{locator.uri_path}"


def parse_local_store_uri(uri: str) -> tuple[str, LocalStoreLocator] | None:
    """Parse a structurally exact local ``store://<name>/<locator>`` URI."""

    if (
        not isinstance(uri, str)
        or not uri.startswith("store://")
        or "?" in uri
        or "#" in uri
        or any(unicodedata.category(character) == "Cc" for character in uri)
    ):
        return None
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return None
    if (
        parsed.scheme != "store"
        or not is_valid_local_store_name(parsed.netloc)
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
    ):
        return None
    locator = LocalStoreLocator.parse_uri_path(parsed.path[1:])
    if locator is None:
        return None
    return parsed.netloc, locator
