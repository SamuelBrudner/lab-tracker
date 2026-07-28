"""Bounded repository-convention snapshots for graph-draft context.

Consumer repositories explicitly enroll convention files in
``.lab-tracker/agent-context.json``.  Commit capture reads those files from the
captured git tree (never from the dirty worktree), strips Lab Tracker's own
managed prompt blocks, and stores one compact JSON snapshot in note metadata.

The server treats the snapshot as untrusted descriptive data.  Validation here
is therefore about bounds and corruption, not trust or authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

AGENT_CONTEXT_CONFIG_VERSION = 1
REPOSITORY_CONVENTIONS_VERSION = 1
AGENT_CONTEXT_CONFIG_RELATIVE_PATH = Path(".lab-tracker") / "agent-context.json"

REPOSITORY_CONVENTIONS_METADATA_KEY = "git_repository_conventions"
REPOSITORY_CONVENTIONS_HASH_METADATA_KEY = "git_repository_conventions_hash"

MAX_REPOSITORY_CONVENTION_FILES = 8
MAX_AGENT_CONTEXT_CONFIG_BYTES = 64 * 1024
MAX_REPOSITORY_CONVENTION_PATH_BYTES = 1024
MAX_REPOSITORY_CONVENTION_SOURCE_BYTES = 128 * 1024
MAX_REPOSITORY_CONVENTION_DOCUMENT_BYTES = 12 * 1024
MAX_REPOSITORY_CONVENTION_TOTAL_BYTES = 24 * 1024
MAX_REPOSITORY_CONVENTIONS_METADATA_BYTES = 64 * 1024

DISCOVERED_CONVENTION_PATHS = (
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    ".github/copilot-instructions.md",
    "CONTRIBUTING.md",
)
_DISCOVERED_CONVENTION_GLOBS = (
    ".github/instructions/*.instructions.md",
    ".cursor/rules/*.mdc",
)
_MANAGED_BLOCK_PAIRS = (
    (
        "<!-- BEGIN LAB TRACKER MCP ACTIVATION -->",
        "<!-- END LAB TRACKER MCP ACTIVATION -->",
    ),
    (
        "<!-- BEGIN LAB TRACKER CODE CONVENTIONS -->",
        "<!-- END LAB TRACKER CODE CONVENTIONS -->",
    ),
    (
        "<!-- BEGIN LAB TRACKER AGENTS CODE CONVENTIONS -->",
        "<!-- END LAB TRACKER AGENTS CODE CONVENTIONS -->",
    ),
)
_VERSION_LINE = re.compile(r"^<!-- lab-tracker-code-conventions[^\n]*-->\s*$", re.MULTILINE)


@dataclass(frozen=True)
class AgentContextConfig:
    """Repo-local selection of convention files that may reach Lab Tracker."""

    paths: list[str] = field(default_factory=list)
    config_path: Path | None = None
    version: int = AGENT_CONTEXT_CONFIG_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "paths": list(self.paths)}


def default_agent_context_config_path(repo_root: str | Path) -> Path:
    return Path(repo_root).expanduser().resolve() / AGENT_CONTEXT_CONFIG_RELATIVE_PATH


def load_agent_context_config(
    repo_root: str | Path,
    *,
    required: bool = False,
) -> AgentContextConfig:
    """Load the repo-local enrollment file; absence is an empty configuration."""

    path = default_agent_context_config_path(repo_root)
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("Agent-context config and its directory must not be symlinks.")
    if not path.exists():
        if required:
            raise ValueError(f"Agent-context config not found: {path}")
        return AgentContextConfig(config_path=path)
    try:
        config_size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"Agent-context config is unavailable: {path}") from exc
    if config_size > MAX_AGENT_CONTEXT_CONFIG_BYTES:
        raise ValueError("Agent-context config exceeds the 64 KiB safety limit.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Agent-context config is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Agent-context config must be a JSON object.")
    raw_version = payload.get("version", AGENT_CONTEXT_CONFIG_VERSION)
    if isinstance(raw_version, bool):
        raise ValueError("Agent-context config version must be an integer.")
    try:
        version = int(raw_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("Agent-context config version must be an integer.") from exc
    if version != AGENT_CONTEXT_CONFIG_VERSION:
        raise ValueError(
            f"Unsupported agent-context config version {version}; "
            f"expected {AGENT_CONTEXT_CONFIG_VERSION}."
        )
    paths = _normalized_config_paths(payload.get("paths") or [])
    return AgentContextConfig(paths=paths, config_path=path, version=version)


def save_agent_context_config(config: AgentContextConfig) -> None:
    """Atomically persist an enrollment configuration without following links."""

    if config.config_path is None:
        raise ValueError("Agent-context config_path is required.")
    path = Path(os.path.abspath(config.config_path.expanduser()))
    if path.name != AGENT_CONTEXT_CONFIG_RELATIVE_PATH.name or (
        path.parent.name != AGENT_CONTEXT_CONFIG_RELATIVE_PATH.parent.name
    ):
        raise ValueError(
            "Agent-context config must be .lab-tracker/agent-context.json."
        )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(
            f"Could not create agent-context config directory: {path.parent}"
        ) from exc
    expected_parent = path.parent.parent.resolve() / ".lab-tracker"
    try:
        resolved_parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"Agent-context config directory is unavailable: {path.parent}") from exc
    if path.parent.is_symlink() or resolved_parent != expected_parent:
        raise ValueError(
            "Agent-context config directory must be a real directory inside the repository."
        )
    if path.is_symlink():
        raise ValueError("Agent-context config must not be a symlink.")
    if config.version != AGENT_CONTEXT_CONFIG_VERSION:
        raise ValueError(
            f"Unsupported agent-context config version {config.version}; "
            f"expected {AGENT_CONTEXT_CONFIG_VERSION}."
        )
    paths = _normalized_config_paths(config.paths)
    content = json.dumps(
        {"version": config.version, "paths": paths},
        indent=2,
        sort_keys=True,
    ) + "\n"
    fd = -1
    temporary = ""
    try:
        fd, temporary = tempfile.mkstemp(
            dir=resolved_parent,
            prefix=".agent-context-",
            suffix=".tmp",
            text=True,
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = ""
    except OSError as exc:
        raise ValueError(f"Could not write agent-context config: {path}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary:
            with suppress(OSError):
                Path(temporary).unlink()


def discover_repository_convention_files(repo_root: str | Path) -> list[str]:
    """Return recognized, regular convention files currently present in a repo."""

    root = Path(repo_root).expanduser().resolve()
    discovered: list[str] = []
    for relative in DISCOVERED_CONVENTION_PATHS:
        if _safe_regular_file(root, relative):
            discovered.append(relative)
    for pattern in _DISCOVERED_CONVENTION_GLOBS:
        for path in sorted(root.glob(pattern)):
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if _safe_regular_file(root, relative):
                discovered.append(relative)
    return list(dict.fromkeys(discovered))


def validate_enrolled_convention_path(repo_root: str | Path, path: str) -> str:
    """Validate that a selected file is repo-local, regular, UTF-8, and tracked."""

    root = Path(repo_root).expanduser().resolve()
    relative = _validated_relative_path(path)
    if not _safe_regular_file(root, relative):
        raise ValueError(f"Convention path must be a regular repo-local file: {relative}")
    try:
        raw = (root / relative).read_bytes()
        if b"\x00" in raw:
            raise UnicodeError
        raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Convention path must be UTF-8 text: {relative}") from exc
    tracked = _git_text(root, "ls-files", "--error-unmatch", "--", relative)
    if not tracked.strip():
        raise ValueError(
            f"Convention path must be tracked by git before enrollment: {relative}"
        )
    return relative


def normalize_enrolled_convention_path(path: str) -> str:
    """Normalize a configured path without requiring the file to still exist."""

    return _validated_relative_path(path)


def capture_repository_conventions(
    repo_root: str | Path,
    *,
    commit: str,
    repository: str,
    config: AgentContextConfig | None = None,
) -> dict[str, Any] | None:
    """Capture enrolled convention blobs from one exact git commit.

    Missing, deleted, binary, symlink, and oversized files are skipped without
    aborting commit capture. An explicitly empty or wholly omitted enrollment
    produces a tombstone snapshot; ``None`` means enrollment was never enabled.
    """

    root = Path(repo_root).expanduser().resolve()
    selected = config or load_agent_context_config(root)
    selected_paths = _normalized_config_paths(selected.paths)
    config_present = bool(selected.config_path and selected.config_path.exists())
    if not selected_paths and not config_present:
        return None
    commit_sha = _git_text(root, "rev-parse", f"{commit}^{{commit}}").strip()
    if not commit_sha:
        return None
    committed_at = _git_text(root, "show", "-s", "--format=%cI", commit_sha).strip()
    generation_text = _git_text(root, "rev-list", "--count", commit_sha).strip()
    try:
        commit_generation = int(generation_text)
    except ValueError:
        commit_generation = 0
    entries = _git_tree_entries(root, commit_sha, selected_paths)
    documents_by_hash: dict[str, dict[str, Any]] = {}
    omitted: list[dict[str, Any]] = []
    total_content_bytes = 0

    for relative in selected_paths:
        entry = entries.get(relative)
        if entry is None:
            omitted.append({"path": relative, "reason": "missing_at_commit"})
            continue
        mode, object_id = entry
        if not mode.startswith("100"):
            omitted.append({"path": relative, "reason": "not_regular_file"})
            continue
        size_text = _git_text(root, "cat-file", "-s", object_id).strip()
        try:
            source_size = int(size_text)
        except ValueError:
            omitted.append({"path": relative, "reason": "unreadable"})
            continue
        if source_size > MAX_REPOSITORY_CONVENTION_SOURCE_BYTES:
            omitted.append(
                {"path": relative, "reason": "oversized", "size_bytes": source_size}
            )
            continue
        raw = _git_bytes(root, "cat-file", "blob", object_id)
        if raw is None or b"\x00" in raw:
            omitted.append({"path": relative, "reason": "not_utf8_text"})
            continue
        try:
            cleaned = _strip_lab_tracker_managed_blocks(raw.decode("utf-8")).strip()
        except UnicodeError:
            omitted.append({"path": relative, "reason": "not_utf8_text"})
            continue
        if not cleaned:
            omitted.append({"path": relative, "reason": "empty_after_managed_blocks"})
            continue
        cleaned_bytes = cleaned.encode("utf-8")
        content_hash = _sha256(cleaned_bytes)
        duplicate = documents_by_hash.get(content_hash)
        if duplicate is not None:
            duplicate["paths"].append(relative)
            continue
        if len(documents_by_hash) >= MAX_REPOSITORY_CONVENTION_FILES:
            omitted.append({"path": relative, "reason": "file_limit"})
            continue
        remaining = MAX_REPOSITORY_CONVENTION_TOTAL_BYTES - total_content_bytes
        if remaining <= 0:
            omitted.append({"path": relative, "reason": "total_size_limit"})
            continue
        visible_limit = min(MAX_REPOSITORY_CONVENTION_DOCUMENT_BYTES, remaining)
        visible, truncated = _bounded_utf8(cleaned, visible_limit)
        visible_size = len(visible.encode("utf-8"))
        if not visible_size:
            omitted.append({"path": relative, "reason": "total_size_limit"})
            continue
        total_content_bytes += visible_size
        documents_by_hash[content_hash] = {
            "paths": [relative],
            "content": visible,
            "content_hash": content_hash,
            "source_size_bytes": len(cleaned_bytes),
            "included_size_bytes": visible_size,
            "truncated": truncated,
        }

    documents = list(documents_by_hash.values())
    snapshot: dict[str, Any] = {
        "version": REPOSITORY_CONVENTIONS_VERSION,
        "repository": repository.strip() or f"local:{root.name}",
        "commit": commit_sha,
        "documents": documents,
        "omitted": omitted,
    }
    if _valid_iso_datetime(committed_at):
        snapshot["committed_at"] = committed_at
    if commit_generation >= 0:
        snapshot["commit_generation"] = commit_generation
    snapshot["snapshot_hash"] = _snapshot_hash(snapshot)
    return snapshot


def repository_conventions_metadata(snapshot: Mapping[str, Any] | None) -> dict[str, str]:
    """Serialize one validated snapshot into scalar note metadata fields."""

    if not snapshot:
        return {}
    snapshot_hash = str(snapshot.get("snapshot_hash") or "")
    if not snapshot_hash:
        return {}
    return {
        REPOSITORY_CONVENTIONS_METADATA_KEY: json.dumps(
            dict(snapshot), separators=(",", ":"), sort_keys=True
        ),
        REPOSITORY_CONVENTIONS_HASH_METADATA_KEY: snapshot_hash,
    }


def parse_repository_conventions_metadata(
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Parse and re-bound an untrusted note-metadata snapshot."""

    raw = (metadata or {}).get(REPOSITORY_CONVENTIONS_METADATA_KEY)
    if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > (
        MAX_REPOSITORY_CONVENTIONS_METADATA_BYTES
    ):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or parsed.get("version") != (
        REPOSITORY_CONVENTIONS_VERSION
    ):
        return None
    repository = parsed.get("repository")
    commit = parsed.get("commit")
    snapshot_hash = parsed.get("snapshot_hash")
    documents = parsed.get("documents")
    if not all(
        isinstance(value, str) and value
        for value in (repository, commit, snapshot_hash)
    ):
        return None
    if not isinstance(documents, list) or len(documents) > (
        MAX_REPOSITORY_CONVENTION_FILES
    ):
        return None
    committed_at = parsed.get("committed_at")
    if committed_at is not None and (
        not isinstance(committed_at, str) or not _valid_iso_datetime(committed_at)
    ):
        return None
    commit_generation = parsed.get("commit_generation")
    if commit_generation is not None and (
        isinstance(commit_generation, bool)
        or not isinstance(commit_generation, int)
        or commit_generation < 0
    ):
        return None
    clean_documents: list[dict[str, Any]] = []
    total = 0
    for document in documents:
        if not isinstance(document, dict):
            return None
        paths = document.get("paths")
        content = document.get("content")
        content_hash = document.get("content_hash")
        if not isinstance(paths, list) or not paths or not all(
            isinstance(path, str) and path for path in paths
        ):
            return None
        if not isinstance(content, str) or not isinstance(content_hash, str):
            return None
        size = len(content.encode("utf-8"))
        if size > MAX_REPOSITORY_CONVENTION_DOCUMENT_BYTES:
            return None
        total += size
        if total > MAX_REPOSITORY_CONVENTION_TOTAL_BYTES:
            return None
        try:
            source_size = int(document.get("source_size_bytes") or size)
        except (TypeError, ValueError):
            return None
        clean_documents.append(
            {
                "paths": paths,
                "content": content,
                "content_hash": content_hash,
                "source_size_bytes": source_size,
                "included_size_bytes": size,
                "truncated": bool(document.get("truncated")),
            }
        )
    raw_omitted = parsed.get("omitted") or []
    if not isinstance(raw_omitted, list):
        return None
    clean: dict[str, Any] = {
        "version": REPOSITORY_CONVENTIONS_VERSION,
        "repository": repository,
        "commit": commit,
        "documents": clean_documents,
        "omitted": [
            dict(item)
            for item in raw_omitted[:MAX_REPOSITORY_CONVENTION_FILES]
            if isinstance(item, dict)
        ],
    }
    if committed_at is not None:
        clean["committed_at"] = committed_at
    if commit_generation is not None:
        clean["commit_generation"] = commit_generation
    clean["snapshot_hash"] = _snapshot_hash(clean)
    expected = str((metadata or {}).get(REPOSITORY_CONVENTIONS_HASH_METADATA_KEY) or "")
    if clean["snapshot_hash"] != snapshot_hash or (expected and expected != snapshot_hash):
        return None
    return clean


