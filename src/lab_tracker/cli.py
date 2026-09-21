"""Command-line utilities for Lab Tracker packaging and consumer setup."""

from __future__ import annotations

import argparse
import difflib
import importlib.util
import ipaddress
import json
import math
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable
from contextlib import suppress
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
    COMMIT_CAPTURE_RECOVERY_POLICY,
    code_conventions_version_line,
    code_facing_idioms,
    cursor_rules_mdc,
    managed_agent_activation_block,
    managed_code_conventions_block,
    package_version,
)
from lab_tracker.demo_seed import DemoSeedResult, seed_demo_data
from lab_tracker.instance_url import DEFAULT_BASE_URL, normalize_instance_base_url
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository


@dataclass
class InitResult:
    created: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    overwritten: list[Path] = field(default_factory=list)
    stripped: list[Path] = field(default_factory=list)
    up_to_date: list[Path] = field(default_factory=list)
    backups: dict[Path, Path] = field(default_factory=dict)
    diffs: dict[Path, str] = field(default_factory=dict)
    offers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    _preview_contents: dict[Path, str] = field(default_factory=dict, repr=False)

    def as_dict(self) -> dict[str, object]:
        return {
            "created": [str(path) for path in self.created],
            "skipped": [str(path) for path in self.skipped],
            "overwritten": [str(path) for path in self.overwritten],
            "stripped": [str(path) for path in self.stripped],
            "up_to_date": [str(path) for path in self.up_to_date],
            "backups": {str(path): str(backup) for path, backup in self.backups.items()},
            "diffs": {str(path): diff for path, diff in self.diffs.items()},
            "offers": list(self.offers),
            "warnings": list(self.warnings),
        }


def init_consumer_repo(
    target: str | Path = ".",
    *,
    project_name: str | None = None,
    mcp_base_url: str | None = None,
    force: bool = False,
    yes: bool = False,
    dry_run: bool = False,
    uninstall: bool = False,
    install_skills: bool = False,
) -> InitResult:
    root = Path(target).expanduser().resolve()
    if not dry_run:
        root.mkdir(parents=True, exist_ok=True)
    result = InitResult()
    if uninstall:
        if install_skills:
            _uninstall_setup_skill(result=result, dry_run=dry_run)
        for activation_target in _ACTIVATION_BLOCK_TARGETS:
            _strip_managed_block(
                root / activation_target,
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
    resolved_mcp_base_url = normalize_instance_base_url(
        mcp_base_url or DEFAULT_BASE_URL
    )
    files = {
        root / ".mcp.json": _mcp_json(resolved_mcp_base_url),
        root / ".cursor" / "mcp.json": _cursor_mcp_json(resolved_mcp_base_url),
        root / ".gemini" / "settings.json": _gemini_settings_json(resolved_mcp_base_url),
        root / ".claude" / "settings.json": _claude_settings_json(),
        root / "scripts" / "lt.py": _lt_shim(),
        root / "AGENTS.lt.md": _agents_fragment(),
        root / "lt_ids.json": _ids_placeholder(project_name),
    }
    for path, content in files.items():
        _write_scaffold_file(path, content, force=force, result=result, dry_run=dry_run)
    for activation_target in _ACTIVATION_BLOCK_TARGETS:
        _write_managed_activation_block(root / activation_target, result=result, dry_run=dry_run)
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
            managed_code_conventions_block(),
            begin_marker=CODE_CONVENTIONS_BLOCK_BEGIN,
            end_marker=CODE_CONVENTIONS_BLOCK_END,
            result=result,
            dry_run=dry_run,
            document_transform=_normalize_cursor_rules_document,
        )
    else:
        result.offers.append(
            "Managed code-facing convention blocks are available with "
            "`lab_tracker init --yes`."
        )
    result.offers.append(
        "Bind a project id into lt_ids.json with `lt project bind` "
        "(--dry-run previews the write)."
    )
    if install_skills:
        _install_setup_skill(result=result, dry_run=dry_run)
    else:
        result.offers.append(
            "The lab-tracker-setup skill can be installed for Claude/Codex "
            "agents with `--install-skills`."
        )
    if not dry_run:
        _record_enrolled_repo(root, "init")
    result.warnings.extend(_hook_environment_warnings())
    return result


def _print_init_warnings(result: InitResult) -> None:
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)


