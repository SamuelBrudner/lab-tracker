"""``lt hpc epilog``: finish a Slurm job's run from a TaskEpilog, with no job-script edits.

An administrator installs ``scripts/slurm-task-epilog.sh`` as the cluster's
``TaskEpilog``; for every job submitted through ``lt hpc submit`` it calls
this verb as the job user after the batch step ends. The verb reads Slurm's
own environment and the run manifest ``lt hpc submit`` wrote into the submit
directory, then writes the same ``finish`` event ``lt hpc finish`` would --
unless the job already finished its run itself. It never contacts the server:
the scheduled ``lt watch run`` / ``lt hpc sync`` on a login node drains it.
"""

from __future__ import annotations

import argparse
from typing import Any

from lab_tracker_client.hpc import epilog_finish


def add_hpc_epilog_parser(hpc_commands: argparse._SubParsersAction[Any]) -> None:
    """Register ``lt hpc epilog`` under the ``lt hpc`` command group."""

    parser = hpc_commands.add_parser(
        "epilog",
        help="Finish the current Slurm job's 'lt hpc submit' run (for a TaskEpilog).",
    )
    parser.add_argument(
        "--config", help="Config path. Defaults to the one the job was submitted with."
    )
    parser.add_argument(
        "--exit-code",
        type=int,
        help="Job exit code (default: SLURM_JOB_EXIT_CODE2/SLURM_JOB_EXIT_CODE when set).",
    )
    parser.add_argument(
        "--log",
        action="append",
        default=[],
        help="Log file to excerpt (default: slurm-<job>.out in the submit directory, if present).",
    )
    parser.add_argument(
        "--fail-silent",
        action="store_true",
        help="Exit 0 with no output on any error (for epilog scripts).",
    )
    parser.set_defaults(func=_cmd_hpc_epilog, needs_client=False)


def _cmd_hpc_epilog(args: argparse.Namespace) -> Any:
    try:
        return epilog_finish(exit_code=args.exit_code, logs=args.log, config_path=args.config)
    except Exception as exc:  # noqa: BLE001 - one clean line, never a traceback.
        if args.fail_silent:
            return None
        raise SystemExit(f"lt hpc epilog: {exc}") from None


__all__ = ["add_hpc_epilog_parser"]
