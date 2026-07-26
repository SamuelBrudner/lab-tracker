"""Isolated, output-free directory predicate for local store health.

The application executes this file directly with an isolated Python interpreter.
It deliberately imports only the standard library and communicates exclusively
through its exit status.  Parent-side policy authorization is preliminary: this
helper rejects a final link or reparse point, but it does not claim handle-bound
protection against an intermediate-component retargeting race.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Mapping, Sequence

LOCAL_STORE_HEALTH_ROOT_ENV = "LAB_TRACKER_INTERNAL_LOCAL_STORE_HEALTH_ROOT"

_HEALTHY_EXIT = 0
_UNREACHABLE_EXIT = 1


def _is_plain_directory(path: str) -> bool:
    try:
        metadata = os.stat(path, follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode):
            return False
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        attributes = getattr(metadata, "st_file_attributes", 0)
        return not bool(reparse_flag and attributes & reparse_flag)
    except BaseException:
        return False


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Return zero only for the one valid, plain-directory protocol request."""

    try:
        arguments = sys.argv if argv is None else argv
        environment = os.environ if environ is None else environ
        if len(arguments) != 1:
            return _UNREACHABLE_EXIT
        root = environment.get(LOCAL_STORE_HEALTH_ROOT_ENV)
        if not root or "\0" in root:
            return _UNREACHABLE_EXIT
        return _HEALTHY_EXIT if _is_plain_directory(root) else _UNREACHABLE_EXIT
    except BaseException:
        return _UNREACHABLE_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