def _hook_environment_warnings() -> list[str]:
    """Warn when the interpreter/PATH that will run generated hooks can't resolve them.

    init scaffolds agent hooks that call bare ``lt`` and a ``scripts/lt.py`` shim
    that imports ``lab_tracker_client``. When the client lives in a different venv
    than the ambient interpreter, that config is dead-on-arrival and fails
    silently, so surface it explicitly at init time (GH #76).
    """

    warnings: list[str] = []
    if shutil.which("lt") is None:
        warnings.append(
            "Generated agent hooks call `lt`, but no `lt` executable is on the "
            "current PATH. SessionStart/UserPromptSubmit hooks (`lt setup status`, "
            "`lt prime`) will silently no-op until the Lab Tracker client is "
            "installed and its bin directory is on the PATH your agent uses "
            "(e.g. activate the venv that provides `lt`)."
        )
    if importlib.util.find_spec("lab_tracker_client") is None:
        warnings.append(
            "scripts/lt.py imports lab_tracker_client, but it is not importable "
            f"under {sys.executable}. Install the client into the interpreter that "
            "runs the shim (`pip install lab-tracker-client` or the repo), or invoke "
            "it with that interpreter."
        )
    return warnings


def _record_enrolled_repo(root: Path, action: str) -> None:
    """Fail-soft: registry metadata must never break init/update."""

    with suppress(Exception):
        from lab_tracker_client.registry import record_repo

        record_repo(root, action)


def _skills_homes() -> tuple[tuple[str, Path], ...]:
    """Return the skill homes targeted by ``--install-skills``.

    The explicit environment override predates Codex support and remains a
    single-target escape hatch for custom installs and isolated tests. Without
    it, install the generated setup skill for both supported agent homes.
    """

    override = os.getenv("LAB_TRACKER_SKILLS_HOME")
    if override:
        return (("custom", Path(override).expanduser()),)
    home = Path.home()
    return (
        ("claude", home / ".claude" / "skills"),
        ("codex", home / ".agents" / "skills"),
    )


def _skills_home() -> Path:
    """Return the legacy primary skill home (Claude, or the override)."""

    return _skills_homes()[0][1]


def _setup_skill_targets() -> tuple[tuple[str, Path], ...]:
    return tuple(
        (name, home / "lab-tracker-setup" / "SKILL.md")
        for name, home in _skills_homes()
    )


def _setup_skill_path() -> Path:
    """Return the legacy primary setup-skill path."""

    return _setup_skill_targets()[0][1]


def _install_setup_skill(*, result: InitResult, dry_run: bool = False) -> None:
    """Render the packaged setup skill into each configured agent skill home.

    A real file copy (no symlinks — Windows), LF-only because the trailing
    sha line pins the exact bytes, fully generated from package text so
    upgrades refresh it via the same call. A version-line-only difference is
    rewritten without a backup (a package bump with unchanged text must not
    churn — or clobber — the single ``.bak-lt-update`` slot); a genuinely
    customised copy is backed up like any other refreshed scaffold file.
    """

    from lab_tracker.setup_guide import (
        setup_skill_markdown,
        skill_content_without_version_line,
    )

    content = setup_skill_markdown()
    for _name, path in _setup_skill_targets():
        _install_setup_skill_at_path(
            path,
            content=content,
            skill_content_without_version_line=skill_content_without_version_line,
            result=result,
            dry_run=dry_run,
        )


def _install_setup_skill_at_path(
    path: Path,
    *,
    content: str,
    skill_content_without_version_line: Callable[[str], str],
    result: InitResult,
    dry_run: bool,
) -> None:
    if not path.exists():
        if dry_run:
            _record_dry_run_change(path, "", content, result)
            result.created.append(path)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        result.created.append(path)
        return
    existing = path.read_text(encoding="utf-8")
    if existing == content:
        result.up_to_date.append(path)
        return
    version_line_only = skill_content_without_version_line(
        existing
    ) == skill_content_without_version_line(content)
    if not version_line_only:
        backup = path.with_name(path.name + _UPDATE_BACKUP_SUFFIX)
        result.backups[path] = backup
        if not dry_run:
            backup.write_text(existing, encoding="utf-8", newline="\n")
    if dry_run:
        _record_dry_run_change(path, existing, content, result)
        result.overwritten.append(path)
        return
    path.write_text(content, encoding="utf-8", newline="\n")
    result.overwritten.append(path)


def _uninstall_setup_skill(*, result: InitResult, dry_run: bool = False) -> None:
    for _name, path in _setup_skill_targets():
        _uninstall_setup_skill_at_path(path, result=result, dry_run=dry_run)


