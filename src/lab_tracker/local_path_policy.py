"""Cross-platform parsing and containment for host-local artifact paths.

Persisted artifact URIs are untrusted input.  This module keeps URI syntax,
native path conversion, and allowed-root authorization in one small policy so
every local filesystem consumer applies the same rules.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import MutableSequence, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote_to_bytes, urlsplit
from urllib.request import url2pathname

from lab_tracker.local_path_syntax import (
    is_reserved_windows_component as is_reserved_windows_component,
)
from lab_tracker.local_path_syntax import (
    parse_windows_absolute_local_path,
)

_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_WINDOWS_FILE_URI_PATH = re.compile(r"^/[A-Za-z]:/")


def _has_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _has_malformed_percent_escape(value: str) -> bool:
    index = 0
    while True:
        index = value.find("%", index)
        if index < 0:
            return False
        if _PERCENT_ESCAPE.fullmatch(value[index : index + 3]) is None:
            return True
        index += 3


def _has_encoded_separator(value: str) -> bool:
    lowered = value.lower()
    if "%2f" in lowered:
        return True
    return os.name == "nt" and "%5c" in lowered


def _is_supported_absolute_path(path: str) -> bool:
    """Return whether ``path`` is an absolute path on the current local volume.

    UNC and device paths are intentionally not local-artifact inputs.  They can
    initiate network or device-namespace I/O during canonicalization and need a
    separate, explicit network-store policy.
    """

    if not path or _has_control_characters(path) or not os.path.isabs(path):
        return False
    if os.name != "nt":
        # Four-slash file URIs decode to a double-slash path.  POSIX leaves the
        # meaning of exactly two leading slashes implementation-defined, so fail
        # closed rather than treating it as an authorityless UNC alias.
        return not path.startswith("//")

    return parse_windows_absolute_local_path(path, allow_navigation=False) is not None


def is_supported_absolute_local_root(root: str | os.PathLike[str]) -> bool:
    """Return whether ``root`` is a raw, native, absolute local path.

    This predicate is deliberately lexical and side-effect free.  In particular,
    it does not expand ``~``, make relative paths absolute, canonicalize links, or
    probe the filesystem.  Callers can therefore reject untrusted registered
    roots before any host-path operation occurs.
    """

    try:
        raw_root = os.fspath(root)
    except TypeError:
        return False
    return isinstance(raw_root, str) and _is_supported_absolute_path(raw_root)


def native_local_path_from_uri(uri: str) -> str | None:
    """Decode one local path or strict ``file:`` URI for the current platform.

    Empty authority and literal ``localhost`` are the only supported file-URI
    authorities.  Direct UNC/device namespace inputs and encoded separators are
    rejected before path canonicalization.  Pre-follow reparse inspection is
    tracked separately by ``lab-tracker-n5kp.61``.
    """

    if not uri or _has_control_characters(uri):
        return None

    # A native Windows drive path is otherwise parsed as URL scheme ``c``.
    # Preserve native absolute paths without applying URI percent-decoding.
    if os.path.isabs(uri):
        return uri if _is_supported_absolute_path(uri) else None
    if any(character.isspace() for character in uri):
        return None

    try:
        parsed = urlsplit(uri)
    except (TypeError, ValueError):
        return None
    if parsed.scheme.lower() != "file":
        return None
    if parsed.query or parsed.fragment:
        return None
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        return None
    raw_path = parsed.path
    if not raw_path or _has_malformed_percent_escape(raw_path) or _has_encoded_separator(raw_path):
        return None
    if os.name == "nt" and ("\\" in raw_path or _WINDOWS_FILE_URI_PATH.match(raw_path) is None):
        # Python 3.10/3.11 nturl2path scans backward from the first colon and
        # would alias malformed paths such as /garbageC:/x to C:\x.  Accept
        # only the slash-separated drive-path shape emitted by
        # pathlib.Path.as_uri().
        return None
    try:
        encoded_path = unquote_to_bytes(raw_path)
        if os.name == "nt":
            # Windows paths are Unicode.  Validate the URI's UTF-8 bytes before
            # url2pathname so urllib cannot silently replace malformed input
            # with U+FFFD and alias a different filename.
            encoded_path.decode("utf-8", errors="strict")
            decoded = url2pathname(raw_path)
        else:
            # Path.as_uri() percent-encodes os.fsencode(path).  Decode with the
            # inverse filesystem codec so non-UTF-8 POSIX filenames retain
            # surrogateescape identity instead of being replacement-decoded.
            decoded = os.fsdecode(encoded_path)
    except (OSError, TypeError, UnicodeError, ValueError):
        return None
    return decoded if _is_supported_absolute_path(decoded) else None


def _lexical_path_key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _canonical_path_key(path: str) -> str:
    normalized = os.path.normpath(path)
    if os.name != "nt":
        return normalized
    drive, tail = os.path.splitdrive(normalized)
    return f"{drive.upper()}{tail}"


def _is_contained(candidate: str, root: str, *, canonical: bool) -> bool:
    key = _canonical_path_key if canonical else _lexical_path_key
    candidate_key = key(candidate)
    root_key = key(root)
    if canonical and os.name == "nt":
        candidate_drive, candidate_tail = os.path.splitdrive(candidate_key)
        root_drive, root_tail = os.path.splitdrive(root_key)
        if candidate_drive != root_drive:
            return False
        candidate_parts = tuple(part for part in re.split(r"[\\/]", candidate_tail) if part)
        root_parts = tuple(part for part in re.split(r"[\\/]", root_tail) if part)
        return (
            len(candidate_parts) >= len(root_parts)
            and candidate_parts[: len(root_parts)] == root_parts
        )
    try:
        return os.path.commonpath((candidate_key, root_key)) == root_key
    except (OSError, ValueError):
        # Different Windows drives/shares and absolute/relative mismatches fail
        # closed instead of escaping through string-prefix comparisons.
        return False


def _is_link_or_reparse_point(path: str) -> bool:
    try:
        if os.path.islink(path):
            return True
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except (OSError, ValueError):
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and attributes & reparse_flag)


@dataclass(frozen=True, slots=True, init=False, eq=False, repr=False)
class LocalPathPolicy:
    """Prepare native local paths against optional canonical allowed roots.

    ``None`` preserves the resolver's explicit unscoped mode.  An empty
    sequence is deny-all and rejects candidates without touching their
    filesystem locations.  A returned canonical path is a preliminary plan;
    byte readers must still bind final authorization to the opened descriptor.
    """

    _lexical_roots: tuple[str, ...] | None
    _canonical_roots: tuple[str, ...] | None
    _recovery_roots: tuple[str, ...] | None

    def __init__(self, allowed_roots: Sequence[str | Path] | None = None) -> None:
        if allowed_roots is None:
            object.__setattr__(self, "_lexical_roots", None)
            object.__setattr__(self, "_canonical_roots", None)
            object.__setattr__(self, "_recovery_roots", None)
            return

        lexical_roots: list[str] = []
        canonical_roots: list[str] = []
        recovery_roots: list[str] = []
        seen_lexical: set[str] = set()
        for root in allowed_roots:
            try:
                expanded = os.fspath(Path(root).expanduser())
                lexical = os.path.abspath(expanded)
            except (OSError, RuntimeError, TypeError, ValueError):
                raise ValueError("Local resolver roots must be valid local paths.") from None
            if not _is_supported_absolute_path(lexical):
                raise ValueError("Local resolver roots must be absolute local paths.")
            try:
                # Preserve unresolved ``..`` components until realpath follows
                # preceding links. Lexically collapsing ``link/..`` first can
                # authorize a different directory from native path traversal.
                canonical = os.path.realpath(expanded)
            except (OSError, RuntimeError, ValueError):
                raise ValueError("Local resolver roots could not be canonicalized.") from None
            if not _is_supported_absolute_path(canonical):
                raise ValueError("Local resolver roots must resolve to local paths.")
            lexical_key = _lexical_path_key(lexical)
            if lexical_key not in seen_lexical:
                seen_lexical.add(lexical_key)
                lexical_roots.append(lexical)

            # Recovery must not walk one canonical tree twice through aliases
            # or overlapping roots.  Keep the broadest unique authorities.
            if any(
                _is_contained(canonical, existing, canonical=True) for existing in canonical_roots
            ):
                continue
            retained_indexes = [
                index
                for index, existing in enumerate(canonical_roots)
                if not _is_contained(existing, canonical, canonical=True)
            ]
            canonical_roots = [canonical_roots[index] for index in retained_indexes]
            recovery_roots = [recovery_roots[index] for index in retained_indexes]
            canonical_roots.append(canonical)
            recovery_roots.append(lexical)
        object.__setattr__(self, "_lexical_roots", tuple(lexical_roots))
        object.__setattr__(self, "_canonical_roots", tuple(canonical_roots))
        object.__setattr__(self, "_recovery_roots", tuple(recovery_roots))

    @classmethod
    def from_config(cls, raw: str | None) -> LocalPathPolicy:
        """Parse an ``os.pathsep``-separated operator root configuration.

        This preserves the historical environment parser: unset, empty, and
        whitespace-only values deny every local path; empty components are
        omitted; and non-empty components retain their exact spelling for the
        constructor's existing relative/tilde normalization.
        """

        if raw is not None and not isinstance(raw, str):
            raise TypeError("Local resolver root configuration must be a string.")
        allowed_roots = [part for part in raw.split(os.pathsep) if part.strip()] if raw else []
        return cls(allowed_roots)

    @property
    def canonical_roots(self) -> tuple[str, ...] | None:
        """Return unique broadest physical roots for containment comparisons."""

        return self._canonical_roots

    @property
    def lexical_roots(self) -> tuple[str, ...] | None:
        """Return every unique operator spelling for helper authority."""

        return self._lexical_roots

    @property
    def recovery_roots(self) -> tuple[str, ...] | None:
        """Return one authorized spelling per broadest canonical walk root."""

        return self._recovery_roots

    def restricted_to_absolute_root(self, root: str | os.PathLike[str]) -> LocalPathPolicy | None:
        """Return this authority narrowed to one complete registered-store root.

        An unscoped operator policy delegates exactly the store root.  An
        explicit deny-all policy delegates nothing.  A configured policy only
        delegates when the *whole* canonical store root is beneath one operator
        root; a narrower operator grant never partially authorizes a broader
        registered store.
        """

        if self._canonical_roots == ():
            return None
        try:
            raw_root = os.fspath(root)
        except (OSError, TypeError, ValueError):
            return None
        if not is_supported_absolute_local_root(raw_root):
            return None
        try:
            lexical_root = os.path.normpath(raw_root)
        except (OSError, ValueError):
            return None
        if self._canonical_roots is not None:
            precheck_roots = (
                *(self._lexical_roots or ()),
                *self._canonical_roots,
            )
            if not any(
                _is_contained(lexical_root, operator_root, canonical=False)
                for operator_root in precheck_roots
            ):
                return None
        try:
            # Canonicalize the original spelling so a link followed by ``..``
            # retains native filesystem traversal semantics.
            canonical_root = os.path.realpath(raw_root)
        except (OSError, RuntimeError, ValueError):
            return None
        if not is_supported_absolute_local_root(canonical_root):
            return None
        if self._canonical_roots is not None and not any(
            _is_contained(canonical_root, operator_root, canonical=True)
            for operator_root in self._canonical_roots
        ):
            return None
        # Build from the canonical spelling so both preliminary lexical
        # authorization and exact-handle authorization share one authority.
        try:
            restricted = LocalPathPolicy([canonical_root])
        except ValueError:
            # Canonicalization can fail or the root can retarget between the
            # first check and construction of the exact restricted policy.
            return None
        restricted_roots = restricted.canonical_roots
        if not restricted_roots or len(restricted_roots) != 1:
            return None
        if not (
            _is_contained(restricted_roots[0], canonical_root, canonical=True)
            and _is_contained(canonical_root, restricted_roots[0], canonical=True)
        ):
            # The registered root retargeted between canonicalizations. Even if
            # both locations sit below one broad operator root, they are
            # different store authorities.
            return None
        if self._canonical_roots is not None and not any(
            _is_contained(restricted_roots[0], operator_root, canonical=True)
            for operator_root in self._canonical_roots
        ):
            # The root changed while it was canonicalized. Never let that race
            # turn a narrower operator authority into an outside store grant.
            return None
        return restricted

    def authorize_uri(self, uri: str) -> str | None:
        """Return one canonical authorized path, or ``None`` without raising."""

        if self._canonical_roots == ():
            return None
        path = native_local_path_from_uri(uri)
        if path is None:
            return None
        return self.authorize_path(path)

    def authorize_path(self, path: str | os.PathLike[str]) -> str | None:
        """Return a canonical in-policy path plan, or ``None``.

        This method does not grant a reopenable capability.  Consumers that
        read bytes must validate and retain the exact opened descriptor.
        """

        if self._canonical_roots == ():
            return None
        try:
            native = os.fspath(path)
        except TypeError:
            return None
        if not _is_supported_absolute_path(native):
            return None

        # Reject obvious sibling/cross-volume candidates before realpath.  This
        # avoids touching a hostile volume/share merely to decide it is outside
        # every configured root.  Keep both configured and canonical spellings
        # so a configured root that is itself a symlink remains usable.
        if self._canonical_roots is not None:
            lexical_roots = self._lexical_roots or ()
            precheck_roots = (*lexical_roots, *self._canonical_roots)
            if not any(_is_contained(native, root, canonical=False) for root in precheck_roots):
                return None
        try:
            canonical = os.path.realpath(native)
        except (OSError, ValueError):
            return None
        if not _is_supported_absolute_path(canonical):
            return None
        if not self.contains_canonical_path(canonical):
            return None
        return canonical

    def contains_canonical_path(self, path: str) -> bool:
        """Check a canonical path against the policy's canonical roots."""

        if self._canonical_roots is None:
            return True
        return any(_is_contained(path, root, canonical=True) for root in self._canonical_roots)

    def prune_walk_directories(self, directory: str, children: MutableSequence[str]) -> None:
        """Prune link/reparse and escaping children from a top-down ``os.walk``."""

        kept: list[str] = []
        for name in children:
            child = os.path.join(directory, name)
            if _is_link_or_reparse_point(child):
                continue
            if self.authorize_path(child) is not None:
                kept.append(name)
        children[:] = kept
