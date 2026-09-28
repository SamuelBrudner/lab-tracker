"""`lt capture file`: capture one saved file through the fail-soft figure core.

This is the language-neutral entry point other runtimes shell out to (the R
``labtracker`` package's autotrack hook, MATLAB or shell pipelines). It runs
exactly the capture ``lab_tracker_client.savefig`` runs for a file that is
already on disk: a staged note with a content hash, host and session
metadata, offline queueing to the checkout's watch outbox, and the per-process
circuit breaker.

Contract:

* stdout is the ``FigureCaptureResult`` dict as JSON, plus ``notices``: the
  stderr notices the capture printed (so a caller that discards stderr, such
  as a background process, can still show each one once);
* the exit status is 0 for every fail-soft outcome (``imported``,
  ``coalesced``, ``queued``, ``skipped``, ``failed``) and nonzero only for a
  usage error (argparse exits 2);
* ``--require-bound`` applies autotrack's rule: the save is captured only when
  its project comes from ``--project``, ``LAB_TRACKER_PROJECT_ID`` or the
  file's checkout binding (``lt_ids.json``); otherwise nothing is sent or
  queued and the result is ``skipped`` with reason ``project_unbound``;
* ``--output PATH`` also writes the same JSON to ``PATH`` atomically, so a
  caller that ran the command in the background knows when it finished.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any

from lab_tracker.models import NoteMetadataScalar
from lab_tracker_client import figure as _figure
from lab_tracker_client.outbox import write_json_atomic

# ``kind`` names metadata keys (``<kind>_client_capture_id``) and the adapter
# (``lab-tracker-client-<kind>``), so it is a plain lowercase token.
_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_BOOL_VALUES = {"true": True, "false": False}


def add_capture_parsers(subcommands: argparse._SubParsersAction) -> None:
    """Register ``lt capture file``."""

    capture_parser = subcommands.add_parser(
        "capture",
        help="Capture saved files as staged evidence (fail-soft; for scripts and other runtimes).",
    )
    capture_commands = capture_parser.add_subparsers(dest="capture_command", required=True)
    file_parser = capture_commands.add_parser(
        "file",
        help=(
            "Capture one saved file as a staged note (content hash, host and session "
            "metadata; queued offline when the server is unreachable). Prints the "
            "capture result as JSON and exits 0 for every capture outcome; only a "
            "usage error exits nonzero."
        ),
    )
    file_parser.add_argument("path", help="The saved file to capture.")
    file_parser.add_argument(
        "--kind",
        type=_kind,
        default="figure",
        help="Evidence kind label, a lowercase token (default: figure).",
    )
    file_parser.add_argument(
        "--project",
        default=None,
        help=(
            "Project UUID. Defaults to LAB_TRACKER_PROJECT_ID, then the file's checkout "
            "binding (lt_ids.json)."
        ),
    )
    file_parser.add_argument(
        "--logical-id",
        default=None,
        help="Stable logical id for the capture (default: the path relative to the cwd).",
    )
    file_parser.add_argument(
        "--metadata",
        action="append",
        type=parse_metadata_item,
        default=[],
        metavar="KEY=VALUE",
        help=(
            "Scalar note metadata; repeatable. true/false and numbers that print back "
            "unchanged are typed, anything else is a string."
        ),
    )
    file_parser.add_argument(
        "--require-bound",
        action="store_true",
        help=(
            "Capture only when the project comes from --project, LAB_TRACKER_PROJECT_ID, "
            "or the checkout's lt_ids.json (the autotrack rule); otherwise skip without "
            "sending or queueing anything."
        ),
    )
    file_parser.add_argument(
        "--output",
        default=None,
        help="Also write the JSON result to this file atomically (for background callers).",
    )
    file_parser.set_defaults(func=_cmd_capture_file, needs_client=False)


def parse_metadata_item(text: str) -> tuple[str, NoteMetadataScalar]:
    """Parse one ``KEY=VALUE`` metadata item; an argparse ``type``.

    A value is typed only when it prints back identically: ``true``/``false``
    become booleans and canonical integers or floats become numbers, while
    ``007``, ``1.50`` or ``True`` stay strings.
    """

    key, separator, raw = str(text).partition("=")
    key = key.strip()
    if not separator or not key:
        raise argparse.ArgumentTypeError(f"metadata must be KEY=VALUE, got {text!r}")
    return key, _typed_value(raw)


def _typed_value(raw: str) -> NoteMetadataScalar:
    if raw in _BOOL_VALUES:
        return _BOOL_VALUES[raw]
    for parse in (int, float):
        try:
            value = parse(raw)
        except ValueError:
            continue
        if str(value) == raw:
            return value
    return raw


def _kind(text: str) -> str:
    if not _KIND_PATTERN.fullmatch(str(text)):
        raise argparse.ArgumentTypeError(
            f"kind must be a lowercase token such as 'figure' or 'table', got {text!r}"
        )
    return str(text)


def _cmd_capture_file(args: argparse.Namespace) -> dict[str, Any]:
    notices = io.StringIO()
    with redirect_stderr(notices):
        result = _figure._capture_saved_figure(
            fig=None,
            path=Path(args.path),
            client=None,
            project_id=args.project,
            logical_id=args.logical_id,
            metadata=dict(args.metadata) or None,
            preview_max_bytes=_figure.FIGURE_PREVIEW_MAX_BYTES,
            version_every_change=False,
            kind=args.kind,
            require_bound_project=args.require_bound,
        )
    printed = notices.getvalue()
    # Re-emit what the capture said, so a person at a terminal still sees it.
    sys.stderr.write(printed)
    payload = result.to_dict()
    payload["notices"] = [line for line in printed.splitlines() if line.strip()]
    if args.output:
        _write_output(Path(args.output), payload)
    return payload


def _write_output(path: Path, payload: dict[str, Any]) -> None:
    try:
        write_json_atomic(path.expanduser(), json.loads(json.dumps(payload, default=str)))
    except OSError as exc:
        print(f"lab-tracker: could not write the capture result to {path}: {exc}", file=sys.stderr)


__all__ = ["add_capture_parsers", "parse_metadata_item"]
