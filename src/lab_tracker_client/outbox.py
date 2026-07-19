"""Shared, typed outbox drain mechanics for capture adapters.

The watch, repo, and HPC adapters each maintain a local outbox directory of
durable event files that later sync into Lab Tracker. They historically each
carried a near-identical drain loop that shared three latent bugs:

* the event file was decoded *before* the per-record recovery boundary, so one
  malformed/unsupported record aborted every later record in the batch;
* the oldest ``limit`` files were sliced *before* terminal states were filtered,
  so an already-synced oldest record could starve pending work forever under a
  limit; and
* one copy used a non-unique temp filename, so concurrent writers could truncate
  each other's records.

This module owns the corrected drain once. Each adapter injects its own event
codec, actionability predicate, and result/failure builders, keeping the shared
mechanism narrow (drain mechanics only) while the domain shapes stay concrete.

Key guarantees:

* Each record is decoded, classified, and processed inside a per-record
  boundary. An unreadable record is quarantined (moved out of the ``*.json``
  drain set with a durable ``.error`` sidecar) and reported, never aborting the
  batch.
* Actionable records are selected *before* ``limit`` is applied, so
  ``limit`` bounds the number of actionable records processed and terminal
  records can never consume the budget.
* Writes go through a unique-temp atomic replace, and the whole drain runs under
  a best-effort advisory lock so concurrent drainers do not double-process.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]

QUARANTINE_SUFFIX = ".quarantine"
ERROR_SUFFIX = ".error"
LOCK_FILENAME = ".lock"


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically write ``payload`` as JSON via a process-unique temp file.

    The temp name includes a uuid so two concurrent writers targeting the same
    event file cannot write to the same temp path and truncate each other before
    the atomic ``replace``.
    """

    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def list_event_files(outbox: str | Path) -> list[Path]:
    """Return the sorted ``*.json`` event files in ``outbox``.

    Quarantined events (``*.json.quarantine``), error sidecars, temp files, and
    the lock file all fall outside the ``*.json`` glob, so they are never
    re-drained or counted as events.
    """

    path = Path(outbox).expanduser()
    if not path.exists():
        return []
    return sorted(item for item in path.glob("*.json") if item.is_file())


def count_quarantined(outbox: str | Path) -> int:
    """Count quarantined (unreadable, moved-aside) event files in ``outbox``."""

    path = Path(outbox).expanduser()
    if not path.exists():
        return 0
    return sum(1 for item in path.glob(f"*{QUARANTINE_SUFFIX}") if item.is_file())


