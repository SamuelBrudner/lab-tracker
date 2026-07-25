"""Canonical, portable paths for registered artifact stores.

Portable store paths are deliberately stricter than native filesystem or URL
paths. They are slash-separated logical names that can be interpreted
consistently across transports and operating systems without accepting aliases
for traversal or separators.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import quote, unquote_to_bytes, urlsplit

from lab_tracker.local_path_policy import is_reserved_windows_component

_STORE_NAME_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62})\Z")
_GENERIC_STORE_NAME_FORBIDDEN_CHARACTERS = frozenset(r"/\?#")
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_URI_AMBIGUOUS_COMPONENT_CHARACTERS = frozenset("#")
_MAX_COMPONENT_BYTES = 255
_MAX_URI_PATH_BYTES = 4096
_URI_AUTHORITY_SAFE_CHARACTERS = "-._~"
_URI_PATH_SAFE_CHARACTERS = "-._~@"


def is_valid_local_store_name(value: object) -> bool:
    """Return whether ``value`` is one canonical local-store name."""

    return isinstance(value, str) and _STORE_NAME_RE.fullmatch(value) is not None


def is_valid_store_name(value: object) -> bool:
    """Return whether a generic store name is one exact logical URI authority.

    Nonlocal store names retain legacy spaces, ``@``, and ``:`` characters.
    Names that ``urlsplit`` rejects or reinterprets are excluded so structured
    and URI-only references share one canonical identity.
    """

    if (
        not isinstance(value, str)
        or not value.strip()
        or any(
            character in _GENERIC_STORE_NAME_FORBIDDEN_CHARACTERS
            or unicodedata.category(character) == "Cc"
            for character in value
        )
    ):
        return False
    try:
        value.encode("utf-8", errors="strict")
        candidate = f"store://{value}/artifact"
        parsed = urlsplit(candidate)
    except (UnicodeError, ValueError):
        return False
    return (
        parsed.scheme == "store"
        and parsed.netloc == value
        and parsed.path == "/artifact"
        and not parsed.query
        and not parsed.fragment
    )


def _component_has_portable_structure(component: str) -> bool:
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
    return not is_reserved_windows_component(component)


def _component_is_valid(component: object) -> bool:
    if not isinstance(component, str) or not component:
        return False
    if not _component_has_portable_structure(component):
        return False
    # Backends and filesystems sometimes apply Unicode compatibility
    # normalization after URL decoding. Reject any component whose NFKC form
    # becomes traversal, a separator/escape, or other nonportable syntax so
    # such normalization cannot widen the registered prefix.
    compatibility_form = unicodedata.normalize("NFKC", component)
    if not _component_has_portable_structure(compatibility_form):
        return False
    try:
        encoded = component.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return len(encoded) <= _MAX_COMPONENT_BYTES


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
class PortableStorePath:
    """A validated slash-separated path relative to a registered store."""

    components: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.components, tuple) or not _components_are_valid(self.components):
            raise ValueError("Store locator components are invalid.")

    @classmethod
    def parse_decoded(cls, value: str) -> PortableStorePath | None:
        """Parse a decoded slash-separated path.

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
    def parse_uri_path(cls, raw_path: str) -> PortableStorePath | None:
        """Parse a URI path without its leading slash, decoding exactly once."""

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


# Backward-compatible domain name retained for local-store callers. Keeping a
# true alias also guarantees that local and nonlocal store paths share one
# validation grammar and compare as the same immutable value.
LocalStoreLocator = PortableStorePath


def canonical_store_uri(
    store_name: str, locator: PortableStorePath
) -> str | None:
    """Return a canonical logical URI for a registered nonlocal store."""

    authority = canonical_store_authority(store_name)
    if authority is None or not isinstance(locator, PortableStorePath):
        return None
    return f"store://{authority}/{locator.uri_path}"


def canonical_store_authority(store_name: str) -> str | None:
    """Encode one exact logical store name as a write-safe URI authority."""

    if not is_valid_store_name(store_name):
        return None
    return quote(
        store_name,
        safe=_URI_AUTHORITY_SAFE_CHARACTERS,
        encoding="utf-8",
        errors="strict",
    )


def parse_canonical_store_authority(authority: str) -> str | None:
    """Decode one canonical URI authority back to its exact logical name."""

    if (
        not isinstance(authority, str)
        or not authority
        or not _percent_escapes_are_well_formed(authority)
    ):
        return None
    try:
        store_name = unquote_to_bytes(authority).decode("utf-8", errors="strict")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return None
    if canonical_store_authority(store_name) != authority:
        return None
    return store_name


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
