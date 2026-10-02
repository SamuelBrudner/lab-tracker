"""Run client upgrade, repo checks, previewed updates, and verification as one job.

This module uses only the standard library. The checkout bootstrap executes it
directly, so even an older or broken installed client can be upgraded. Every
doctor and update runs in a fresh process using the selected installation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

UPSTREAM = "https://github.com/SamuelBrudner/lab-tracker"
REVISION = re.compile(r"[0-9a-f]{40}", re.IGNORECASE)
CONVENTIONS = {"CLAUDE.md", "AGENTS.md", ".cursor/rules/lab-tracker.mdc"}
INSTALL_PROBE = """\
import importlib.metadata, json
d = importlib.metadata.distribution('lab-tracker')
print(d.read_text('direct_url.json') or '{}')
"""


class MaintenanceError(RuntimeError):
    """A failed step whose message contains no subprocess output or credentials."""


def add_parser_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", action="append", default=[], help="Repo path; repeat for a set.")
    parser.add_argument("--all", action="store_true", help="Include all registered repos.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--yes", action="store_true", help="Apply the previewed updates.")
    mode.add_argument("--dry-run", action="store_true", help="Preview only (the default).")
    parser.add_argument(
        "--upgrade-client",
        action="store_true",
        help="Check GitHub main and upgrade a uv tool install when its commit differs.",
    )
    parser.add_argument(
        "--revision",
        help="With --upgrade-client, use this full Git revision instead of main.",
    )
    parser.add_argument(
        "--refresh-skills",
        action="store_true",
        help="Also preview/refresh machine setup skills.",
    )
    parser.add_argument("--lt-path", help="Installed lt executable; defaults to the lt on PATH.")


def _run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MaintenanceError(f"{Path(argv[0]).name} timed out after {timeout} seconds.") from exc
    except OSError as exc:
        raise MaintenanceError(
            f"Could not start {Path(argv[0]).name}: {type(exc).__name__}."
        ) from exc


def _json_command(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    doctor: bool = False,
) -> tuple[dict[str, Any], int]:
    result = _run(argv, cwd=cwd, env=env)
    if result.returncode not in ({0, 1} if doctor else {0}):
        raise MaintenanceError(f"{Path(argv[0]).name} exited with status {result.returncode}.")
    try:
        payload = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise MaintenanceError("The command did not return a JSON report.") from exc
    if not isinstance(payload, dict):
        raise MaintenanceError("The command returned a JSON report of the wrong type.")
    if doctor and payload.get("command") != "doctor":
        raise MaintenanceError("The command did not return a doctor report.")
    if doctor and (
        not isinstance(payload.get("targets"), list)
        or not all(isinstance(target, dict) for target in payload["targets"])
        or any(
            key in payload and not isinstance(payload[key], dict)
            for key in ("lt_mcp", "server", "client")
        )
    ):
        raise MaintenanceError("The doctor report has an invalid schema.")
    if (
        len(argv) > 1
        and argv[1] == "update"
        and (
            not isinstance(payload.get("created"), list)
            or not isinstance(payload.get("overwritten"), list)
            or not isinstance(payload.get("diffs"), dict)
        )
    ):
        raise MaintenanceError("The update report has an invalid schema.")
    return payload, result.returncode


def _roots(args: argparse.Namespace) -> list[Path]:
    if not args.repo and not args.all:
        raise MaintenanceError("Choose --all or at least one --repo.")
    roots = list(args.repo)
    if args.all:
        config_dir = Path(os.environ.get("LAB_TRACKER_CONFIG_DIR", "~/.lab-tracker")).expanduser()
        registry = config_dir / "applied-repos.json"
        if registry.exists():
            try:
                payload = json.loads(registry.read_text(encoding="utf-8"))
                if payload.get("version") != 1 or not isinstance(payload.get("repos"), list):
                    raise ValueError("Invalid registry schema")
                for entry in payload["repos"]:
                    if (
                        not isinstance(entry, dict)
                        or not isinstance(entry.get("root"), str)
                        or not entry["root"].strip()
                        or not Path(entry["root"]).expanduser().is_absolute()
                    ):
                        raise ValueError("Invalid registry entry")
                    roots.append(entry["root"])
            except (OSError, ValueError, AttributeError) as exc:
                raise MaintenanceError("The applied-repos registry could not be read.") from exc
    resolved = list(dict.fromkeys(Path(root).expanduser().resolve() for root in roots))
    if not resolved:
        raise MaintenanceError("No repos selected; supply --repo or enroll repos first.")
    return resolved


def _installation(lt: Path, *, cwd: Path, env: dict[str, str]) -> dict[str, Any]:
    # Upgrades must target the selected uv tool, not an unrelated pip/conda install.
    tool = lt.resolve().parent.parent
    if not (tool / "uv-receipt.toml").is_file() or tool.name != "lab-tracker":
        raise MaintenanceError("Client upgrade requires a lab-tracker uv tool installation.")
    python = lt.resolve().parent / ("python.exe" if os.name == "nt" else "python3")
    if not python.is_file() and os.name != "nt":
        python = lt.resolve().parent / "python"
    metadata, _ = _json_command([str(python), "-c", INSTALL_PROBE], cwd=cwd, env=env)
    vcs = metadata.get("vcs_info")
    directory = metadata.get("dir_info", {})
    if (
        str(metadata.get("url", "")).removesuffix(".git").rstrip("/") != UPSTREAM
        or not isinstance(directory, dict)
        or directory.get("editable")
        or not isinstance(vcs, dict)
        or vcs.get("vcs") != "git"
        or not REVISION.fullmatch(str(vcs.get("commit_id", "")))
    ):
        raise MaintenanceError(
            "Client upgrade requires an install from the Lab Tracker GitHub repo."
        )
    return {"revision": vcs["commit_id"].lower(), "tool": tool}


def _upgrade(
    args: argparse.Namespace,
    lt: Path,
    *,
    cwd: Path,
    env: dict[str, str],
) -> dict[str, Any]:
    installed = _installation(lt, cwd=cwd, env=env)
    revision = args.revision
    if revision is None:
        result = _run(
            ["git", "ls-remote", UPSTREAM + ".git", "refs/heads/main"], cwd=cwd, env=env, timeout=30
        )
        if result.returncode:
            raise MaintenanceError("Could not read GitHub main; client upgrade was not attempted.")
        lines = result.stdout.splitlines()
        fields = lines[0].split() if len(lines) == 1 else []
        if len(fields) != 2 or fields[1] != "refs/heads/main":
            raise MaintenanceError("GitHub did not return a single main revision.")
        revision = fields[0]
    if not REVISION.fullmatch(revision):
        raise MaintenanceError("Client revision must be a full 40-character Git SHA.")
    revision = revision.lower()
    payload: dict[str, Any] = {
        "installed_revision": installed["revision"],
        "target_revision": revision,
    }
    if installed["revision"] == revision:
        payload["status"] = "current"
        return payload
    uv = shutil.which("uv")
    if not uv:
        raise MaintenanceError("uv is required to upgrade this client.")
    # An overridden uv tools directory must point at this very installation.
    result = _run([uv, "tool", "dir"], cwd=cwd, env=env, timeout=30)
    if result.returncode or Path(result.stdout.strip()).resolve() != installed["tool"].parent:
        raise MaintenanceError("uv tool dir does not contain the selected lt installation.")
    argv = [uv, "tool", "install", "--force", "--from", f"git+{UPSTREAM}@{revision}", "lab-tracker"]
    payload.update(status="would-update", argv=argv)
    if not args.yes:
        return payload
    result = _run(argv, cwd=cwd, env=env, timeout=300)
    if result.returncode:
        raise MaintenanceError(
            f"Client install failed with status {result.returncode}; no repos updated."
        )
    verified = _installation(lt, cwd=cwd, env=env)
    if verified["revision"] != revision:
        raise MaintenanceError("Installed client revision differs from the requested revision.")
    payload["status"] = "updated"
    return payload


def _update_summary(payload: dict[str, Any], *, preview: bool = False) -> dict[str, Any]:
    # Existing customised files can contain credentials. Keep raw diffs and
    # child stderr out of the aggregate report; the listed preview argv lets
    # an operator inspect the complete lt update diff locally when needed.
    summary = {
        key: payload.get(key, []) for key in ("created", "overwritten", "warnings", "offers")
    }
    if preview:
        # Updating two blocks can record intermediate writes that cancel out.
        # List only files whose final preview differs from the original file.
        for key in ("created", "overwritten"):
            summary[key] = list(
                dict.fromkeys(path for path in summary[key] if path in payload["diffs"])
            )
    return summary


def _doctor_summary(payload: dict[str, Any], exit_code: int) -> dict[str, Any]:
    return {
        "exit_code": exit_code,
        "package_version": payload.get("package_version"),
        "targets": payload.get("targets", []),
        "lt_mcp": {"importable": (payload.get("lt_mcp") or {}).get("importable")},
        "server_reachable": (payload.get("server") or {}).get("reachable"),
        "client": payload.get("client"),
        "warnings": payload.get("warnings", []),
    }


def _doctor_clean(payload: dict[str, Any], exit_code: int) -> bool:
    targets = payload.get("targets")
    return (
        exit_code == 0
        and isinstance(targets, list)
        and all(isinstance(target, dict) for target in targets)
        and {target.get("name") for target in targets} == CONVENTIONS
        and all(target.get("present") and target.get("in_sync") for target in targets)
        and isinstance(payload.get("lt_mcp"), dict)
        and payload["lt_mcp"].get("importable") is True
    )


def _binding(root: Path) -> str:
    try:
        payload = json.loads((root / "lt_ids.json").read_text(encoding="utf-8"))
        UUID(payload["project_id"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return "needs_project_binding"
    return "bound"


def maintain(args: argparse.Namespace) -> dict[str, Any]:
    report: dict[str, Any] = {
        "command": "maintain",
        "dry_run": not args.yes,
        "client_update": {"status": "not_requested"},
        "repos": [],
        "errors": [],
    }
    try:
        if args.revision and not args.upgrade_client:
            raise MaintenanceError("--revision requires --upgrade-client.")
        if args.revision and not REVISION.fullmatch(args.revision):
            raise MaintenanceError("Client revision must be a full 40-character Git SHA.")
        roots = _roots(args)
        executable = args.lt_path or shutil.which("lt")
        if not executable:
            raise MaintenanceError("lt is not installed; supply --lt-path or install the client.")
        lt = Path(executable).expanduser().absolute()
        if not lt.is_file():
            raise MaintenanceError("The selected lt executable does not exist.")
        # A source checkout/PYTHONPATH must never make a fresh process test old
        # source code instead of the newly installed tool.
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        env["PYTHONSAFEPATH"] = "1"
        for key in ("LAB_TRACKER_CONFIG_DIR", "LAB_TRACKER_SKILLS_HOME"):
            if env.get(key):
                env[key] = str(Path(env[key]).expanduser().resolve())
        with tempfile.TemporaryDirectory(prefix="lt-maintain-") as temp:
            cwd = Path(temp)
            if args.upgrade_client:
                report["client_update"] = {"status": "checking"}
                report["client_update"] = _upgrade(args, lt, cwd=cwd, env=env)
            if report["client_update"]["status"] == "would-update":
                report["warnings"] = [
                    "Repo previews below use the installed client. Apply to upgrade first, "
                    "then preview and verify with the new client."
                ]
            if args.refresh_skills:
                argv = [str(lt), "update", "--skills-only", "--dry-run"]
                preview, _ = _json_command(argv, cwd=cwd, env=env)
                skill_changes = bool(preview.get("diffs") or preview.get("created"))
                report["skills"] = {
                    "preview_argv": argv,
                    "preview": _update_summary(preview, preview=True),
                    "status": "would-update" if skill_changes else "current",
                }
                if args.yes and skill_changes:
                    applied, _ = _json_command(argv[:-1], cwd=cwd, env=env)
                    report["skills"]["applied"] = _update_summary(applied)
                    verified, _ = _json_command(argv, cwd=cwd, env=env)
                    report["skills"]["status"] = (
                        "verification_failed"
                        if verified.get("diffs") or verified.get("created")
                        else "updated"
                    )
            for root in roots:
                entry: dict[str, Any] = {"root": str(root)}
                report["repos"].append(entry)
                try:
                    if not root.is_dir():
                        raise MaintenanceError(
                            "Repo directory is missing; it was not created or pruned."
                        )
                    doctor_argv = [str(lt), "doctor", "--target", str(root)]
                    before, exit_code = _json_command(doctor_argv, cwd=cwd, env=env, doctor=True)
                    entry["before"] = _doctor_summary(before, exit_code)
                    update_argv = [str(lt), "update", "--target", str(root), "--yes"]
                    preview, _ = _json_command(update_argv + ["--dry-run"], cwd=cwd, env=env)
                    entry["preview_argv"] = update_argv + ["--dry-run"]
                    entry["preview"] = _update_summary(preview, preview=True)
                    changes = bool(preview.get("diffs") or preview.get("created"))
                    entry["status"] = "would-update" if changes else "current"
                    if args.yes and changes:
                        applied, _ = _json_command(update_argv, cwd=cwd, env=env)
                        entry["applied"] = _update_summary(applied)
                        after, exit_code = _json_command(doctor_argv, cwd=cwd, env=env, doctor=True)
                        entry["after"] = _doctor_summary(after, exit_code)
                        entry["status"] = (
                            "updated" if _doctor_clean(after, exit_code) else "verification_failed"
                        )
                    elif not _doctor_clean(before, exit_code):
                        entry["status"] = "check_failed" if not changes else entry["status"]
                    entry["binding"] = _binding(root)
                except MaintenanceError as exc:
                    entry.update(status="error", error=str(exc))
                except OSError as exc:
                    entry.update(
                        status="error", error=f"Filesystem check failed: {type(exc).__name__}."
                    )
    except MaintenanceError as exc:
        if report["client_update"]["status"] == "checking":
            report["client_update"]["status"] = "error"
        report["errors"].append(str(exc))
    except OSError as exc:
        report["errors"].append(f"Filesystem check failed: {type(exc).__name__}.")
    # Missing bindings are actionable even though plain doctor treats absent
    # blocks as a valid opt-out. Automation must not mistake that for enrollment.
    report["ok"] = (
        not report["errors"]
        and report["client_update"]["status"] != "would-update"
        and report.get("skills", {}).get("status") not in {"would-update", "verification_failed"}
        and all(
            repo.get("status") in {"current", "updated"} and repo.get("binding") == "bound"
            for repo in report["repos"]
        )
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_parser_options(parser)
    report = maintain(parser.parse_args(argv))
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