def metadata_without_repository_conventions(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Remove the bulky transport fields before ordinary note compaction."""

    return {
        str(key): value
        for key, value in metadata.items()
        if key
        not in {
            REPOSITORY_CONVENTIONS_METADATA_KEY,
            REPOSITORY_CONVENTIONS_HASH_METADATA_KEY,
        }
    }


def _git_tree_entries(
    root: Path,
    commit: str,
    selected_paths: list[str],
) -> dict[str, tuple[str, str]]:
    raw = _git_bytes(root, "ls-tree", "-z", commit, "--", *selected_paths)
    if raw is None:
        return {}
    entries: dict[str, tuple[str, str]] = {}
    for record in raw.split(b"\x00"):
        if not record or b"\t" not in record:
            continue
        header, raw_path = record.split(b"\t", 1)
        fields = header.decode("ascii", errors="ignore").split()
        try:
            path = raw_path.decode("utf-8")
        except UnicodeError:
            continue
        if len(fields) == 3 and fields[1] == "blob":
            entries[path] = (fields[0], fields[2])
    return entries


def _strip_lab_tracker_managed_blocks(content: str) -> str:
    cleaned = content
    for begin, end in _MANAGED_BLOCK_PAIRS:
        cleaned = re.sub(
            re.escape(begin) + r".*?" + re.escape(end) + r"\s*",
            "",
            cleaned,
            flags=re.DOTALL,
        )
    return _VERSION_LINE.sub("", cleaned)


def _snapshot_hash(snapshot: Mapping[str, Any]) -> str:
    digest_payload = {
        "version": snapshot.get("version"),
        "repository": snapshot.get("repository"),
        "documents": [
            {
                "paths": document.get("paths"),
                "content_hash": document.get("content_hash"),
                "content": document.get("content"),
                "truncated": bool(document.get("truncated")),
            }
            for document in snapshot.get("documents") or []
            if isinstance(document, Mapping)
        ],
        "omitted": snapshot.get("omitted") or [],
    }
    return _sha256(json.dumps(digest_payload, sort_keys=True).encode("utf-8"))


def _bounded_utf8(value: str, max_bytes: int) -> tuple[str, bool]:
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value, False
    marker = "\n\n[truncated by Lab Tracker]"
    marker_bytes = marker.encode("utf-8")
    if max_bytes < len(marker_bytes):
        return raw[:max_bytes].decode("utf-8", errors="ignore"), True
    available = max(0, max_bytes - len(marker_bytes))
    prefix = raw[:available].decode("utf-8", errors="ignore").rstrip()
    return prefix + marker, True


def _normalized_config_paths(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("Agent-context config paths must be a list.")
    if not all(isinstance(item, str) for item in value):
        raise ValueError("Agent-context config paths must contain only strings.")
    paths = sorted({_validated_relative_path(item) for item in value})
    if len(paths) > MAX_REPOSITORY_CONVENTION_FILES:
        raise ValueError(
            "Agent-context config may enroll at most "
            f"{MAX_REPOSITORY_CONVENTION_FILES} convention files."
        )
    return paths


def _valid_iso_datetime(value: str) -> bool:
    if not value or len(value) > 64:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _validated_relative_path(value: str) -> str:
    raw = value.strip().replace("\\", "/")
    if len(raw.encode("utf-8")) > MAX_REPOSITORY_CONVENTION_PATH_BYTES:
        raise ValueError(
            "Convention path exceeds the "
            f"{MAX_REPOSITORY_CONVENTION_PATH_BYTES}-byte safety limit."
        )
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or ".." in path.parts or raw.startswith("~"):
        raise ValueError(f"Convention path must be repo-relative: {value!r}")
    normalized = path.as_posix()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized or normalized == "AGENTS.lt.md":
        raise ValueError(f"Convention path is not enrollable: {value!r}")
    return normalized


def _safe_regular_file(root: Path, relative: str) -> bool:
    try:
        path = root / _validated_relative_path(relative)
        return path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root)
    except (OSError, ValueError):
        return False


def _git_text(root: Path, *args: str) -> str:
    raw = _git_bytes(root, *args)
    return raw.decode("utf-8", errors="replace") if raw is not None else ""


def _git_bytes(root: Path, *args: str) -> bytes | None:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed executable, no shell.
            ["git", "-C", str(root), *args],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()
