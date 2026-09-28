"""Event-driven watch capture for files an agent writes: ``lt watch touch``.

A coding agent's ``PostToolUse`` hook (matcher ``Write|Edit|MultiEdit|
NotebookEdit``) pipes the hook JSON to ``lt watch touch``. When the written
path falls under a watch root of the checkout's own ``.lab-tracker/watch.json``,
exactly the event a ``lt watch scan`` of that watch would queue for that one
file is queued (same identity, so the scheduled scan dedupes against it), and
the watch outbox gets a best-effort drain. Everything else is a fast no-op:
no network call, no directory walk beyond locating the config, no hashing.

Touch never widens capture: it queues only what the configured watch would
capture anyway, only on a write-tool event (a ``Read`` is never captured),
and, for staged-note watches, only when a project is declared by the watch,
its config, ``LAB_TRACKER_PROJECT_ID``, or the checkout's ``lt_ids.json``
(never a profile default). ``LAB_TRACKER_AGENT_HOOKS=0`` turns it off.
"""

from __future__ import annotations

import fnmatch
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lab_tracker_client import watch as watch_capture
from lab_tracker_client.agent_session import (
    EDIT_TOOL_PATH_KEYS,
    agent_hooks_enabled,
    drain_watch_outbox,
    hook_base_dir,
    hook_cwd,
    record_drain,
    redact_secrets,
)
from lab_tracker_client.client import LabTracker

JsonObject = dict[str, Any]

TOUCH_SYNC_LIMIT = 10
_CONFIG_RELATIVE = Path(".lab-tracker") / "watch.json"


@dataclass(frozen=True)
class WatchMatch:
    """A touched file and the configured watch whose scan would capture it."""

    path: Path
    root: Path
    watch: Mapping[str, Any]


def hook_paths(hook: Mapping[str, Any]) -> list[str]:
    """The written path(s) a hook payload names; ``[]`` for a non-write tool.

    ``tool_input.file_path`` (Write, Edit, MultiEdit) or ``notebook_path``
    (NotebookEdit); a top-level ``file_path`` is accepted for agents whose
    file-edit hooks put it there.
    """

    tool_name = hook.get("tool_name")
    if isinstance(tool_name, str) and tool_name and tool_name not in EDIT_TOOL_PATH_KEYS:
        return []
    candidates: list[Any] = []
    tool_input = hook.get("tool_input")
    if isinstance(tool_input, Mapping):
        candidates.extend([tool_input.get("file_path"), tool_input.get("notebook_path")])
    candidates.append(hook.get("file_path"))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return [candidate.strip()]
    return []


def find_checkout_watch_config(base: Path) -> Path | None:
    """The checkout's own watch config for ``base``, found with stat calls only.

    ``LAB_TRACKER_WATCH_CONFIG`` wins. Otherwise walk up from ``base`` to the
    checkout root (the first directory holding ``.git``) and stop there, so a
    lab-wide config in a parent directory never claims an agent's writes.
    Outside any checkout only ``base`` itself is considered.
    """

    explicit = os.getenv("LAB_TRACKER_WATCH_CONFIG")
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate if candidate.is_file() else None
    start = Path(os.path.abspath(base))
    for directory in (start, *start.parents):
        candidate = directory / _CONFIG_RELATIVE
        if candidate.is_file():
            return candidate
        if (directory / ".git").exists():
            return None
    return None


def match_watches(
    config: watch_capture.WatchConfig, path: Path, *, raw: Path | None = None
) -> list[WatchMatch]:
    """Configured watches whose scan would capture ``path`` (already absolute)."""

    matches: list[WatchMatch] = []
    for watch in config.watches:
        root = _watch_root(config, watch)
        if root is None:
            continue
        if _watch_would_capture(watch, root=root, path=path, raw=raw):
            matches.append(WatchMatch(path=path, root=root, watch=watch))
    return matches