def _uninstall_setup_skill_at_path(
    path: Path,
    *,
    result: InitResult,
    dry_run: bool,
) -> None:
    backup = path.with_name(path.name + _UPDATE_BACKUP_SUFFIX)
    if not path.exists() and not backup.exists():
        result.skipped.append(path)
        return
    if path.exists():
        result.stripped.append(path)
    if backup.exists():
        result.stripped.append(backup)
    if dry_run:
        if path.exists():
            _record_dry_run_change(path, path.read_text(encoding="utf-8"), "", result)
        return
    for item in (path, backup):
        if item.exists():
            item.unlink()
    with suppress(OSError):
        path.parent.rmdir()


_UPDATE_BACKUP_SUFFIX = ".bak-lt-update"

# One activation block per agent instruction file, kept byte-identical so
# Claude Code (CLAUDE.md), Codex CLI and other AGENTS.md readers, and
# Gemini CLI (GEMINI.md) all receive the same consultation policy.
_ACTIVATION_BLOCK_TARGETS: tuple[str, ...] = ("CLAUDE.md", "AGENTS.md", "GEMINI.md")

_CONVENTIONS_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("CLAUDE.md", CODE_CONVENTIONS_BLOCK_BEGIN, CODE_CONVENTIONS_BLOCK_END),
    ("AGENTS.md", AGENTS_CODE_CONVENTIONS_BLOCK_BEGIN, AGENTS_CODE_CONVENTIONS_BLOCK_END),
    (".cursor/rules/lab-tracker.mdc", CODE_CONVENTIONS_BLOCK_BEGIN, CODE_CONVENTIONS_BLOCK_END),
)


def update_consumer_repo(
    target: str | Path = ".",
    *,
    mcp_base_url: str | None = None,
    yes: bool = False,
    dry_run: bool = False,
    install_skills: bool = False,
) -> InitResult:
    """Refresh a previously initialised consumer repo to the current package.

    - Scaffold files are rewritten to the current canonical text; a customised
      or stale file is first backed up next to itself (``*.bak-lt-update``) so
      nothing is lost. ``lt_ids.json`` is user data and is only created when
      missing, never rewritten.
    - Managed blocks refresh in place wherever they are already present, which
      preserves the original consent decision; missing code-conventions blocks
      are only added with ``yes`` (the same gate as ``init --yes``).
    """

    root = Path(target).expanduser().resolve()
    if not dry_run:
        root.mkdir(parents=True, exist_ok=True)
    result = InitResult()
    resolved_mcp_base_url = normalize_instance_base_url(
        mcp_base_url or DEFAULT_BASE_URL
    )
    files = {
        root / ".mcp.json": _mcp_json(resolved_mcp_base_url),
        root / ".cursor" / "mcp.json": _cursor_mcp_json(resolved_mcp_base_url),
        root / ".gemini" / "settings.json": _gemini_settings_json(
            resolved_mcp_base_url
        ),
        root / ".claude" / "settings.json": _claude_settings_json(),
        root / "scripts" / "lt.py": _lt_shim(),
        root / "AGENTS.lt.md": _agents_fragment(),
    }
    for path, content in files.items():
        _update_scaffold_file(path, content, result=result, dry_run=dry_run)
    ids_path = root / "lt_ids.json"
    if ids_path.exists():
        result.skipped.append(ids_path)
    else:
        _write_scaffold_file(
            ids_path, _ids_placeholder(None), force=False, result=result, dry_run=dry_run
        )

    for activation_target in _ACTIVATION_BLOCK_TARGETS:
        _write_managed_activation_block(root / activation_target, result=result, dry_run=dry_run)

    conventions_offer_needed = False
    for relative, begin_marker, end_marker in _CONVENTIONS_TARGETS:
        path = root / Path(relative)
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        present = (
            _extract_managed_body(content, begin_marker=begin_marker, end_marker=end_marker)
            is not None
        )
        if not present and not yes:
            conventions_offer_needed = True
            continue
        _write_managed_block(
            path,
            managed_code_conventions_block(begin_marker=begin_marker, end_marker=end_marker)
            if relative != ".cursor/rules/lab-tracker.mdc"
            else managed_code_conventions_block(),
            begin_marker=begin_marker,
            end_marker=end_marker,
            result=result,
            dry_run=dry_run,
            document_transform=(
                _normalize_cursor_rules_document
                if relative == ".cursor/rules/lab-tracker.mdc"
                else None
            ),
        )
    if conventions_offer_needed:
        result.offers.append(
            "Managed code-facing convention blocks are available with "
            "`lab_tracker update --yes`."
        )
    if install_skills:
        _install_setup_skill(result=result, dry_run=dry_run)
    if not dry_run:
        _record_enrolled_repo(root, "update")
    result.warnings.extend(_hook_environment_warnings())
    return result


