"""Command-line utilities for Lab Tracker packaging and consumer setup."""

from __future__ import annotations

import argparse
import difflib
import ipaddress
import json
import math
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from textwrap import dedent

from alembic import command
from alembic.config import Config

from lab_tracker.api import LabTrackerAPI
from lab_tracker.backup import BackupError, create_sqlite_backup, restore_sqlite_backup
from lab_tracker.config import get_settings
from lab_tracker.db import get_engine, get_session_factory
from lab_tracker.decision_context_constants import (
    AGENTS_CODE_CONVENTIONS_BLOCK_BEGIN,
    AGENTS_CODE_CONVENTIONS_BLOCK_END,
    CLAUDE_BLOCK_BEGIN,
    CLAUDE_BLOCK_END,
    CODE_CONVENTIONS_BLOCK_BEGIN,
    CODE_CONVENTIONS_BLOCK_END,
    code_conventions_version_line,
    code_facing_idioms,
    cursor_rules_mdc,
    managed_claude_block,
    managed_code_conventions_block,
    package_version,
)
from lab_tracker.demo_seed import DemoSeedResult, seed_demo_data
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository


@dataclass
class InitResult:
    created: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    overwritten: list[Path] = field(default_factory=list)
    stripped: list[Path] = field(default_factory=list)
    diffs: dict[Path, str] = field(default_factory=dict)
    offers: list[str] = field(default_factory=list)
    _preview_contents: dict[Path, str] = field(default_factory=dict, repr=False)

    def as_dict(self) -> dict[str, object]:
        return {
            "created": [str(path) for path in self.created],
            "skipped": [str(path) for path in self.skipped],
            "overwritten": [str(path) for path in self.overwritten],
            "stripped": [str(path) for path in self.stripped],
            "diffs": {str(path): diff for path, diff in self.diffs.items()},
            "offers": list(self.offers),
        }


def init_consumer_repo(
    target: str | Path = ".",
    *,
    project_name: str | None = None,
    force: bool = False,
    yes: bool = False,
    dry_run: bool = False,
    uninstall: bool = False,
) -> InitResult:
    root = Path(target).expanduser().resolve()
    if not dry_run:
        root.mkdir(parents=True, exist_ok=True)
    result = InitResult()
    if uninstall:
        _strip_managed_block(
            root / "CLAUDE.md",
            begin_marker=CLAUDE_BLOCK_BEGIN,
            end_marker=CLAUDE_BLOCK_END,
            result=result,
            dry_run=dry_run,
        )
        _strip_managed_block(
            root / "CLAUDE.md",
            begin_marker=CODE_CONVENTIONS_BLOCK_BEGIN,
            end_marker=CODE_CONVENTIONS_BLOCK_END,
            result=result,
            dry_run=dry_run,
        )
        _strip_managed_block(
            root / "AGENTS.md",
            begin_marker=AGENTS_CODE_CONVENTIONS_BLOCK_BEGIN,
            end_marker=AGENTS_CODE_CONVENTIONS_BLOCK_END,
            result=result,
            dry_run=dry_run,
        )
        _strip_managed_block(
            root / ".cursor" / "rules" / "lab-tracker.mdc",
            begin_marker=CODE_CONVENTIONS_BLOCK_BEGIN,
            end_marker=CODE_CONVENTIONS_BLOCK_END,
            result=result,
            dry_run=dry_run,
        )
        return result
    files = {
        root / ".mcp.json": _mcp_json(),
        root / ".cursor" / "mcp.json": _cursor_mcp_json(),
        root / ".claude" / "settings.json": _claude_settings_json(),
        root / "scripts" / "lt.py": _lt_shim(),
        root / "AGENTS.lt.md": _agents_fragment(),
        root / "lt_ids.json": _ids_placeholder(project_name),
    }
    for path, content in files.items():
        _write_scaffold_file(path, content, force=force, result=result, dry_run=dry_run)
    _write_managed_claude_block(root / "CLAUDE.md", result=result, dry_run=dry_run)
    if yes:
        _write_managed_block(
            root / "CLAUDE.md",
            managed_code_conventions_block(),
            begin_marker=CODE_CONVENTIONS_BLOCK_BEGIN,
            end_marker=CODE_CONVENTIONS_BLOCK_END,
            result=result,
            dry_run=dry_run,
        )
        _write_managed_block(
            root / "AGENTS.md",
            managed_code_conventions_block(
                begin_marker=AGENTS_CODE_CONVENTIONS_BLOCK_BEGIN,
                end_marker=AGENTS_CODE_CONVENTIONS_BLOCK_END,
            ),
            begin_marker=AGENTS_CODE_CONVENTIONS_BLOCK_BEGIN,
            end_marker=AGENTS_CODE_CONVENTIONS_BLOCK_END,
            result=result,
            dry_run=dry_run,
        )
        _write_managed_block(
            root / ".cursor" / "rules" / "lab-tracker.mdc",
            cursor_rules_mdc(),
            begin_marker=CODE_CONVENTIONS_BLOCK_BEGIN,
            end_marker=CODE_CONVENTIONS_BLOCK_END,
            result=result,
            dry_run=dry_run,
        )
    else:
        result.offers.append(
            "Managed code-facing convention blocks are available with "
            "`lab_tracker init --yes`."
        )
    return result