def _watch_root(config: watch_capture.WatchConfig, watch: Mapping[str, Any]) -> Path | None:
    raw_root = str(watch.get("root") or "").strip()
    if not raw_root:
        return None
    root = Path(raw_root).expanduser()
    if not root.is_absolute():
        # Relative roots are anchored at the checkout the config belongs to.
        root = config.checkout_root() / root
    with suppress(OSError, RuntimeError):
        return root.resolve()
    return None


def _watch_would_capture(
    watch: Mapping[str, Any], *, root: Path, path: Path, raw: Path | None
) -> bool:
    """Mirror ``lab_tracker.file_watch.discover_files`` (or the manifest glob) for one file."""

    mode = str(watch.get("mode") or watch_capture.MODE_FILES)
    if mode == watch_capture.MODE_MANIFEST:
        pattern = str(watch.get("pattern") or watch_capture.DEFAULT_MANIFEST_PATTERN)
        return _is_within(path, root) and fnmatch.fnmatch(path.name, pattern)
    if path == root:
        base = root.parent
    elif _is_within(path, root):
        base = root
    else:
        return False
    if (raw is not None and raw.is_symlink()) or not path.is_file():
        return False
    if watch_capture.is_hidden_relative(path, relative_to=base):
        return False
    include = _strings(watch.get("include")) or ["*"]
    if not watch_capture.matches_any(path, root=base, patterns=include):
        return False
    return not watch_capture.matches_any(path, root=base, patterns=_strings(watch.get("exclude")))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return path != root


def touch(
    paths: Sequence[str],
    *,
    base: Path,
    cwd: Path | None = None,
    config_path: str | None = None,
    sync: bool = True,
    client_factory: Callable[[], LabTracker] | None = None,
) -> JsonObject:
    """Queue the watch events a scan would queue for ``paths``; drain best-effort.

    Never raises for an unwatched path, a missing or broken config, an
    unstable file, or an unreachable server: each is a normal ``action``.
    """

    payload: JsonObject = {"command": "watch-touch"}
    if not agent_hooks_enabled():
        return _done(payload, "disabled")
    if not paths:
        return _done(payload, "no-path")
    config_file = Path(config_path).expanduser() if config_path else None
    if config_file is None:
        config_file = find_checkout_watch_config(base)
    if config_file is None or not config_file.is_file():
        return _done(payload, "no-config")
    try:
        config = watch_capture.load_config(config_path=config_file)
    except Exception as exc:  # noqa: BLE001 - a broken config must not break the agent.
        _notice(f"watch touch skipped: {config_file} could not be read ({exc}).")
        return _done(payload, "config-error", config=str(config_file), detail=str(exc))
    payload["config"] = str(config.config_path)
    anchor = cwd or base
    matches: list[WatchMatch] = []
    for value in paths:
        raw = Path(value).expanduser()
        if not raw.is_absolute():
            raw = anchor / raw
        try:
            resolved = raw.resolve()
        except (OSError, RuntimeError):
            continue
        matches.extend(match_watches(config, resolved, raw=raw))
    if not matches:
        return _done(payload, "unwatched")
    results = [_queue_match(config, match) for match in matches]
    payload["results"] = results
    fresh = [item for item in results if item["action"] in {"queued", "rearmed"}]
    if fresh:
        action = "queued"
    elif any(item["action"] == "already-queued" for item in results):
        action = "already-queued"
    else:
        action = str(results[0]["action"])
    payload["action"] = action
    payload["outbox"] = str(config.outbox_path())
    if fresh and sync:
        # No server configured: queue only -- no network call and no notice.
        try:
            summary = drain_watch_outbox(
                config, client_factory=client_factory, limit=TOUCH_SYNC_LIMIT
            )
        except Exception as exc:  # noqa: BLE001 - queued events retry on the next sync.
            payload["sync_error"] = redact_secrets(str(exc))[:500]
        else:
            record_drain(payload, summary)
    return payload


