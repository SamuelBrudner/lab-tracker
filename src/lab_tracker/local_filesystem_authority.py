"""Filesystem-I/O-free authority selection for host-local operations.

Operator roots are normalized only as lexical path components.  This module
must never canonicalize, stat, open, or otherwise follow a candidate path.
The bounded platform helper is the sole owner of pre-follow resolution.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import islice
from os import PathLike
from typing import Final, Literal

from lab_tracker.local_path_syntax import parse_windows_absolute_local_path

_GENERIC_ROOT_ERROR: Final = "Local filesystem roots must be valid local paths."
MAX_LOCAL_FILESYSTEM_AUTHORITY_ROOTS: Final = 64
MAX_LOCAL_FILESYSTEM_PATH_CHARACTERS: Final = 16 * 1024
MAX_LOCAL_FILESYSTEM_PATH_BYTES: Final = 16 * 1024
MAX_LOCAL_FILESYSTEM_PATH_COMPONENTS: Final = 4_096
MAX_LOCAL_FILESYSTEM_COMPONENT_BYTES: Final = 255
_MAX_LOCAL_FILESYSTEM_CONFIG_CHARACTERS: Final = MAX_LOCAL_FILESYSTEM_AUTHORITY_ROOTS * (
    MAX_LOCAL_FILESYSTEM_PATH_CHARACTERS + 1
)


@dataclass(frozen=True, slots=True, repr=False)
class _LexicalRoot:
    rendered: str
    anchor: str
    components: tuple[str, ...]


@dataclass(frozen=True, slots=True, repr=False)
class _LexicalCandidate:
    rendered: str
    anchor: str
    components: tuple[str, ...]


_LOCAL_FILESYSTEM_BOUNDARY_FACTORY_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False, repr=False)
class LocalFilesystemAuthorityBoundary:
    """One strict native lexical boundary, parsed without filesystem I/O."""

    flavor: Literal["posix", "windows"]
    rendered: str
    anchor: str
    components: tuple[str, ...]

    def __init__(
        self,
        *,
        flavor: Literal["posix", "windows"],
        rendered: str,
        anchor: str,
        components: tuple[str, ...],
        _factory_token: object,
    ) -> None:
        if _factory_token is not _LOCAL_FILESYSTEM_BOUNDARY_FACTORY_TOKEN:
            raise TypeError(
                "LocalFilesystemAuthorityBoundary must be built by its parser."
            )
        object.__setattr__(self, "flavor", flavor)
        object.__setattr__(self, "rendered", rendered)
        object.__setattr__(self, "anchor", anchor)
        object.__setattr__(self, "components", components)

    @classmethod
    def parse(cls, value: object) -> LocalFilesystemAuthorityBoundary | None:
        """Parse an absolute native root with the strict authority grammar."""

        if not isinstance(value, str) or not _is_registry_safe_path_text(value):
            return None
        try:
            parsed = _parse_absolute(value)
        except (OSError, RuntimeError, TypeError, ValueError):
            return None
        flavor: Literal["posix", "windows"] = (
            "windows" if os.name == "nt" else "posix"
        )
        return cls(
            flavor=flavor,
            rendered=parsed.rendered,
            anchor=parsed.anchor,
            components=parsed.components,
            _factory_token=_LOCAL_FILESYSTEM_BOUNDARY_FACTORY_TOKEN,
        )

    def contains(self, candidate: LocalFilesystemAuthorityBoundary) -> bool:
        """Return exact-case lexical component containment."""

        if not isinstance(candidate, LocalFilesystemAuthorityBoundary):
            return False
        size = len(self.components)
        return (
            candidate.flavor == self.flavor
            and candidate.anchor == self.anchor
            and candidate.components[:size] == self.components
        )


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class LocalDirectoryGrant:
    """Opaque proof that one candidate has a selected lexical operator root."""

    _authority_token: object
    _root_index: int
    _candidate: str
    _compatibility_root: str | None = None


@dataclass(frozen=True, slots=True, init=False, eq=False, repr=False)
class LocalFilesystemAuthority:
    """Select an operator grant without observing target filesystem state."""

    _roots: tuple[_LexicalRoot, ...]
    _token: object
    _unscoped_library_compatibility: bool

    def __init__(
        self,
        roots: Sequence[str | PathLike[str]],
        *,
        cwd: str | PathLike[str] | None = None,
    ) -> None:
        """Build authority from explicit roots.

        Relative roots retain their historical meaning, but are joined to one
        captured startup working directory.  Dot components are rejected
        instead of normalized because collapsing ``link/..`` changes native
        traversal semantics when ``link`` is an alias.
        """

        if isinstance(roots, (str, bytes)):
            raise TypeError("Local filesystem roots must be a sequence of paths.")
        try:
            raw_roots = tuple(islice(iter(roots), MAX_LOCAL_FILESYSTEM_AUTHORITY_ROOTS + 1))
        except TypeError:
            raise TypeError("Local filesystem roots must be a sequence of paths.") from None
        if len(raw_roots) > MAX_LOCAL_FILESYSTEM_AUTHORITY_ROOTS:
            raise ValueError(_GENERIC_ROOT_ERROR)

        captured_cwd: str | None = None
        parsed: list[_LexicalRoot] = []
        seen: set[tuple[str, tuple[str, ...]]] = set()
        try:
            for raw_root in raw_roots:
                rendered = os.fspath(raw_root)
                if not isinstance(rendered, str):
                    raise ValueError(_GENERIC_ROOT_ERROR)
                expanded = _expand_operator_root(rendered)
                if not _is_absolute(expanded):
                    if captured_cwd is None:
                        captured_cwd = _capture_cwd(cwd)
                    expanded = _join_relative_root(captured_cwd, expanded)
                root = _parse_absolute(expanded)
                key = (root.anchor, root.components)
                if key not in seen:
                    seen.add(key)
                    parsed.append(root)
        except (OSError, RuntimeError, TypeError, ValueError):
            raise ValueError(_GENERIC_ROOT_ERROR) from None

        object.__setattr__(self, "_roots", tuple(parsed))
        object.__setattr__(self, "_token", object())
        object.__setattr__(self, "_unscoped_library_compatibility", False)

    @classmethod
    def from_roots(
        cls,
        roots: Sequence[str | PathLike[str]],
        *,
        cwd: str | PathLike[str] | None = None,
    ) -> LocalFilesystemAuthority:
        """Build an explicit authority; an empty sequence denies all paths."""

        return cls(roots, cwd=cwd)

    @classmethod
    def from_config(
        cls,
        raw: str | None,
        *,
        cwd: str | PathLike[str] | None = None,
    ) -> LocalFilesystemAuthority:
        """Parse the configured path-list; unset and empty values deny all."""

        if raw is not None and not isinstance(raw, str):
            raise TypeError("Local filesystem root configuration must be a string.")
        path_module = ntpath if os.name == "nt" else posixpath
        if raw and (
            len(raw) > _MAX_LOCAL_FILESYSTEM_CONFIG_CHARACTERS
            or raw.count(path_module.pathsep) >= MAX_LOCAL_FILESYSTEM_AUTHORITY_ROOTS
        ):
            raise ValueError(_GENERIC_ROOT_ERROR)
        roots = [part for part in raw.split(path_module.pathsep) if part.strip()] if raw else []
        return cls(roots, cwd=cwd)

    @classmethod
    def for_unscoped_library_compatibility(cls) -> LocalFilesystemAuthority:
        """Build the explicit library-only authority for direct file reads.

        Runtime composition must always use :meth:`from_config`.  This authority
        intentionally owns no enumerable roots, so enabling recovery on a
        compatibility resolver can never turn into a host-root scan.
        """

        authority = cls(())
        object.__setattr__(authority, "_unscoped_library_compatibility", True)
        return authority

    @property
    def legacy_roots(self) -> tuple[str, ...]:
        """Return normalized roots for compatibility-only library consumers.

        New filesystem consumers must depend on a narrow broker role instead.
        Runtime recovery enumeration uses the private broker request surface.
        """

        return tuple(root.rendered for root in self._roots)

    def _recovery_roots(self) -> tuple[str, ...]:
        """Reveal explicit roots only to the concrete bounded broker.

        Unscoped library compatibility deliberately has no enumerable roots,
        even though it can select a drive or POSIX anchor for one direct read.
        """

        if self._unscoped_library_compatibility:
            return ()
        return tuple(root.rendered for root in self._roots)

    def _request_for_root_index(
        self,
        root_index: int,
    ) -> tuple[str, tuple[str, ...]]:
        """Reveal one configured root for an enumerated broker target."""

        if (
            self._unscoped_library_compatibility
            or type(root_index) is not int
            or root_index < 0
            or root_index >= len(self._roots)
        ):
            raise ValueError("Local filesystem grant is invalid.")
        rendered = self._roots[root_index].rendered
        return rendered, (rendered,)

    def select_directory(
        self,
        candidate: str | PathLike[str],
    ) -> LocalDirectoryGrant | None:
        """Select the single most-specific component-containing root."""

        try:
            rendered = os.fspath(candidate)
            if not isinstance(rendered, str):
                return None
            parsed_candidate = _parse_candidate_absolute(rendered)
        except (OSError, RuntimeError, TypeError, ValueError):
            return None

        if self._unscoped_library_compatibility:
            compatibility_root = (
                f"{parsed_candidate.anchor}\\" if os.name == "nt" else parsed_candidate.anchor
            )
            return LocalDirectoryGrant(
                self._token,
                -1,
                parsed_candidate.rendered,
                compatibility_root,
            )

        selected_index: int | None = None
        selected_size = -1
        for index, root in enumerate(self._roots):
            if (
                root.anchor == parsed_candidate.anchor
                and len(parsed_candidate.components) >= len(root.components)
                and parsed_candidate.components[: len(root.components)] == root.components
                and len(root.components) > selected_size
            ):
                selected_index = index
                selected_size = len(root.components)
        if selected_index is None:
            return None
        suffix = parsed_candidate.components[selected_size:]
        if _provably_pops_above_selected_root(suffix):
            return None
        return LocalDirectoryGrant(
            self._token,
            selected_index,
            parsed_candidate.rendered,
        )

    def _request_for(
        self,
        grant: LocalDirectoryGrant,
    ) -> tuple[str, tuple[str, ...]]:
        """Reveal one selected request only to the concrete broker."""

        if not isinstance(grant, LocalDirectoryGrant) or grant._authority_token is not self._token:
            raise ValueError("Local filesystem grant is invalid.")
        if grant._root_index == -1:
            if not self._unscoped_library_compatibility or grant._compatibility_root is None:
                raise ValueError("Local filesystem grant is invalid.")
            return grant._candidate, (grant._compatibility_root,)
        if grant._root_index < 0 or grant._root_index >= len(self._roots):
            raise ValueError("Local filesystem grant is invalid.")
        return grant._candidate, (self._roots[grant._root_index].rendered,)


def _capture_cwd(cwd: str | PathLike[str] | None) -> str:
    raw_cwd = os.getcwd() if cwd is None else os.fspath(cwd)
    if not isinstance(raw_cwd, str):
        raise ValueError(_GENERIC_ROOT_ERROR)
    return _parse_absolute(raw_cwd).rendered


def _expand_operator_root(root: str) -> str:
    _validate_lexical_path_budget(root, windows=os.name == "nt")
    _reject_control_characters(root)
    if not root.startswith("~"):
        return root
    allowed_prefixes = ("~/", "~\\") if os.name == "nt" else ("~/",)
    if root != "~" and not root.startswith(allowed_prefixes):
        raise ValueError(_GENERIC_ROOT_ERROR)
    if os.name == "nt":
        home = os.environ.get("USERPROFILE")
        if not home:
            home_drive = os.environ.get("HOMEDRIVE")
            home_path = os.environ.get("HOMEPATH")
            home = f"{home_drive}{home_path}" if home_drive and home_path else None
        separator = "\\"
    else:
        home = os.environ.get("HOME")
        separator = "/"
    if not home or not _is_absolute(home):
        raise ValueError(_GENERIC_ROOT_ERROR)
    if root == "~" or len(root) == 2:
        return home
    trimmed_home = home.rstrip("/\\")
    return f"{trimmed_home}{separator}{root[2:]}"


def _join_relative_root(cwd: str, root: str) -> str:
    if not root:
        raise ValueError(_GENERIC_ROOT_ERROR)
    separator = "\\" if os.name == "nt" else "/"
    stripped_cwd = cwd.rstrip("/\\")
    return f"{stripped_cwd}{separator}{root}"


def _is_absolute(path: str) -> bool:
    if os.name == "nt":
        # Python 3.10 reports a bare UNC share (``\\server\share``) as
        # non-absolute because ``ntpath.splitdrive`` consumes the whole value.
        # Treat every drive- or separator-anchored spelling as absolute here
        # so unsupported namespaces reach the strict Windows parser and fail
        # closed instead of being joined beneath the captured working directory.
        drive, tail = ntpath.splitdrive(path)
        return bool(drive) or tail.startswith(("\\", "/"))
    return posixpath.isabs(path)


def _parse_absolute(path: str) -> _LexicalRoot:
    _validate_lexical_path_budget(path, windows=os.name == "nt")
    _reject_control_characters(path)
    return _parse_windows_absolute(path) if os.name == "nt" else _parse_posix_absolute(path)


def _parse_candidate_absolute(path: str) -> _LexicalCandidate:
    _validate_lexical_path_budget(path, windows=os.name == "nt")
    _reject_control_characters(path)
    if os.name == "nt":
        return _parse_windows_candidate(path)
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError(_GENERIC_ROOT_ERROR)
    components = tuple(path[1:].split("/"))
    if any(
        component not in {"", ".", ".."}
        and _filesystem_path_bytes(component) > MAX_LOCAL_FILESYSTEM_COMPONENT_BYTES
        for component in components
    ):
        raise ValueError(_GENERIC_ROOT_ERROR)
    return _LexicalCandidate(path, "/", components)


def _parse_windows_candidate(path: str) -> _LexicalCandidate:
    _validate_lexical_path_budget(path, windows=True)
    _reject_control_characters(path)
    parsed = parse_windows_absolute_local_path(path, allow_navigation=True)
    if parsed is None:
        raise ValueError(_GENERIC_ROOT_ERROR)
    return _LexicalCandidate(parsed.rendered, parsed.anchor, parsed.components)


def _provably_pops_above_selected_root(suffix: Sequence[str]) -> bool:
    """Reject only traversal that escapes before any unresolved component."""

    for component in suffix:
        if component in {"", "."}:
            continue
        # A leading parent escapes.  An ordinary component may be an alias, so
        # resolution of every later parent depends on the helper's handle walk.
        return component == ".."
    return False


def _parse_posix_absolute(path: str) -> _LexicalRoot:
    _validate_lexical_path_budget(path, windows=False)
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError(_GENERIC_ROOT_ERROR)
    raw_components = path[1:].split("/")
    components = _validated_components(raw_components)
    rendered = "/" if not components else f"/{'/'.join(components)}"
    return _LexicalRoot(rendered, "/", components)


def _parse_windows_absolute(path: str) -> _LexicalRoot:
    _validate_lexical_path_budget(path, windows=True)
    parsed = parse_windows_absolute_local_path(path, allow_navigation=False)
    if parsed is None:
        raise ValueError(_GENERIC_ROOT_ERROR)
    return _LexicalRoot(parsed.rendered, parsed.anchor, parsed.components)


def _validated_components(
    raw_components: Sequence[str],
) -> tuple[str, ...]:
    components = list(raw_components)
    if components and components[-1] == "":
        components.pop()
    if any(component in {"", ".", ".."} for component in components) or any(
        _filesystem_path_bytes(component) > MAX_LOCAL_FILESYSTEM_COMPONENT_BYTES
        for component in components
    ):
        raise ValueError(_GENERIC_ROOT_ERROR)
    return tuple(components)


def _validate_lexical_path_budget(path: str, *, windows: bool) -> None:
    if (
        len(path) > MAX_LOCAL_FILESYSTEM_PATH_CHARACTERS
        or path.count("\\" if windows else "/") > MAX_LOCAL_FILESYSTEM_PATH_COMPONENTS
    ):
        raise ValueError(_GENERIC_ROOT_ERROR)
    try:
        encoded_size = (
            len(path.encode("utf-16-le", errors="strict")) // 2
            if windows
            else _filesystem_path_bytes(path)
        )
    except (LookupError, UnicodeError):
        raise ValueError(_GENERIC_ROOT_ERROR) from None
    if encoded_size > MAX_LOCAL_FILESYSTEM_PATH_BYTES:
        raise ValueError(_GENERIC_ROOT_ERROR)


def _filesystem_path_bytes(path: str) -> int:
    return len(os.fsencode(path))


def _is_registry_safe_path_text(value: str) -> bool:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return not any(unicodedata.category(character) == "Cc" for character in value)


def _reject_control_characters(value: str) -> None:
    if not value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(_GENERIC_ROOT_ERROR)