def serve_app(
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    reload: bool = False,
    run_migrations: bool = True,
    open_browser: bool = True,
    insecure_allow_lan: bool = False,
    server_runner: Callable[..., None] | None = None,
    browser_opener: Callable[[str], object] | None = None,
) -> None:
    if _is_non_loopback_host(host) and not insecure_allow_lan:
        settings = get_settings()
        if not settings.is_auth_enabled():
            raise SystemExit(
                "Refusing to bind Lab Tracker to a non-loopback interface while "
                "authentication is disabled. Enable auth or pass "
                "--insecure-allow-lan if this is intentional."
            )

    if run_migrations:
        settings = get_settings()
        create_sqlite_backup(
            settings.database_url,
            backup_dir=settings.backup_path,
            keep=settings.backup_keep,
            skip_missing=True,
            skip_unsupported=True,
        )
        alembic_config = _alembic_config()
        command.upgrade(alembic_config, "head")

    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{browser_host}:{port}/app"
    if open_browser:
        opener = browser_opener or webbrowser.open
        _start_browser_when_ready(
            url,
            readiness_url=f"http://{browser_host}:{port}/health",
            opener=opener,
        )

    if server_runner is None:
        import uvicorn

        server_runner = uvicorn.run
    server_runner("lab_tracker.asgi:app", host=host, port=port, reload=reload)


def seed_demo_database(
    *,
    run_migrations: bool = True,
    allow_duplicates: bool = False,
) -> DemoSeedResult:
    if run_migrations:
        alembic_config = _alembic_config()
        command.upgrade(alembic_config, "head")

    settings = get_settings()
    engine = get_engine(settings)
    session_factory = get_session_factory(engine=engine)
    try:
        with session_factory() as session:
            repository = SQLAlchemyLabTrackerRepository(session)
            root_api = LabTrackerAPI(settings=settings)
            with root_api.request_scope(repository, surface="cli") as scope:
                result = seed_demo_data(scope.api, allow_duplicates=allow_duplicates)
                scope.commit()
                return result
    finally:
        engine.dispose()


def _start_browser_when_ready(
    url: str,
    *,
    readiness_url: str,
    opener: Callable[[str], object],
) -> threading.Thread:
    thread = threading.Thread(
        target=_open_browser_when_ready,
        kwargs={
            "opener": opener,
            "readiness_url": readiness_url,
            "url": url,
        },
        daemon=True,
    )
    thread.start()
    return thread


def _open_browser_when_ready(
    url: str,
    *,
    readiness_url: str,
    opener: Callable[[str], object],
    readiness_probe: Callable[[str], bool] | None = None,
    timeout_seconds: float = 20.0,
    poll_interval_seconds: float = 0.2,
    max_attempts: int | None = None,
    sleep: Callable[[float], object] = time.sleep,
) -> bool:
    probe = readiness_probe or _http_ready
    attempts = max_attempts
    if attempts is None:
        attempts = max(1, math.ceil(timeout_seconds / poll_interval_seconds))
    for _attempt in range(attempts):
        if probe(readiness_url):
            opener(url)
            return True
        sleep(poll_interval_seconds)
    print(
        f"Lab Tracker is still starting. Open {url} after the server is ready.",
        file=sys.stderr,
    )
    return False