def _queue_match(config: watch_capture.WatchConfig, match: WatchMatch) -> JsonObject:
    watch = match.watch
    result: JsonObject = {
        "path": str(match.path),
        "watch": str(watch.get("name") or watch.get("root") or ""),
    }
    try:
        event = _event_for_match(config, match)
    except Exception as exc:  # noqa: BLE001 - a vanished or still-changing file is not a failure.
        result.update({"action": "not-captured", "detail": str(exc)})
        return result
    if event["sink"] == watch_capture.SINK_STAGED_NOTE and not _declared_project(config, event):
        _notice(
            f"watch touch skipped {match.path}: no project is declared for it "
            "(watch.json, lt_ids.json, or LAB_TRACKER_PROJECT_ID). Nothing was queued; "
            "`lt project bind` binds this checkout."
        )
        result["action"] = "unbound"
        return result
    outbox = config.outbox_path()
    target = watch_capture.event_path(event, outbox)
    already = target.exists()
    # Same per-file step as ``watch.scan_watch``: a stale event whose source
    # again matches is re-armed rather than duplicated.
    rearmed = already and watch_capture._rearm_stale_event(target, event)
    written = watch_capture.write_event(event, outbox)
    result["event_path"] = str(written)
    result["action"] = "rearmed" if rearmed else ("already-queued" if already else "queued")
    return result


def _event_for_match(config: watch_capture.WatchConfig, match: WatchMatch) -> JsonObject:
    """Build the event exactly as ``watch.scan_configured`` would for this file."""

    watch = match.watch
    sink = str(watch.get("sink") or watch_capture.SINK_STAGED_NOTE)
    common: dict[str, Any] = {
        "sink": sink,
        "project_id": _optional(watch.get("project_id")),
        "question_id": _optional(watch.get("question_id")),
        "dataset_ids": _strings(watch.get("dataset_ids")),
        "tags": _strings(watch.get("tags")),
        "session_id": _optional(watch.get("session_id")),
    }
    if str(watch.get("mode") or watch_capture.MODE_FILES) == watch_capture.MODE_MANIFEST:
        return watch_capture.event_from_manifest(config, match.path, **common)
    acquisition = sink == watch_capture.SINK_ACQUISITION_OUTPUT
    default_adapter = "lt-watch-acquisition" if acquisition else "lt-watch-files"
    return watch_capture.event_from_file(
        config,
        match.path,
        root=match.root,
        source_provider=_optional(watch.get("source_provider")) or "local-folder",
        adapter=_optional(watch.get("adapter")) or default_adapter,
        **common,
    )


def _declared_project(config: watch_capture.WatchConfig, event: Mapping[str, Any]) -> str | None:
    context = event.get("context") if isinstance(event.get("context"), Mapping) else {}
    configured = _optional(context.get("project_id")) if isinstance(context, Mapping) else None
    # client=None: a connection profile's default project never counts here.
    return watch_capture.resolve_default_project_id(
        config.checkout_root(), None, configured=configured
    )


def touch_from_hook(
    hook: Mapping[str, Any],
    *,
    paths: Sequence[str] = (),
    repo: str | None = None,
    config_path: str | None = None,
    sync: bool = True,
    client_factory: Callable[[], LabTracker] | None = None,
) -> JsonObject:
    """``lt watch touch``: explicit ``paths`` or the path in an agent hook payload.

    Explicit paths are relative to the current directory, like any command
    argument; a hook's path is relative to the hook's ``cwd``.
    """

    base = hook_base_dir(hook, repo)
    return touch(
        [os.path.abspath(os.path.expanduser(path)) for path in paths] or hook_paths(hook),
        base=base,
        cwd=hook_cwd(hook, base),
        config_path=config_path,
        sync=sync,
        client_factory=client_factory,
    )


def _done(payload: JsonObject, action: str, **extra: Any) -> JsonObject:
    payload.update({"action": action, **extra})
    return payload


def _notice(message: str) -> None:
    print(f"lab-tracker: {message}", file=sys.stderr)


def _optional(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    return []


__all__ = [
    "TOUCH_SYNC_LIMIT",
    "WatchMatch",
    "find_checkout_watch_config",
    "hook_paths",
    "match_watches",
    "touch",
    "touch_from_hook",
]