def _update_scaffold_file(
    path: Path,
    content: str,
    *,
    result: InitResult,
    dry_run: bool = False,
) -> None:
    if not path.exists():
        _write_scaffold_file(path, content, force=False, result=result, dry_run=dry_run)
        return
    existing = path.read_text(encoding="utf-8")
    if existing == content:
        result.up_to_date.append(path)
        return
    backup = path.with_name(path.name + _UPDATE_BACKUP_SUFFIX)
    result.backups[path] = backup
    if dry_run:
        _record_dry_run_change(path, existing, content, result)
        result.overwritten.append(path)
        return
    backup.write_text(existing, encoding="utf-8")
    path.write_text(content, encoding="utf-8")
    result.overwritten.append(path)


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
        help=(
            "Scaffold Lab Tracker integration files into a consumer repo: MCP "
            "config and instruction blocks for Claude Code, Codex CLI, Gemini "
            "CLI, and Cursor (agent choice stays with the user), so connected "
            "agents consult the graph and route writes through the human-gated "
            "proposal workflow."
        ),
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
    init_parser.add_argument(
        "--install-skills",
        action="store_true",
        help=(
            "Also render the lab-tracker-setup skill into the Claude and Codex "
            "skill homes (with --uninstall: remove it)."
        ),
    )
    update_parser = subcommands.add_parser(
        "update",
        help=(
            "Refresh scaffolded integration files and managed blocks in a "
            "consumer repo to the current package text."
        ),
    )
    update_parser.add_argument(
        "--target",
        default=".",
        help="Consumer repo path to update. Defaults to the current directory.",
    )
    update_parser.add_argument(
        "--yes",
        action="store_true",
        help="Also install managed code-conventions blocks that are not present yet.",
    )
    update_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing files.",
    )
    update_parser.add_argument(
        "--install-skills",
        action="store_true",
        help="Also refresh the lab-tracker-setup skill in the Claude and Codex homes.",
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
        from lab_tracker_client.setup import resolved_base_url_for_setup

        mcp_base_url, _ = resolved_base_url_for_setup()
        result = init_consumer_repo(
            args.target,
            project_name=args.project_name,
            mcp_base_url=mcp_base_url,
            force=args.force,
            yes=args.yes,
            dry_run=args.dry_run,
            uninstall=args.uninstall,
            install_skills=args.install_skills,
        )
        print(json.dumps(result.as_dict(), indent=2))
        _print_init_warnings(result)
    elif args.command == "update":
        from lab_tracker_client.setup import resolved_base_url_for_setup

        mcp_base_url, _ = resolved_base_url_for_setup()
        result = update_consumer_repo(
            args.target,
            mcp_base_url=mcp_base_url,
            yes=args.yes,
            dry_run=args.dry_run,
            install_skills=args.install_skills,
        )
        print(json.dumps(result.as_dict(), indent=2))
        _print_init_warnings(result)
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


def _write_managed_activation_block(
    path: Path,
    *,
    result: InitResult,
    dry_run: bool = False,
) -> None:
    _write_managed_block(
        path,
        managed_agent_activation_block(),
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
    document_transform: Callable[[str], str] | None = None,
) -> None:
    if path.exists() or path in result._preview_contents:
        existing = _planned_or_disk_text(path, result)
        content = _upsert_managed_block(
            existing,
            block,
            begin_marker=begin_marker,
            end_marker=end_marker,
        )
        if document_transform is not None:
            content = document_transform(content)
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
    content = document_transform(block) if document_transform is not None else block
    if dry_run:
        _record_dry_run_change(path, "", content, result)
        result.created.append(path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    result.created.append(path)


def _normalize_cursor_rules_document(content: str) -> str:
    """Keep one canonical file-level header around the managed Cursor rule."""

    front_matter, separator, _managed_block = cursor_rules_mdc().partition("\n\n")
    if not separator:  # pragma: no cover - canonical template always has a body
        return content

    remaining = content
    if remaining.startswith("---\n"):
        closing = remaining.find("\n---\n", len("---\n"))
        if closing != -1:
            remaining = remaining[closing + len("\n---\n") :]

    remaining = remaining.lstrip("\n")
    while remaining.startswith(front_matter):
        boundary = len(front_matter)
        if len(remaining) > boundary and remaining[boundary] != "\n":
            break
        remaining = remaining[boundary:].lstrip("\n")

    return f"{front_matter}\n\n{remaining}"


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
        # Drift is a CONTENT verdict: a package bump that leaves the idiom
        # text unchanged must not cry wolf (a noisy doctor trains users to
        # ignore it). The version line stays reported for information.
        drifted = present and not body_in_sync
        targets.append(
            {
                "name": name,
                "path": str(path),
                "present": present,
                "in_sync": present and body_in_sync,
                "body_in_sync": body_in_sync,
                "version_in_sync": version_in_sync,
                "drifted": drifted,
                "version_line": found_version,
            }
        )
    payload: dict[str, object] = {
        "command": "doctor",
        "package_version": package_version(),
        "expected_version_line": version_line,
        "code_facing_idioms": body,
        "targets": targets,
    }
    if any(target["drifted"] for target in targets):
        payload["suggestion"] = (
            "Managed blocks differ from the installed package text; "
            "`lt update` refreshes them (`--dry-run` previews the changes)."
        )
    return payload


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


def _mcp_json(base_url: str = DEFAULT_BASE_URL) -> str:
    base_url = normalize_instance_base_url(base_url)
    payload = {
        "mcpServers": {
            "lab-tracker": {
                "command": "lt-mcp",
                "env": {
                    "LAB_TRACKER_BASE_URL": base_url,
                },
            }
        }
    }
    return json.dumps(payload, indent=2) + "\n"


def _cursor_mcp_json(base_url: str = DEFAULT_BASE_URL) -> str:
    # Cursor reads project MCP config from .cursor/mcp.json using the same
    # top-level `mcpServers` shape as .mcp.json (it does not auto-read root
    # .mcp.json, and it does not use Copilot's `servers` schema). Keep the two
    # byte-identical so Cursor and Claude/Codex stay in lockstep.
    return _mcp_json(base_url)


def _gemini_settings_json(base_url: str = DEFAULT_BASE_URL) -> str:
    # Gemini CLI reads project settings from .gemini/settings.json and accepts
    # the same top-level `mcpServers` shape (it does not auto-read root
    # .mcp.json). Only the MCP entry is written; Gemini CLI merges project
    # settings over user-level ones. Kept byte-identical to .mcp.json so every
    # vendor launches the same portable `lt-mcp` command.
    return _mcp_json(base_url)


def _claude_settings_json() -> str:
    payload = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            # One line when healthy, short advisory when setup
                            # is missing or drifted; --fail-silent guarantees
                            # the hook can never block a session.
                            "command": "lt setup status --brief --fail-silent",
                        }
                    ],
                }
            ],
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
            ],
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
        f"""\
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

        Guided setup lives on the `lt` CLI: `lt setup status` is a read-only
        inventory of server reachability and what is configured in this repo.
        Setup write commands take `--dry-run` previews (`lt setup init`,
        `lt watch add`), and `lt setup connect`, `lt project bind`, and
        `lt hooks install` also require `--yes`; suggest them to the user
        rather than applying them unprompted.

        The proposal workflow is human-gated: evidence staged from this repo
        (notes, figures, watch folders, commit hooks) can be swept into
        AI-drafted graph change proposals — the daily review — that a person
        accepts, edits, or rejects in the Lab Tracker app. Agents may stage
        evidence and, when asked, trigger or request drafts; they never accept
        or commit them. Drafting runs server-side on the provider the operator
        configured (`LAB_TRACKER_GRAPH_DRAFT_PROVIDER`: OpenAI, Anthropic, or
        Google — the lab's choice). See `docs/agent-setup.md` in the Lab
        Tracker repository, or the `lab-tracker://setup-guide` MCP resource.

        {COMMIT_CAPTURE_RECOVERY_POLICY}

        MCP config is scaffolded per agent and works with any MCP-capable
        coding agent: `.mcp.json` (Claude Code and other root-config readers),
        `.cursor/mcp.json` (Cursor), and `.gemini/settings.json` (Gemini CLI)
        all launch the portable `lt-mcp` command, so consumer repos should not
        hard-code local Lab Tracker virtualenv paths. Codex CLI reads this
        repo's `AGENTS.md` and registers MCP servers in `~/.codex/config.toml`
        — add `[mcp_servers.lab-tracker]` with `command = "lt-mcp"` (a
        project-scoped `.codex/config.toml` works only in trusted repos, so it
        is not scaffolded here).
        """
    )


def _ids_placeholder(project_name: str | None) -> str:
    payload = {
        "project_id": "",
        "project_name": project_name or "",
    }
    return json.dumps(payload, indent=2) + "\n"