def _http_ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=0.5) as response:
            return 200 <= response.status < 500
    except (OSError, urllib.error.URLError):
        return False


def _is_non_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized in {"localhost", "127.0.0.1", "::1"}:
        return False
    try:
        return not ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return True


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="lab_tracker")
    subcommands = parser.add_subparsers(dest="command", required=True)

    init_parser = subcommands.add_parser(
        "init",
        help="Scaffold Lab Tracker integration files into a consumer repo.",
    )
    init_parser.add_argument(
        "--target",
        default=".",
        help="Consumer repo path to scaffold. Defaults to the current directory.",
    )
    init_parser.add_argument(
        "--project-name",
        default=None,
        help="Optional project name to record in the generated lt_ids.json placeholder.",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing scaffolded files.",
    )
    init_parser.add_argument(
        "--yes",
        action="store_true",
        help="Consent to recommended managed code-conventions blocks.",
    )
    init_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show scaffold and managed-block diffs without writing files.",
    )
    init_parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Strip Lab Tracker managed blocks while preserving surrounding content.",
    )
    serve_parser = subcommands.add_parser(
        "serve",
        help="Run migrations, open the browser, and start the Lab Tracker web app.",
    )
    serve_parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind.")
    serve_parser.add_argument("--port", default=8000, type=int, help="Port to listen on.")
    serve_parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable uvicorn reload for local development.",
    )
    serve_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open the browser after migrations complete.",
    )
    serve_parser.add_argument(
        "--skip-migrations",
        action="store_true",
        help="Start the server without running alembic upgrade head first.",
    )
    serve_parser.add_argument(
        "--insecure-allow-lan",
        action="store_true",
        help=(
            "Allow binding to a non-loopback interface even when authentication "
            "is disabled."
        ),
    )
    seed_parser = subcommands.add_parser(
        "seed-demo",
        help="Populate the configured database with a local-development demo project.",
    )
    seed_parser.add_argument(
        "--skip-migrations",
        action="store_true",
        help="Seed without running alembic upgrade head first.",
    )
    seed_parser.add_argument(
        "--allow-duplicates",
        action="store_true",
        help="Create a fresh demo project even if the default demo already exists.",
    )
    doctor_parser = subcommands.add_parser(
        "doctor",
        aliases=["check-idioms"],
        help="Check managed code-facing idiom blocks for drift.",
    )
    doctor_parser.add_argument(
        "--target",
        default=".",
        help="Consumer repo path to inspect. Defaults to the current directory.",
    )
    backup_parser = subcommands.add_parser(
        "backup",
        help="Create a SQLite backup snapshot with the online backup API.",
    )
    backup_parser.add_argument(
        "--to",
        default=None,
        help="Backup directory. Defaults to LAB_TRACKER_BACKUP_PATH.",
    )
    backup_parser.add_argument(
        "--keep",
        default=None,
        type=int,
        help="Number of newest snapshots to keep. Defaults to LAB_TRACKER_BACKUP_KEEP.",
    )
    backup_parser.add_argument(
        "--database-url",
        default=None,
        help="SQLite database URL. Defaults to LAB_TRACKER_DATABASE_URL.",
    )
    restore_parser = subcommands.add_parser(
        "restore",
        help="Restore a SQLite backup snapshot into the configured database.",
    )
    restore_parser.add_argument("backup_path", help="Path to a backup snapshot.")
    restore_parser.add_argument(
        "--database-url",
        default=None,
        help="SQLite database URL to restore into. Defaults to LAB_TRACKER_DATABASE_URL.",
    )
    restore_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the target database after you have stopped Lab Tracker.",
    )

    args = parser.parse_args(argv)
    if args.command == "init":
        result = init_consumer_repo(
            args.target,
            project_name=args.project_name,
            force=args.force,
            yes=args.yes,
            dry_run=args.dry_run,
            uninstall=args.uninstall,
        )
        print(json.dumps(result.as_dict(), indent=2))
    elif args.command == "serve":
        try:
            serve_app(
                host=args.host,
                port=args.port,
                reload=args.reload,
                run_migrations=not args.skip_migrations,
                open_browser=not args.no_browser,
                insecure_allow_lan=args.insecure_allow_lan,
            )
        except Exception as exc:
            print(f"Failed to start Lab Tracker: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
    elif args.command == "seed-demo":
        try:
            result = seed_demo_database(
                run_migrations=not args.skip_migrations,
                allow_duplicates=args.allow_duplicates,
            )
        except Exception as exc:
            print(f"Failed to seed Lab Tracker demo data: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        print(json.dumps(result.as_dict(), indent=2))
    elif args.command in {"doctor", "check-idioms"}:
        payload = _doctor(args.target)
        print(json.dumps(payload, indent=2))
        if _doctor_exit_code(payload):
            raise SystemExit(1)
    elif args.command == "backup":
        settings = get_settings()
        try:
            result = create_sqlite_backup(
                args.database_url or settings.database_url,
                backup_dir=args.to or settings.backup_path,
                keep=args.keep if args.keep is not None else settings.backup_keep,
            )
        except BackupError as exc:
            print(f"Backup failed: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        print(json.dumps(result.as_dict(), indent=2))
    elif args.command == "restore":
        settings = get_settings()
        try:
            result = restore_sqlite_backup(
                args.backup_path,
                args.database_url or settings.database_url,
                force=args.force,
            )
        except BackupError as exc:
            print(f"Restore failed: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        print(json.dumps(result.as_dict(), indent=2))


def _alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(resources.files("lab_tracker") / "alembic"))
    return config


def _write_scaffold_file(
    path: Path,
    content: str,
    *,
    force: bool,
    result: InitResult,
    dry_run: bool = False,
) -> None:
    if path.exists() and not force:
        result.skipped.append(path)
        return
    existing = _planned_or_disk_text(path, result)
    if dry_run:
        _record_dry_run_change(path, existing, content, result)
        if path.exists():
            result.overwritten.append(path)
        else:
            result.created.append(path)
        return
    existed = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if existed:
        result.overwritten.append(path)
    else:
        result.created.append(path)


def _write_managed_claude_block(
    path: Path,
    *,
    result: InitResult,
    dry_run: bool = False,
) -> None:
    _write_managed_block(
        path,
        managed_claude_block(),
        begin_marker=CLAUDE_BLOCK_BEGIN,
        end_marker=CLAUDE_BLOCK_END,
        result=result,
        dry_run=dry_run,
    )


def _write_managed_block(
    path: Path,
    block: str,
    *,
    begin_marker: str,
    end_marker: str,
    result: InitResult,
    dry_run: bool = False,
) -> None:
    if path.exists() or path in result._preview_contents:
        existing = _planned_or_disk_text(path, result)
        content = _upsert_managed_block(
            existing,
            block,
            begin_marker=begin_marker,
            end_marker=end_marker,
        )
        if content == existing:
            result.skipped.append(path)
            return
        if dry_run:
            _record_dry_run_change(path, existing, content, result)
            result.overwritten.append(path)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        result.overwritten.append(path)
        return
    if dry_run:
        _record_dry_run_change(path, "", block, result)
        result.created.append(path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(block, encoding="utf-8")
    result.created.append(path)


def _upsert_managed_block(
    existing: str,
    block: str,
    *,
    begin_marker: str = CLAUDE_BLOCK_BEGIN,
    end_marker: str = CLAUDE_BLOCK_END,
) -> str:
    prefix, suffix, removed = _managed_block_parts(existing, begin_marker, end_marker)
    if removed:
        return _join_surrounding_block(prefix, block, suffix)
    if existing.strip():
        return f"{existing.rstrip()}\n\n{block}"
    return block


def _strip_managed_block(
    path: Path,
    *,
    begin_marker: str,
    end_marker: str,
    result: InitResult,
    dry_run: bool = False,
) -> None:
    if not path.exists() and path not in result._preview_contents:
        result.skipped.append(path)
        return
    existing = _planned_or_disk_text(path, result)
    prefix, suffix, removed = _managed_block_parts(existing, begin_marker, end_marker)
    if not removed:
        result.skipped.append(path)
        return
    content = _join_surrounding_text(prefix, suffix)
    if path not in result.stripped:
        result.stripped.append(path)
    if dry_run:
        _record_dry_run_change(path, existing, content, result)
        return
    path.write_text(content, encoding="utf-8")
    result.overwritten.append(path)


def _managed_block_parts(
    existing: str,
    begin_marker: str,
    end_marker: str,
) -> tuple[str, str, bool]:
    has_begin = begin_marker in existing
    has_end = end_marker in existing
    if has_begin and has_end:
        prefix, rest = existing.split(begin_marker, 1)
        _old_block, suffix = rest.split(end_marker, 1)
        return prefix, _drop_trailing_version_line(suffix), True
    if has_begin:
        prefix, _rest = existing.split(begin_marker, 1)
        return prefix, "", True
    if has_end:
        _old_block, suffix = existing.split(end_marker, 1)
        return "", _drop_trailing_version_line(suffix), True
    return existing, "", False


def _join_surrounding_block(prefix: str, block: str, suffix: str) -> str:
    if prefix.strip():
        return f"{prefix.rstrip()}\n\n{block}{suffix.lstrip()}"
    return f"{block}{suffix.lstrip()}"


def _join_surrounding_text(prefix: str, suffix: str) -> str:
    if prefix.strip() and suffix.strip():
        return f"{prefix.rstrip()}\n\n{suffix.lstrip()}"
    if prefix.strip():
        return prefix.rstrip() + "\n"
    return suffix.lstrip()


def _drop_trailing_version_line(suffix: str) -> str:
    stripped = suffix.lstrip()
    if stripped.startswith("<!-- lab-tracker-code-conventions"):
        _version_line, separator, rest = stripped.partition("\n")
        return rest if separator else ""
    return suffix


def _planned_or_disk_text(path: Path, result: InitResult) -> str:
    if path in result._preview_contents:
        return result._preview_contents[path]
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def _record_dry_run_change(
    path: Path,
    existing: str,
    content: str,
    result: InitResult,
) -> None:
    result._preview_contents[path] = content
    result.diffs[path] = _text_diff(path, existing, content)


def _text_diff(path: Path, existing: str, content: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            existing.splitlines(),
            content.splitlines(),
            fromfile=f"{path} (current)",
            tofile=f"{path} (proposed)",
            lineterm="",
        )
    )


def _extract_managed_body(
    content: str,
    *,
    begin_marker: str,
    end_marker: str,
) -> str | None:
    if begin_marker not in content or end_marker not in content:
        return None
    _prefix, rest = content.split(begin_marker, 1)
    body, _suffix = rest.split(end_marker, 1)
    return body.strip() + "\n"


def _extract_version_line(
    content: str,
    *,
    end_marker: str,
) -> str:
    if end_marker not in content:
        return ""
    _prefix, suffix = content.split(end_marker, 1)
    stripped = suffix.lstrip()
    if not stripped.startswith("<!-- lab-tracker-code-conventions"):
        return ""
    return stripped.splitlines()[0]


def _doctor(target: str | Path = ".") -> dict[str, object]:
    root = Path(target).expanduser().resolve()
    body = code_facing_idioms()
    version_line = code_conventions_version_line(body)
    checks = [
        (
            "CLAUDE.md",
            root / "CLAUDE.md",
            CODE_CONVENTIONS_BLOCK_BEGIN,
            CODE_CONVENTIONS_BLOCK_END,
        ),
        (
            "AGENTS.md",
            root / "AGENTS.md",
            AGENTS_CODE_CONVENTIONS_BLOCK_BEGIN,
            AGENTS_CODE_CONVENTIONS_BLOCK_END,
        ),
        (
            ".cursor/rules/lab-tracker.mdc",
            root / ".cursor" / "rules" / "lab-tracker.mdc",
            CODE_CONVENTIONS_BLOCK_BEGIN,
            CODE_CONVENTIONS_BLOCK_END,
        ),
    ]
    targets: list[dict[str, object]] = []
    for name, path, begin_marker, end_marker in checks:
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        found_body = _extract_managed_body(
            content,
            begin_marker=begin_marker,
            end_marker=end_marker,
        )
        found_version = _extract_version_line(content, end_marker=end_marker)
        present = found_body is not None
        body_in_sync = present and found_body == body
        version_in_sync = present and found_version == version_line
        drifted = present and not (body_in_sync and version_in_sync)
        targets.append(
            {
                "name": name,
                "path": str(path),
                "present": present,
                "in_sync": present and body_in_sync and version_in_sync,
                "body_in_sync": body_in_sync,
                "version_in_sync": version_in_sync,
                "drifted": drifted,
                "version_line": found_version,
            }
        )
    return {
        "command": "doctor",
        "package_version": package_version(),
        "expected_version_line": version_line,
        "code_facing_idioms": body,
        "targets": targets,
    }


def _doctor_exit_code(payload: object) -> int:
    if not isinstance(payload, dict) or payload.get("command") != "doctor":
        return 0
    targets = payload.get("targets")
    if not isinstance(targets, list):
        return 1
    all_targets_clean = all(
        isinstance(target, dict) and not target.get("drifted") for target in targets
    )
    return 0 if all_targets_clean else 1


def _mcp_json() -> str:
    payload = {
        "mcpServers": {
            "lab-tracker": {
                "command": "lt-mcp",
                "env": {
                    "LAB_TRACKER_MCP_BASE_URL": "http://127.0.0.1:8000",
                },
            }
        }
    }
    return json.dumps(payload, indent=2) + "\n"


def _cursor_mcp_json() -> str:
    # Cursor reads project MCP config from .cursor/mcp.json using the same
    # top-level `mcpServers` shape as .mcp.json (it does not auto-read root
    # .mcp.json, and it does not use Copilot's `servers` schema). Keep the two
    # byte-identical so Cursor and Claude/Codex stay in lockstep.
    return _mcp_json()


def _claude_settings_json() -> str:
    payload = {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                "lt prime --if-research-facing --fail-silent --limit 5"
                            ),
                        }
                    ],
                }
            ]
        }
    }
    return json.dumps(payload, indent=2) + "\n"


