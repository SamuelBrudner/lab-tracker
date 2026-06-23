"""Shared file discovery and stable fingerprint helpers."""

from __future__ import annotations

import fnmatch
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileFingerprint:
    """Stable file fingerprint observed by a watch scan."""

    size_bytes: int
    mtime: float
    checksum: str


def file_sha256(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def stable_file_fingerprint(path: str | Path) -> FileFingerprint | None:
    resolved = Path(path).expanduser().resolve()
    try:
        before = resolved.stat()
        checksum = file_sha256(resolved)
        after = resolved.stat()
    except (FileNotFoundError, PermissionError):
        return None
    if before.st_size != after.st_size or before.st_mtime != after.st_mtime:
        return None
    return FileFingerprint(
        size_bytes=after.st_size,
        mtime=after.st_mtime,
        checksum=checksum,
    )


def discover_files(
    root: str | Path,
    *,
    include_patterns: Sequence[str] | None = None,
    exclude_patterns: Sequence[str] | None = None,
    ignore_hidden: bool = True,
) -> list[Path]:
    resolved_root = Path(root).expanduser().resolve()
    if not resolved_root.exists():
        raise ValueError(f"watch root is not an existing path: {resolved_root}")
    include = list(include_patterns or ["*"])
    exclude = list(exclude_patterns or [])
    candidates = [resolved_root] if resolved_root.is_file() else sorted(resolved_root.rglob("*"))
    files: list[Path] = []
    base = resolved_root.parent if resolved_root.is_file() else resolved_root
    for candidate in candidates:
        if candidate.is_symlink() or not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if ignore_hidden and is_hidden_relative(resolved, relative_to=base):
            continue
        if not matches_any(resolved, root=base, patterns=include):
            continue
        if matches_any(resolved, root=base, patterns=exclude):
            continue
        files.append(resolved)
    return files


def is_hidden_relative(path: Path, *, relative_to: Path) -> bool:
    try:
        candidate = path.relative_to(relative_to)
    except ValueError:
        candidate = path
    return any(part.startswith(".") for part in candidate.parts)


def matches_any(path: Path, *, root: Path, patterns: Sequence[str]) -> bool:
    if not patterns:
        return False
    try:
        relative_path = path.relative_to(root).as_posix()
    except ValueError:
        relative_path = path.name
    return any(
        fnmatch.fnmatch(relative_path, pattern) or fnmatch.fnmatch(path.name, pattern)
        for pattern in patterns
    )


def local_folder_external_id(root: Path, relative_path: str) -> str:
    return f"{root.as_uri()}::{relative_path}"
