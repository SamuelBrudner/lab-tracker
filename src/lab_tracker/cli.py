"""Command-line utilities for Lab Tracker packaging and consumer setup."""

from __future__ import annotations

import argparse
import difflib
import errno
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

from lab_tracker._version import DISTRIBUTION_NAME, __version__
from lab_tracker.api import LabTrackerAPI
from lab_tracker.backup import BackupError, create_sqlite_backup, restore_sqlite_backup
from lab_tracker.capture_channels.cli import add_integrations_parsers, run_integrations_command
from lab_tracker.config import get_settings
from lab_tracker.db import get_engine, get_session_factory
from lab_tracker.decision_context_constants import (
    AGENT_SESSION_CAPTURE_DISCOVERY_POLICY,
    AGENTS_CODE_CONVENTIONS_BLOCK_BEGIN,
    AGENTS_CODE_CONVENTIONS_BLOCK_END,
    CLAUDE_BLOCK_BEGIN,
    CLAUDE_BLOCK_END,
    CLIENT_MAINTENANCE_POLICY,
    CODE_CONVENTIONS_BLOCK_BEGIN,
    CODE_CONVENTIONS_BLOCK_END,
    COMMIT_CAPTURE_RECOVERY_POLICY,
    EMPTY_PROJECT_ORIENTATION_POLICY,
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
    """What an init, update, or uninstall run changed, or would change.

    Each repo path lands in one of ``created``, ``skipped``, ``overwritten``
    and ``up_to_date``. Uninstall also lists each file it strips a managed
    block from, or deletes, under ``stripped``. ``diffs`` holds, in a dry
    run, how every planned file would change and, after applying, how every
    file that already existed changed (a created file is the canonical text).
    """

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
    _preview_original_contents: dict[Path, str] = field(default_factory=dict, repr=False)
    # Files whose managed blocks are planned but not yet written, in order,
    # with the identity that tells two paths to one file (a symlinked
    # CLAUDE.md -> AGENTS.md) apart from two files.
    _planned_files: list[Path] = field(default_factory=list, repr=False)
    _planned_identities: dict[Path, tuple[object, ...]] = field(
        default_factory=dict, repr=False
    )
    # Backups a repair has planned, by backup path, written before the files.
    _pending_backups: dict[Path, str] = field(default_factory=dict, repr=False)

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
    result = InitResult()
    # Planning reads and can refuse (markers out of order); writing comes
    # after it, so a refused run leaves every file as it was.
    if uninstall:
        for block in _activation_blocks(root) + _conventions_blocks(root):
            _plan_strip(block, result=result, dry_run=dry_run)
        if install_skills:
            _uninstall_setup_skill(result=result, dry_run=dry_run)
        result.stripped.extend(
            _commit_planned_files(result, dry_run=dry_run, unchanged=result.skipped)
        )
        return result
    _plan_managed_blocks(
        _activation_blocks(root) + (_conventions_blocks(root) if yes else []),
        result=result,
        dry_run=dry_run,
    )
    if not dry_run:
        root.mkdir(parents=True, exist_ok=True)
    resolved_mcp_base_url = normalize_instance_base_url(
        mcp_base_url or DEFAULT_BASE_URL
    )
    files = {
        root / ".mcp.json": _mcp_json(resolved_mcp_base_url),
        root / ".cursor" / "mcp.json": _cursor_mcp_json(resolved_mcp_base_url),
        root / ".gemini" / "settings.json": _gemini_settings_json(resolved_mcp_base_url),
        root / ".claude" / "settings.json": _claude_settings_json_for(root),
        root / "scripts" / "lt.py": _lt_shim(),
        root / "AGENTS.lt.md": _agents_fragment(),
        root / "lt_ids.json": _ids_placeholder(project_name),
    }
    for path, content in files.items():
        _write_scaffold_file(path, content, force=force, result=result, dry_run=dry_run)
    if not yes:
        result.offers.append(
            "Managed code-facing convention blocks are available with "
            "`lab_tracker init --yes`."
        )
    _commit_planned_files(result, dry_run=dry_run, unchanged=result.skipped)
    result.offers.append(
        "Bind a project id into lt_ids.json with `lt project bind` "
        "(--dry-run previews the write)."
    )
    if install_skills:
        _install_setup_skill(result=result, dry_run=dry_run)
    else:
        result.offers.append(
            "Both Lab Tracker skills and references can be installed for Claude/Codex "
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
    it, install both skill trees for both supported agent homes. A
    relative override is taken as written and so resolves against the current
    directory; it should be an absolute path.
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
    """Install both complete skill trees into each configured agent skill home.

    Real file copies work on Windows. LF-only writes preserve the setup skill's
    generated hash, and upgrades refresh every bundled resource. A setup
    version-line-only difference is
    rewritten without a backup (a package bump with unchanged text must not
    churn — or clobber — the single ``.bak-lt-update`` slot); a genuinely
    customised copy is backed up like any other refreshed scaffold file.
    """

    from lab_tracker.setup_guide import (
        skill_content_without_version_line,
    )
    from lab_tracker.skill_bundle import skill_resources

    for _name, home in _skills_homes():
        for relative_path, content in skill_resources().items():
            _install_setup_skill_at_path(
                home / relative_path,
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
    _record_diff(path, existing, content, result)
    result.overwritten.append(path)


def _uninstall_setup_skill(*, result: InitResult, dry_run: bool = False) -> None:
    from lab_tracker.skill_bundle import skill_resources

    for _name, home in _skills_homes():
        linked_skills: set[str] = set()
        for skill_name in ("lab-tracker", "lab-tracker-setup"):
            root = home / skill_name
            if root.is_symlink():
                # A development install links the folder to repository source.
                # Removing its children would delete files in that checkout.
                linked_skills.add(skill_name)
                result.stripped.append(root)
                if not dry_run:
                    root.unlink()
        for relative_path in skill_resources():
            if relative_path.split("/", 1)[0] not in linked_skills:
                _uninstall_setup_skill_at_path(
                    home / relative_path, result=result, dry_run=dry_run
                )
        if not dry_run:
            for skill_name in ("lab-tracker", "lab-tracker-setup"):
                with suppress(OSError):
                    (home / skill_name).rmdir()


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
    if path.exists():
        _record_diff(path, path.read_text(encoding="utf-8"), "", result)
    for item in (path, backup):
        if item.exists():
            item.unlink()
    with suppress(OSError):
        path.parent.rmdir()


_UPDATE_BACKUP_SUFFIX = ".bak-lt-update"
# The line managed_code_conventions_block writes right after its END marker.
_CONVENTIONS_VERSION_LINE_PREFIX = "<!-- lab-tracker-code-conventions"
# The blocks that write that line; the activation block has none.
_VERSIONED_END_MARKERS = frozenset(
    {CODE_CONVENTIONS_BLOCK_END, AGENTS_CODE_CONVENTIONS_BLOCK_END}
)
# The name the docs give this command, whichever installed script name ran it.
_DOCUMENTED_PROG = "lab-tracker"

# One activation block per agent instruction file, kept byte-identical so
# Claude Code (CLAUDE.md), Codex CLI and other AGENTS.md readers, and
# Gemini CLI (GEMINI.md) all receive the same consultation policy.
_ACTIVATION_BLOCK_TARGETS: tuple[str, ...] = ("CLAUDE.md", "AGENTS.md", "GEMINI.md")

_CURSOR_RULES = ".cursor/rules/lab-tracker.mdc"
_CONVENTIONS_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("CLAUDE.md", CODE_CONVENTIONS_BLOCK_BEGIN, CODE_CONVENTIONS_BLOCK_END),
    ("AGENTS.md", AGENTS_CODE_CONVENTIONS_BLOCK_BEGIN, AGENTS_CODE_CONVENTIONS_BLOCK_END),
    (_CURSOR_RULES, CODE_CONVENTIONS_BLOCK_BEGIN, CODE_CONVENTIONS_BLOCK_END),
)
# Each pass plans every block once. A pass that changes nothing ends planning;
# a lone-marker repair costs a pass, as does putting back a block it dropped.
_MAX_PLANNING_PASSES = 8


@dataclass(frozen=True)
class _ManagedBlock:
    """One managed block and the file it belongs in."""

    path: Path
    text: str
    begin_marker: str
    end_marker: str
    document_transform: Callable[[str], str] | None = None


def _activation_blocks(root: Path) -> list[_ManagedBlock]:
    block = managed_agent_activation_block()
    return [
        _ManagedBlock(root / target, block, CLAUDE_BLOCK_BEGIN, CLAUDE_BLOCK_END)
        for target in _ACTIVATION_BLOCK_TARGETS
    ]


def _conventions_blocks(root: Path) -> list[_ManagedBlock]:
    return [
        _ManagedBlock(
            root / Path(relative),
            managed_code_conventions_block(begin_marker=begin_marker, end_marker=end_marker),
            begin_marker,
            end_marker,
            _normalize_cursor_rules_document if relative == _CURSOR_RULES else None,
        )
        for relative, begin_marker, end_marker in _CONVENTIONS_TARGETS
    ]


def _has_managed_block(block: _ManagedBlock) -> bool:
    """Whether ``block``'s file holds a paired region for it on disk."""

    content = block.path.read_text(encoding="utf-8") if block.path.exists() else ""
    try:
        body = _extract_managed_body(
            content, begin_marker=block.begin_marker, end_marker=block.end_marker
        )
    except ManagedBlockMarkerError as exc:
        raise SystemExit(f"{block.path}: {exc}") from exc
    return body is not None


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
    result = InitResult()
    blocks = _activation_blocks(root)
    conventions_offer_needed = False
    for block in _conventions_blocks(root):
        if yes or _has_managed_block(block):
            blocks.append(block)
        else:
            conventions_offer_needed = True
    # Planning reads and can refuse (markers out of order); writing comes
    # after it, so a refused run leaves every file as it was.
    _plan_managed_blocks(blocks, result=result, dry_run=dry_run)
    if not dry_run:
        root.mkdir(parents=True, exist_ok=True)
    resolved_mcp_base_url = normalize_instance_base_url(
        mcp_base_url or DEFAULT_BASE_URL
    )
    files = {
        root / ".mcp.json": _mcp_json(resolved_mcp_base_url),
        root / ".cursor" / "mcp.json": _cursor_mcp_json(resolved_mcp_base_url),
        root / ".gemini" / "settings.json": _gemini_settings_json(
            resolved_mcp_base_url
        ),
        root / ".claude" / "settings.json": _claude_settings_json_for(root),
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
    _commit_planned_files(result, dry_run=dry_run, unchanged=result.up_to_date)
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


def refresh_setup_skills(*, dry_run: bool = False) -> InitResult:
    """Install or refresh complete research/setup skill trees, machine-wide.

    This is the skills half of ``update_consumer_repo(install_skills=True)`` on
    its own. It takes no target, so it cannot scaffold: it writes no repo file,
    enrolls no repo in the applied-repos registry, and does not resolve the MCP
    URL. It writes only the skill files and their skill directories, so a
    missing skill home is created. A customised skill is backed up to
    ``SKILL.md.bak-lt-update`` before it is refreshed, and ``dry_run`` only
    records the diffs. With the default homes or an absolute
    ``LAB_TRACKER_SKILLS_HOME`` that leaves the current directory and every
    repository alone; a relative override resolves against the current
    directory (see :func:`_skills_homes`), so set it to an absolute path.
    """

    result = InitResult()
    _install_setup_skill(result=result, dry_run=dry_run)
    return result


def reject_skills_only_conflicts(prog: str, *, yes: bool, target: str | None) -> None:
    """Fail loudly when ``update --skills-only`` is mixed with repo-scoped flags.

    ``--skills-only`` never reads or writes a repository, so ``--yes`` (consent
    for a repo's conventions blocks) and an explicit ``--target`` would be
    silently ignored, misleading the caller about what was changed. ``target``
    is ``None`` when the flag was not passed. Exits 1 (not argparse's 2, which
    is reserved for unrecognized arguments), before anything is written.
    """

    given = [
        flag
        for flag, present in (("--yes", yes), ("--target", target is not None))
        if present
    ]
    if given:
        flags = " and ".join(given)
        raise SystemExit(
            f"{prog} update --skills-only refreshes only Lab Tracker skills and references "
            "machine-wide and never touches a repository, so it cannot be combined "
            f"with {flags}. Drop {flags}, or drop --skills-only to update the "
            "repository."
        )


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
    _record_diff(path, existing, content, result)
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
    allow_non_local: bool = False,
    with_review: bool = False,
) -> DemoSeedResult:
    settings = get_settings()
    if not allow_non_local and (
        not settings.is_local_environment() or settings.is_auth_enabled()
    ):
        raise SystemExit(
            "Refusing to seed demo data into a non-local or auth-enabled database "
            f"(LAB_TRACKER_ENVIRONMENT={settings.environment!r}, auth "
            f"{'enabled' if settings.is_auth_enabled() else 'disabled'}). The demo "
            "project is for local development; pass --allow-non-local if you "
            "really mean to write it into this database."
        )
    if run_migrations:
        alembic_config = _alembic_config()
        command.upgrade(alembic_config, "head")

    engine = get_engine(settings)
    session_factory = get_session_factory(engine=engine)
    try:
        with session_factory() as session:
            repository = SQLAlchemyLabTrackerRepository(session)
            root_api = LabTrackerAPI(settings=settings)
            with root_api.request_scope(repository, surface="cli") as scope:
                result = seed_demo_data(
                    scope.api,
                    allow_duplicates=allow_duplicates,
                    with_review=with_review,
                )
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
    parser.add_argument(
        "--version", action="version", version=f"{DISTRIBUTION_NAME} {__version__}"
    )
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
            "Install both Lab Tracker skills and references into the Claude and Codex "
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
        default=None,
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
        help=(
            "Refresh both Lab Tracker skills and references in the Claude and Codex "
            "homes, in addition to updating the repo (use --skills-only to leave "
            "the repo alone)."
        ),
    )
    update_parser.add_argument(
        "--skills-only",
        action="store_true",
        help=(
            "Install or refresh both Lab Tracker skills and references in the Claude and "
            "Codex homes, machine-wide; implies --install-skills and touches no "
            "repo or file in the current directory, unless LAB_TRACKER_SKILLS_HOME "
            "is a relative path (use an absolute one). Cannot be combined with "
            "--yes or --target; --dry-run previews."
        ),
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
    seed_parser.add_argument(
        "--allow-non-local",
        action="store_true",
        help=(
            "Seed even when the configured database is non-local "
            "(LAB_TRACKER_ENVIRONMENT != local) or has authentication enabled."
        ),
    )
    seed_parser.add_argument(
        "--with-review",
        action="store_true",
        help=(
            "Also stage a golden-day set of captures and a READY draft batch so the "
            "review page has something to review (no model call; idempotent per demo project)."
        ),
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
    from lab_tracker.maintenance.cli import add_parser as add_maintenance_parser

    add_maintenance_parser(subcommands)
    models_parser = subcommands.add_parser(
        "models",
        help="Audit configured AI models against dated, source-backed recommendations.",
    )
    models_parser.add_argument(
        "--check-availability", action="store_true",
        help="Query provider model metadata using configured credentials; no inference calls.",
    )
    models_parser.add_argument(
        "--strict", action="store_true",
        help="Exit 1 if an active model needs review or an explicit availability check fails.",
    )
    models_parser.add_argument("--json", action="store_true", help="Print the full model audit.")
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
    add_integrations_parsers(subcommands)

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
        if args.skills_only:
            # Before resolving the MCP URL: skills-only never uses it, so a
            # malformed LAB_TRACKER_BASE_URL must not break the command.
            reject_skills_only_conflicts(_DOCUMENTED_PROG, yes=args.yes, target=args.target)
            result = refresh_setup_skills(dry_run=args.dry_run)
        else:
            from lab_tracker_client.setup import resolved_base_url_for_setup

            mcp_base_url, _ = resolved_base_url_for_setup()
            result = update_consumer_repo(
                args.target or ".",
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
                allow_non_local=args.allow_non_local,
                with_review=args.with_review,
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
    elif args.command == "maintenance":
        from lab_tracker.maintenance.cli import run_command as run_maintenance_command

        raise SystemExit(run_maintenance_command(args))
    elif args.command == "models":
        from lab_tracker.ai_model_audit import audit_ai_models

        audit = audit_ai_models(get_settings(), check_availability=args.check_availability)
        if args.json:
            print(json.dumps(audit.as_dict(), indent=2))
        else:
            for model in audit.models:
                active = "active" if model.active else "inactive"
                print(f"{model.setting}={model.configured_model} ({active}, {model.currency})")
                print(
                    f"  Recommended: {model.recommended_model}; review due: {model.review_due_on}"
                )
                if model.review_overdue:
                    print("  Recommendation review is overdue; check the official source.")
                print(f"  Source: {model.source_url}")
                for warning in model.warnings:
                    print(f"  {warning}")
                if args.check_availability:
                    print(f"  Configured model metadata: {model.availability.status}")
                    print(
                        "  Recommended model metadata: "
                        f"{model.recommendation_availability.status}"
                    )
            print(
                "Model metadata access does not verify billing, graph quality "
                "or inference readiness."
            )
        if args.strict and audit.attention_needed:
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
    elif args.command == "integrations":
        exit_code = run_integrations_command(args)
        if exit_code:
            raise SystemExit(exit_code)


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
        _record_diff(path, existing, content, result)
        result.overwritten.append(path)
    else:
        result.created.append(path)


def _plan_managed_blocks(
    blocks: list[_ManagedBlock], *, result: InitResult, dry_run: bool
) -> None:
    """Plan every block until a pass changes nothing.

    A lone-marker repair keeps only the text on one side of the marker, which
    can drop a block planned earlier in the same pass, so the next pass puts
    it back. Nothing is written; :func:`_commit_planned_files` does that.
    """

    for _pass in range(_MAX_PLANNING_PASSES):
        before = dict(result._preview_contents)
        for block in blocks:
            _plan_managed_block(block, result=result, dry_run=dry_run)
        if result._preview_contents == before:
            return
    raise SystemExit(  # pragma: no cover - repairs settle in a few passes
        "The Lab Tracker markers in "
        + ", ".join(str(path) for path in result._planned_files)
        + " did not settle; check them by hand."
    )


def _plan_managed_block(block: _ManagedBlock, *, result: InitResult, dry_run: bool) -> None:
    """Plan ``block`` into its file over the text planned for that file so far.

    Each block of a file is planned against the text the previous one left,
    so a run writes a file at most once and files it in one bucket. Paths to
    one file (a CLAUDE.md symlinked to AGENTS.md) share one plan.
    """

    path = block.path
    existing, twins = _planned_text(path, result)
    _backup_before_unpaired_marker_repair(
        path,
        existing,
        begin_marker=block.begin_marker,
        end_marker=block.end_marker,
        result=result,
        dry_run=dry_run,
    )
    content = _managed_upsert_at(
        path,
        existing,
        block.text,
        begin_marker=block.begin_marker,
        end_marker=block.end_marker,
    )
    if block.document_transform is not None:
        content = block.document_transform(content)
    _set_planned_text(path, content, twins, result)


def _planned_text(path: Path, result: InitResult) -> tuple[str, list[Path]]:
    """The text this run has for ``path`` so far, and the other planned paths to its file.

    The text is what an earlier block planned for the file, through any of
    its paths, or else what is on disk (empty when there is no file yet).
    """

    identity = _file_identity(path)
    twins = [
        other
        for other in result._planned_files
        if other != path and result._planned_identities[other] == identity
    ]
    if path not in result._preview_original_contents:
        result._preview_original_contents[path] = (
            path.read_text(encoding="utf-8") if path.exists() else ""
        )
    planned_as = path if path in result._preview_contents else next(iter(twins), None)
    if planned_as is None:
        return result._preview_original_contents[path], twins
    return result._preview_contents[planned_as], twins


def _set_planned_text(path: Path, content: str, twins: list[Path], result: InitResult) -> None:
    for same_file in (path, *twins):
        result._preview_contents[same_file] = content
    if path not in result._planned_files:
        result._planned_files.append(path)
        result._planned_identities[path] = _file_identity(path)


def _file_identity(path: Path) -> tuple[object, ...]:
    """What makes two paths one file: its inode when it exists, else its resolved path."""

    try:
        stat = path.stat()
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            # A symlink loop can be neither read nor written; say so first.
            raise SystemExit(f"{path}: {exc.strerror}") from exc
        return ("path", str(path.resolve()))
    return ("inode", stat.st_dev, stat.st_ino)


def _commit_planned_files(
    result: InitResult,
    *,
    dry_run: bool,
    unchanged: list[Path],
) -> list[Path]:
    """Write each planned file once, file it as created, overwritten, or unchanged.

    ``unchanged`` is the bucket a file whose final text equals its original
    lands in: ``skipped`` for init and uninstall, ``up_to_date`` for update.
    Each path is reported, but a file reached by two paths is written once,
    after any backup a repair planned. Returns the paths whose text changes.
    """

    if not dry_run:
        for backup, original in result._pending_backups.items():
            backup.write_text(original, encoding="utf-8")
    result._pending_backups.clear()
    # Before any write, so a path to a file another path creates still
    # counts as new.
    existed = {path: path.exists() for path in result._planned_files}
    written: set[tuple[object, ...]] = set()
    changed: list[Path] = []
    for path in result._planned_files:
        content = result._preview_contents[path]
        original = result._preview_original_contents[path]
        if existed[path] and content == original:
            result.diffs.pop(path, None)
            unchanged.append(path)
            continue
        if dry_run or existed[path]:
            _record_diff(path, original, content, result)
        identity = result._planned_identities[path]
        if not dry_run and identity not in written:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            written.add(identity)
        (result.overwritten if existed[path] else result.created).append(path)
        changed.append(path)
    result._planned_files.clear()
    result._planned_identities.clear()
    return changed


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
    """Put ``block`` in place of the managed region, or after ``existing``.

    Only the region from BEGIN through END (and the version line after END)
    is Lab Tracker's: the text around it, blank lines included, is kept byte
    for byte, so an up-to-date block leaves the file unchanged. A lone marker
    is repaired by :func:`_managed_block_parts`; with no marker, the block is
    appended after one blank line.
    """

    region = _managed_region(existing, begin_marker, end_marker)
    if region is not None:
        start, stop = region
        rest = existing[stop:]
        if rest.startswith("\n") and block.endswith("\n"):
            # The newline after the old region ends the new one too.
            return existing[:start] + block[:-1] + rest
        return existing[:start] + block + rest
    prefix, suffix, removed = _managed_block_parts(existing, begin_marker, end_marker)
    if removed:
        return _join_surrounding_block(prefix, block, suffix)
    if existing.strip():
        return f"{existing.rstrip()}\n\n{block}"
    return block


def _managed_upsert_at(
    path: Path,
    existing: str,
    block: str,
    *,
    begin_marker: str,
    end_marker: str,
) -> str:
    try:
        return _upsert_managed_block(
            existing, block, begin_marker=begin_marker, end_marker=end_marker
        )
    except ManagedBlockMarkerError as exc:
        raise SystemExit(f"{path}: {exc}") from exc


def _plan_strip(block: _ManagedBlock, *, result: InitResult, dry_run: bool) -> None:
    """Plan removing ``block``'s region from its file, over the text planned so far."""

    path = block.path
    if not path.exists():
        # Nothing to strip, a dangling or looping link included.
        if path not in result.skipped:
            result.skipped.append(path)
        return
    existing, twins = _planned_text(path, result)
    try:
        prefix, suffix, removed = _managed_block_parts(
            existing, block.begin_marker, block.end_marker
        )
    except ManagedBlockMarkerError as exc:
        raise SystemExit(f"{path}: {exc}") from exc
    if removed:
        _backup_before_unpaired_marker_repair(
            path,
            existing,
            begin_marker=block.begin_marker,
            end_marker=block.end_marker,
            result=result,
            dry_run=dry_run,
        )
        existing = _join_surrounding_text(prefix, suffix)
    _set_planned_text(path, existing, twins, result)


def _backup_before_unpaired_marker_repair(
    path: Path,
    existing: str,
    *,
    begin_marker: str,
    end_marker: str,
    result: InitResult,
    dry_run: bool,
) -> None:
    """Plan saving the file before a repair that drops text around a lone marker.

    With only one marker present there is no way to tell where the stale
    managed block ends, so the repair discards everything after a lone BEGIN
    (or before a lone END). Keep the original next to the file so nothing the
    user wrote there is lost, and say so. :func:`_commit_planned_files`
    writes the backup before the repaired file.
    """

    if (begin_marker in existing) == (end_marker in existing):
        return
    if path in result.backups:
        # An earlier repair in this run already saved the original text.
        return
    backup = path.with_name(path.name + _UPDATE_BACKUP_SUFFIX)
    result.backups[path] = backup
    lone_marker = begin_marker if begin_marker in existing else end_marker
    tense = "would be" if dry_run else "was"
    result.warnings.append(
        f"{path} had an unpaired Lab Tracker marker ({lone_marker}); text "
        f"around it {tense} replaced during repair, and the original {tense} "
        f"saved at {backup}."
    )
    # The original, not text an earlier block of this run only planned.
    result._pending_backups[backup] = result._preview_original_contents[path]


class ManagedBlockMarkerError(ValueError):
    """A file holds both Lab Tracker markers of a block, but END comes first."""


def _marker_order_error(begin_marker: str, end_marker: str) -> ManagedBlockMarkerError:
    return ManagedBlockMarkerError(
        f"{end_marker} comes before {begin_marker}, so the managed block "
        "cannot be found; remove or reorder the stray marker by hand, then "
        "run the command again."
    )


def marker_order_suggestion(names: list[str]) -> str:
    """What to do about files whose managed-block markers are out of order."""

    return (
        f"A Lab Tracker END marker comes before its BEGIN in {', '.join(names)}, "
        "so `lt update` stops before writing anything; remove or reorder the "
        "stray marker by hand, then run `lt update`."
    )


def _managed_region(existing: str, begin_marker: str, end_marker: str) -> tuple[int, int] | None:
    """The span of the first BEGIN...END region, or ``None`` when it is not paired.

    The span runs from BEGIN to the end of END or, for a code-conventions
    block whose version comment is the next non-blank text, to where that
    comment stops (see :func:`_version_comment_stop`). The newline after the
    span and everything around it stay outside.
    """

    start = existing.find(begin_marker)
    if start == -1:
        return None
    end = existing.find(end_marker, start + len(begin_marker))
    if end == -1:
        return None
    stop = end + len(end_marker)
    if end_marker in _VERSIONED_END_MARKERS:
        after = existing[stop:]
        following = after.lstrip()
        if following.startswith(_CONVENTIONS_VERSION_LINE_PREFIX):
            stop = _version_comment_stop(existing, stop + len(after) - len(following))
    return start, stop


def _version_comment_stop(text: str, comment_start: int) -> int:
    """Where the version comment at ``comment_start`` stops being Lab Tracker's.

    That is the end of its line or, when other text follows the comment on
    that line, the end of its ``-->``, so the other text stays the user's.
    """

    line_end = text.find("\n", comment_start)
    line_end = len(text) if line_end == -1 else line_end
    comment_end = text.find("-->", comment_start, line_end)
    if comment_end != -1 and text[comment_end + 3 : line_end].strip():
        return comment_end + 3
    return line_end


def _managed_block_parts(
    existing: str,
    begin_marker: str,
    end_marker: str,
) -> tuple[str, str, bool]:
    region = _managed_region(existing, begin_marker, end_marker)
    if region is not None:
        start, stop = region
        return existing[:start], existing[stop:], True
    has_begin = begin_marker in existing
    has_end = end_marker in existing
    if has_begin and has_end:
        raise _marker_order_error(begin_marker, end_marker)
    if has_begin:
        prefix, _rest = existing.split(begin_marker, 1)
        return prefix, "", True
    if has_end:
        _old_block, suffix = existing.split(end_marker, 1)
        if end_marker in _VERSIONED_END_MARKERS:
            suffix = _drop_trailing_version_line(suffix)
        return "", suffix, True
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
    if stripped.startswith(_CONVENTIONS_VERSION_LINE_PREFIX):
        rest = stripped[_version_comment_stop(stripped, 0) :]
        return rest[1:] if rest.startswith("\n") else rest
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
    _record_diff(path, existing, content, result)


def _record_diff(path: Path, existing: str, content: str, result: InitResult) -> None:
    """Keep how ``path`` changes from the text it had before this run."""

    original = result._preview_original_contents.setdefault(path, existing)
    diff = _text_diff(path, original, content)
    if diff:
        result.diffs[path] = diff
    else:
        result.diffs.pop(path, None)


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
    region = _managed_region(content, begin_marker, end_marker)
    if region is None:
        if begin_marker in content and end_marker in content:
            raise _marker_order_error(begin_marker, end_marker)
        return None
    body_start = region[0] + len(begin_marker)
    body_end = content.find(end_marker, body_start)
    return content[body_start:body_end].strip() + "\n"


def _extract_version_line(
    content: str,
    *,
    end_marker: str,
) -> str:
    if end_marker not in content:
        return ""
    _prefix, suffix = content.split(end_marker, 1)
    stripped = suffix.lstrip()
    if not stripped.startswith(_CONVENTIONS_VERSION_LINE_PREFIX):
        return ""
    return stripped[: _version_comment_stop(stripped, 0)]


def _doctor(target: str | Path = ".") -> dict[str, object]:
    from lab_tracker_client.setup import _agent_hooks_status, agent_capture_suggestions

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
        try:
            found_body = _extract_managed_body(
                content,
                begin_marker=begin_marker,
                end_marker=end_marker,
            )
        except ManagedBlockMarkerError as exc:
            # Present but unreadable: drifted, and named, so `lt doctor --all`
            # still reports the other repos.
            targets.append(
                {
                    "name": name,
                    "path": str(path),
                    "present": True,
                    "in_sync": False,
                    "body_in_sync": False,
                    "version_in_sync": False,
                    "drifted": True,
                    "version_line": "",
                    "error": f"{path}: {exc}",
                }
            )
            continue
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
    agent_hooks = _agent_hooks_status(root)
    payload["agent_hooks"] = agent_hooks
    payload["suggestions"] = agent_capture_suggestions(agent_hooks)
    errors = [str(target["error"]) for target in targets if "error" in target]
    if errors:
        # `lt update` refuses these files, so it cannot be the advice.
        payload["errors"] = errors
        payload["suggestion"] = marker_order_suggestion(
            [str(target["name"]) for target in targets if "error" in target]
        )
    elif any(target["drifted"] for target in targets):
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
    # Both commands recognise the hook payload Claude Code pipes on stdin and
    # answer with hookSpecificOutput.additionalContext; their plain JSON
    # results would be parsed as hook output and dropped. See
    # lab_tracker_client.hook_context.
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


def _claude_settings_json_for(root: Path) -> str:
    """The scaffold settings, keeping `lt setup agent-hooks` entries already opted into.

    `lt update` and `init --force` rewrite `.claude/settings.json` to the
    canonical scaffold; the agent-hooks entries are a separate consent, so a
    refresh carries them forward instead of silently dropping them.
    """

    canonical = _claude_settings_json()
    with suppress(Exception):
        from lab_tracker_client.agent_hooks import carry_forward_agent_hooks

        return carry_forward_agent_hooks(canonical, root / ".claude" / "settings.json")
    return canonical


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
        `lt watch add`), and `lt setup connect`, `lt project bind`,
        `lt hooks install`, `lt setup autotrack`, `lt setup schedule`, and
        `lt setup agent-hooks` also require `--yes`; suggest them to the user
        rather than applying them unprompted.

        {EMPTY_PROJECT_ORIENTATION_POLICY}

        {AGENT_SESSION_CAPTURE_DISCOVERY_POLICY}

        {CLIENT_MAINTENANCE_POLICY}

        Capture that needs no code changes, once this checkout is bound to a
        project: `lt setup autotrack` captures figures notebooks save or
        display (`--jupyter`, `--scripts`, and `--r` extend it), `lt run
        --output <dir> -- <command>` records one analysis run, `lt pipeline
        report` records a pipeline run, and `lt capture file <path>` stages one
        saved file from any language. The `lab-tracker://setup-guide` MCP
        resource lists every capture path; offer them rather than enabling
        them unasked.

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