def quarantine_event(path: Path, error: str) -> Path:
    """Move an unreadable event out of the drain set and record why, durably.

    The poison file's bytes are preserved (renamed to ``<name>.quarantine``) and
    a companion ``<name>.error`` sidecar captures the decode error, so a
    scheduled ``--fail-silent`` run leaves an observable on-disk failure signal
    even when console output is suppressed.
    """

    quarantined = path.with_name(path.name + QUARANTINE_SUFFIX)
    with suppress(OSError):
        os.replace(path, quarantined)
    sidecar = path.with_name(path.name + ERROR_SUFFIX)
    with suppress(OSError):
        sidecar.write_text(
            json.dumps(
                {
                    "original_path": str(path),
                    "quarantined_path": str(quarantined),
                    "error": error,
                    "quarantined_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return quarantined


def _acquire_lock(handle: Any) -> bool:
    """Try to take a non-blocking exclusive lock. True if held (or unsupported)."""

    try:
        import fcntl
    except ImportError:
        fcntl = None  # type: ignore[assignment]
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    try:
        import msvcrt
    except ImportError:
        msvcrt = None  # type: ignore[assignment]
    if msvcrt is not None:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    # No advisory locking available on this platform: proceed best-effort. The
    # unique-temp atomic write still prevents record corruption.
    return True


def _release_lock(handle: Any) -> None:
    with suppress(ImportError, OSError):
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    with suppress(ImportError, OSError):
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def outbox_lock(outbox: Path) -> Iterator[bool]:
    """Best-effort advisory exclusive lock over an outbox directory.

    Yields ``True`` when the lock is held (or locking is unavailable and the
    drain proceeds best-effort) and ``False`` when another drainer holds it, so
    the caller can skip rather than double-process. Never raises for
    lock-unsupported filesystems.
    """

    outbox.mkdir(parents=True, exist_ok=True)
    lock_path = outbox / LOCK_FILENAME
    try:
        handle = open(lock_path, "a+")  # noqa: SIM115 - closed in finally
    except OSError:
        # Cannot even create the lock file: proceed best-effort.
        yield True
        return
    acquired = False
    try:
        acquired = _acquire_lock(handle)
        yield acquired
    finally:
        if acquired:
            _release_lock(handle)
        handle.close()


def drain_outbox(
    *,
    outbox: Path,
    command: str,
    dry_run: bool,
    request_draft: bool,
    limit: int | None,
    read_event: Callable[[Path], JsonObject],
    is_actionable: Callable[[JsonObject], bool],
    process: Callable[[Path, JsonObject], JsonObject],
    on_skipped: Callable[[Path, JsonObject], JsonObject],
    on_failure: Callable[[Path, JsonObject, Exception], JsonObject],
) -> JsonObject:
    """Drain an outbox with per-record recovery and select-before-limit ordering.

    Callbacks:
      * ``read_event``   decode+validate one event file (may raise on malformed).
      * ``is_actionable`` True if the event should be processed this run (i.e.
        not terminal, or terminal-but-needs-draft).
      * ``process``      sync one actionable event; return its result dict. A
        truthy ``error`` key routes the result into ``errors`` too.
      * ``on_skipped``   build the ``skipped``/``already_synced`` result dict for
        a non-actionable (terminal) event.
      * ``on_failure``   record the failure durably and build the ``failed``
        result dict when ``process`` raises.
    """

    outbox = Path(outbox).expanduser()
    results: list[JsonObject] = []
    errors: list[JsonObject] = []
    quarantined = 0
    processed_actionable = 0

    with outbox_lock(outbox) as acquired:
        if not acquired:
            return _summary(
                command=command,
                outbox=outbox,
                dry_run=dry_run,
                request_draft=request_draft,
                results=results,
                errors=errors,
                locked=True,
            )
        for path in list_event_files(outbox):
            try:
                event = read_event(path)
            except Exception as exc:  # noqa: BLE001 - quarantine, keep draining.
                quarantine_event(path, str(exc))
                quarantined += 1
                quarantine_result: JsonObject = {
                    "action": "quarantined",
                    "path": str(path),
                    "reason": "unreadable_event",
                    "error": str(exc),
                }
                results.append(quarantine_result)
                errors.append(quarantine_result)
                continue
            if not is_actionable(event):
                results.append(on_skipped(path, event))
                continue
            # Actionable records are selected before the limit is spent, so a
            # terminal-first record can never starve pending work.
            if limit is not None and processed_actionable >= limit:
                break
            processed_actionable += 1
            try:
                result = process(path, event)
                results.append(result)
                if result.get("error"):
                    errors.append(result)
            except Exception as exc:  # noqa: BLE001 - per-record recovery boundary.
                failed = on_failure(path, event, exc)
                results.append(failed)
                errors.append(failed)

    return _summary(
        command=command,
        outbox=outbox,
        dry_run=dry_run,
        request_draft=request_draft,
        results=results,
        errors=errors,
        quarantined=quarantined,
    )


def _summary(
    *,
    command: str,
    outbox: Path,
    dry_run: bool,
    request_draft: bool,
    results: list[JsonObject],
    errors: list[JsonObject],
    quarantined: int = 0,
    locked: bool = False,
) -> JsonObject:
    summary: JsonObject = {
        "command": command,
        "outbox": str(outbox),
        "dry_run": dry_run,
        "request_draft": request_draft,
        "processed": len(results),
        "results": results,
        "errors": errors,
    }
    if quarantined:
        summary["quarantined"] = quarantined
    if locked:
        summary["locked"] = True
    return summary
