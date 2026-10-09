"""Maintenance CLI and portable scheduler artifact generation."""

from __future__ import annotations

import argparse
import hashlib
import json
import plistlib
import shlex
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from lab_tracker.maintenance.config import MaintenanceConfig, load_config
from lab_tracker.maintenance.coordinator import run_once, status, write_private


def add_parser(subcommands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subcommands.add_parser(
        "maintenance", help="Check deployed instances and prepare supervised maintenance reviews."
    )
    actions = parser.add_subparsers(dest="maintenance_action", required=True)
    for name, help_text in (
        ("run", "Run a due maintenance pass and persist its findings."),
        ("status", "Read the last durable report without network requests."),
        ("schedule", "Generate a cron line or launchd plist for the operator host."),
        ("evaluate", "Compare explicit graph models on synthetic fixtures."),
    ):
        action = actions.add_parser(name, help=help_text)
        action.add_argument(
            "--config", type=Path, required=True, help="Nonsecret deployment inventory."
        )
        if name in {"run", "status"}:
            action.add_argument("--json", action="store_true")
        if name == "run":
            action.add_argument(
                "--force", action="store_true", help="Run even before the next due time."
            )
            action.add_argument(
                "--quiet",
                action="store_true",
                help="Print only failures or recoveries that changed.",
            )
        elif name == "schedule":
            action.add_argument("--format", choices=["cron", "launchd"], required=True)
            action.add_argument(
                "--python", type=Path, required=True, help="Absolute installed Python path."
            )
            action.add_argument(
                "--output", type=Path, required=True, help="Write scheduler artifact here."
            )
        elif name == "evaluate":
            action.add_argument("--baseline", required=True)
            action.add_argument("--candidate", required=True)
            action.add_argument(
                "--provider-env", type=Path, help="Existing private provider settings file."
            )
            action.add_argument("--repeat", type=int, default=3)
            action.add_argument("--timeout-seconds", type=float, default=600)
            action.add_argument("--reasoning-effort")
            action.add_argument("--reasoning-mode")
            action.add_argument("--candidate-reasoning-effort")
            action.add_argument("--candidate-reasoning-mode")
            action.add_argument("--min-precision", type=float, default=0.8)
            action.add_argument("--min-recall", type=float, default=0.8)
            action.add_argument("--max-quality-regression", type=float, default=0.02)
            action.add_argument("--max-duplicate-rate", type=float, default=0.05)
            action.add_argument("--max-latency-ratio", type=float, default=2.0)
            mode = action.add_mutually_exclusive_group(required=True)
            mode.add_argument(
                "--live", action="store_true", help="Allow at most ten paid synthetic drafts."
            )
            mode.add_argument(
                "--scripted", action="store_true", help="Exercise the harness without a provider."
            )


def scheduler_artifact(
    config_path: Path, python_path: Path, state_dir: Path, interval: int, format: str
) -> str:
    if not python_path.is_absolute() or not python_path.is_file():
        raise ValueError("Scheduler Python must name an existing absolute executable.")
    arguments = [
        str(python_path),
        "-m",
        "lab_tracker.maintenance",
        "run",
        "--config",
        str(config_path.resolve()),
        "--quiet",
    ]
    docker = shutil.which("docker")
    search_path = ":".join(
        dict.fromkeys(
            [
                str(python_path.parent),
                *([str(Path(docker).parent)] if docker else []),
                "/usr/local/bin",
                "/opt/homebrew/bin",
                "/usr/bin",
                "/bin",
            ]
        )
    )
    paths = arguments + [str(state_dir), search_path]
    if any("\n" in item or "\r" in item for item in paths):
        raise ValueError("Scheduler paths must not contain newlines.")
    if format == "cron":
        # Invoke each minute; run_once enforces arbitrary intervals and serializes overlap.
        if any("%" in item for item in paths):
            raise ValueError("Cron paths must not contain percent signs.")
        return (
            "* * * * * PATH="
            + shlex.quote(search_path)
            + " "
            + shlex.join(arguments)
            + " >> "
            + shlex.quote(str(state_dir / "scheduler.log"))
            + " 2>&1\n"
        )
    return plistlib.dumps(
        {
            "Label": "com.lab-tracker.maintenance",
            "ProgramArguments": arguments,
            "StartInterval": interval,
            "RunAtLoad": True,
            "EnvironmentVariables": {"PATH": search_path},
            "StandardOutPath": str(state_dir / "scheduler.log"),
            "StandardErrorPath": str(state_dir / "scheduler-errors.log"),
        }
    ).decode()


def run_command(args: argparse.Namespace) -> int:
    config: MaintenanceConfig | None = None
    try:
        config = load_config(args.config)
        if args.maintenance_action == "schedule":
            # The scheduler's log directory must exist before launchd/cron opens it.
            config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            artifact = scheduler_artifact(
                args.config, args.python, config.state_dir, config.interval_seconds, args.format
            )
            write_private(args.output, artifact)
            print(str(args.output.resolve()))
            return 0
        if args.maintenance_action == "evaluate":
            from lab_tracker.bounded_subprocess import BoundedSubprocessExecutor, ProcessDeadline
            from lab_tracker.maintenance.evaluation import EvaluationGates

            if not 0 < args.timeout_seconds <= 3600:
                raise ValueError("Evaluation deadline must be at most one hour.")
            gates = EvaluationGates(
                min_precision=args.min_precision,
                min_recall=args.min_recall,
                max_quality_regression=args.max_quality_regression,
                max_duplicate_rate=args.max_duplicate_rate,
                max_latency_ratio=args.max_latency_ratio,
            )
            gates.validate()
            parameters = {
                "baseline": args.baseline,
                "candidate": args.candidate,
                "provider_env": str(args.provider_env.resolve()) if args.provider_env else None,
                "repeat": args.repeat,
                "scripted": args.scripted,
                "reasoning_effort": args.reasoning_effort,
                "reasoning_mode": args.reasoning_mode,
                "candidate_reasoning_effort": args.candidate_reasoning_effort,
                "candidate_reasoning_mode": args.candidate_reasoning_mode,
                "gates": vars(gates),
                "live": args.live,
            }
            result = BoundedSubprocessExecutor().run(
                [
                    sys.executable,
                    "-m",
                    "lab_tracker.maintenance.evaluation",
                    json.dumps(parameters),
                ],
                deadline=ProcessDeadline.after(args.timeout_seconds),
                stdout_limit_bytes=1024 * 1024,
                stderr_limit_bytes=65536,
            )
            if result.returncode != 0:
                raise ValueError("Evaluation failed; no passing evidence was produced.")
            report = json.loads(result.stdout)
            report["requested_baseline"] = args.baseline
            report["requested_candidate"] = args.candidate
            report["scripted"] = args.scripted
            report["checked_at"] = datetime.now(timezone.utc).isoformat()
            # A dry harness result can never become candidate upgrade evidence.
            report["usable_upgrade_evidence"] = report["passed"] and not args.scripted
            identifier = hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()[
                :20
            ]
            output = config.state_dir / "evaluations" / f"{identifier}.json"
            write_private(output, json.dumps(report, indent=2) + "\n")
            print(json.dumps({"report": str(output), **report}, indent=2))
            if not report.get("completed"):
                return 2
            return 0 if report["passed"] else 1
        report = (
            status(config)
            if args.maintenance_action == "status"
            else run_once(config, force=args.force)
        )
        meaningful = bool(
            report.get("changed") or report.get("resolved") or report.get("new_evaluations")
        )
        if not getattr(args, "quiet", False) or meaningful:
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                print(f"Maintenance: {report['status']}; attention: {report['attention_needed']}")
                for key in report.get("changed", []):
                    print(f"  {key}: {report['findings'][key]['summary']}")
                for key in report.get("resolved", []):
                    print(f"  Recovered: {key}")
                for path in report.get("new_evaluations", []):
                    print(f"  Evaluation evidence: {path}")
                if meaningful and report.get("proposal"):
                    print(f"  Review: {report['proposal']}")
        return 1 if report["attention_needed"] else 0
    except Exception as exc:
        if config is not None and args.maintenance_action == "evaluate":
            report = {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "completed": False,
                "passed": False,
                "usable_upgrade_evidence": False,
                "requested_baseline": args.baseline,
                "requested_candidate": args.candidate,
                "error_type": type(exc).__name__,
                "approval_required": True,
                "cost_verified": False,
                "scripted": args.scripted,
            }
            identifier = hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()[
                :20
            ]
            output = config.state_dir / "evaluations" / f"{identifier}.json"
            try:
                write_private(output, json.dumps(report, indent=2) + "\n")
                print(json.dumps({"report": str(output), **report}, indent=2))
            except OSError:
                pass
        # Provider errors, validation inputs and filesystem exceptions can contain secrets.
        print(
            "Maintenance command failed; check inventory, operator access and provider settings.",
            file=sys.stderr,
        )
        return 2
