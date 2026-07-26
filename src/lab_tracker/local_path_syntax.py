"""Pure lexical syntax shared by host-local path boundaries."""

from __future__ import annotations

import ntpath
from dataclasses import dataclass

_WINDOWS_DEVICE_PREFIXES = ("\\\\?\\", "\\\\.\\", "\\??\\", "//?/", "//./")
_WINDOWS_RESERVED_CHARACTERS = frozenset('"*:<>?|')
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{suffix}" for suffix in "123456789¹²³"}
    | {f"LPT{suffix}" for suffix in "123456789¹²³"}
)
_MAX_WINDOWS_LOCAL_PATH_UTF16_UNITS = 32_767
_MAX_WINDOWS_LOCAL_COMPONENT_UTF16_UNITS = 255


@dataclass(frozen=True, slots=True)
class WindowsLocalPath:
    """One normalized drive-local Windows path with exact-case components."""

    drive: str
    components: tuple[str, ...]

    @property
    def anchor(self) -> str:
        return f"{self.drive}:"

    @property
    def rendered(self) -> str:
        if not self.components:
            return f"{self.drive}:\\"
        suffix = "\\".join(self.components)
        return f"{self.drive}:\\{suffix}"


def parse_windows_absolute_local_path(
    path: str,
    *,
    allow_navigation: bool,
) -> WindowsLocalPath | None:
    """Parse one bounded DOS drive path without filesystem access.

    Windows treats slash direction and redundant separators as spelling
    aliases.  Normalize only those aliases; device/UNC namespaces, navigation
    tokens in roots, reserved names, and every other ambiguous component fail
    closed.
    """

    if (
        not isinstance(path, str)
        or not path
        or path.startswith(_WINDOWS_DEVICE_PREFIXES)
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        return None
    try:
        units = len(path.encode("utf-16-le", errors="strict")) // 2
    except UnicodeError:
        return None
    if units > _MAX_WINDOWS_LOCAL_PATH_UTF16_UNITS:
        return None

    drive, tail = ntpath.splitdrive(path)
    if (
        len(drive) != 2
        or not drive[0].isascii()
        or not drive[0].isalpha()
        or drive[1] != ":"
        or not tail.startswith(("\\", "/"))
    ):
        return None

    components = tuple(
        component
        for component in tail.replace("/", "\\").split("\\")
        if component
    )
    for component in components:
        if component in {".", ".."}:
            if allow_navigation:
                continue
            return None
        if is_reserved_windows_component(component):
            return None
        try:
            component_units = len(
                component.encode("utf-16-le", errors="strict")
            ) // 2
        except UnicodeError:
            return None
        if component_units > _MAX_WINDOWS_LOCAL_COMPONENT_UTF16_UNITS:
            return None
    return WindowsLocalPath(drive[0].upper(), components)


def is_reserved_windows_component(name: str) -> bool:
    """Return whether one component has reserved Windows filesystem syntax."""

    if not name:
        return False
    if name[-1:] in (".", " ") and name not in (".", ".."):
        return True
    if any(character in _WINDOWS_RESERVED_CHARACTERS for character in name):
        return True
    stem = name.partition(".")[0].rstrip(" ").upper()
    return stem in _WINDOWS_RESERVED_NAMES