def _lt_shim() -> str:
    return dedent(
        '''\
        """Thin Lab Tracker consumer shim generated by `lab_tracker init`."""

        from lab_tracker_client import *  # noqa: F403
        from lab_tracker_client.cli import main


        if __name__ == "__main__":
            main()
        '''
    )


def _agents_fragment() -> str:
    return dedent(
        """\
        # Lab Tracker Integration

        Use Lab Tracker for project/question/note context and keep concrete execution
        tasks in this repo's issue tracker.

        - Use MCP tools for lightweight reads and small interactive writes during an
          agent session.
        - Use `scripts.lt.upsert_note(...)` from committed scripts for substantive
          findings notes that should stay synced with code, plots, and tables.
        - Use `python -m scripts.lt quick "..." --project <PROJECT_ID>` for scratch
          observations that do not need their own committed script.

        Managed code-facing conventions live in package text and can be rendered
        into CLAUDE.md, AGENTS.md, and .cursor/rules/lab-tracker.mdc during
        consenting init runs.

        Notes are idempotent by the first non-blank line of the note body when the
        existing body is identical. Treat that first line as a stable marker; change
        it intentionally when you mean to create a new Lab Tracker note.

        Configure the MCP server with the generated `.mcp.json`. It uses the portable
        `lt-mcp` command, so consumer repos should not hard-code local Lab Tracker
        virtualenv paths.
        """
    )


def _ids_placeholder(project_name: str | None) -> str:
    payload = {
        "project_id": "",
        "project_name": project_name or "",
    }
    return json.dumps(payload, indent=2) + "\n"
