"""Single installer for the Lab Tracker post-commit capture hook.

``lt hooks install`` writes the ``lt repo`` hook — the ``REPO HOOK`` block
rendered by :func:`lab_tracker_client.repo.hook_managed_block`, whose commit
events carry the bounded diff and conventions snapshot the legacy ``lt git
snapshot`` hook used to record (decision lt-81s6.17: the ``lt repo`` event is
the surviving payload). The legacy ``GRAPH DRAFT`` markers written by the old
installer (and by ``scripts/install-git-graph-draft-hook.ps1``) are recognised
only to migrate such a block in place, carrying its baked project id and base
URL forward so an upgrade can never silently unbind capture. The hook always
exits 0, so a commit is never blocked; proposal generation is deliberately left
to the configured daily-review schedule or an explicit on-demand command.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import lab_tracker_client.repo as repo_capture
from lab_tracker_client.client import LTValidationError
from lab_tracker_client.git_capture import project_from_ids
from lab_tracker_client.repo import HOOK_BEGIN_MARKER as REPO_HOOK_BLOCK_BEGIN
from lab_tracker_client.repo import HOOK_END_MARKER as REPO_HOOK_BLOCK_END
from lab_tracker_client.repo import hook_text_diff

JsonObject = dict[str, Any]

# Legacy ``lt git snapshot`` block markers: recognised for migration and
# removal only; new installs never write them.
HOOK_BLOCK_BEGIN = "# --- BEGIN LAB TRACKER GRAPH DRAFT HOOK ---"
HOOK_BLOCK_END = "# --- END LAB TRACKER GRAPH DRAFT HOOK ---"
_LT_LINE_PATTERN = re.compile(r'LAB_TRACKER_LT="\$\{LAB_TRACKER_LT:-(?P<path>[^}]*)\}"')
_PROJECT_LINE_PATTERN = re.compile(
    r'LAB_TRACKER_PROJECT_ID="\$\{LAB_TRACKER_PROJECT_ID:-(?P<value>[^}]*)\}"'
)
_BASE_URL_LINE_PATTERN = re.compile(
    r'LAB_TRACKER_BASE_URL="\$\{LAB_TRACKER_BASE_URL:-(?P<value>[^}]*)\}"'
)
# The REPO HOOK block single-quotes every baked value (``'\''`` escapes a quote).
_SH_SINGLE_QUOTED = r"'(?P<value>(?:[^']|'\\'')*)'"
_REPO_LT_LINE_PATTERN = re.compile(r"^\s*\[ -n \"\$LT\" \] \|\| LT=" + _SH_SINGLE_QUOTED, re.M)
_REPO_BASE_URL_LINE_PATTERN = re.compile(r"^\s*LAB_TRACKER_BASE_URL=" + _SH_SINGLE_QUOTED, re.M)
_ACTION_NAMES = {
    # ``lt hooks install`` has always reported ``created`` for a fresh hook.
    repo_capture.HOOK_ACTION_INSTALLED: "created",
}
REMOVED_BLOCK_REPO = "repo"
REMOVED_BLOCK_LEGACY = "graph-draft"


def hook_path_for_repo(repo: str | Path = ".") -> tuple[Path, Path]:
    """Return (repo_root, post-commit hook path), honoring core.hooksPath."""

    repo_root = _toplevel(repo)
    completed = subprocess.run(  # noqa: S603 - fixed executable, no shell.
        ["git", "rev-parse", "--git-path", "hooks/post-commit"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise LTValidationError(f"Could not resolve the hook path for {repo_root}.")
    raw = _normalize_git_path(completed.stdout.strip())
    path = Path(raw)
    if not path.is_absolute():
        path = repo_root / path
    return repo_root, path.resolve()


def hook_lt_path(content: str) -> str | None:
    """The ``lt`` path baked into a hook: REPO HOOK block first, then legacy."""

    match = _REPO_LT_LINE_PATTERN.search(content)
    if match:
        return _sh_unquote(match.group("value"))
    legacy = _LT_LINE_PATTERN.search(content)
    return legacy.group("path") if legacy else None


def install_hook(
    *,
    repo: str | Path = ".",
    project_id: str | None = None,
    base_url: str | None = None,
    lt_path: str | None = None,
    config_path: str | Path | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> JsonObject:
    """Install the ``lt repo`` post-commit hook, creating ``repo.json`` if needed.

    The project id comes from ``--project``, a legacy block's baked default,
    ``LAB_TRACKER_PROJECT_ID`` or the repo's ``lt_ids.json`` binding when no
    ``repo.json`` exists yet; when one exists, a conflicting ``--project`` is
    refused rather than silently overridden. A legacy GRAPH DRAFT block is
    migrated in place; a foreign hook needs ``force``; ``dry_run`` reports the
    diff and the config it would create without writing anything.
    """

    repo_root, hook_path = hook_path_for_repo(repo)
    resolved_lt = lt_path or _default_lt_path()
    existing = _read_hook(hook_path)
    legacy_present = _require_paired_markers(existing, hook_path, HOOK_BLOCK_BEGIN, HOOK_BLOCK_END)
    _require_paired_markers(existing, hook_path, REPO_HOOK_BLOCK_BEGIN, REPO_HOOK_BLOCK_END)
    carried_project: str | None = None
    carried_base_url: str | None = None
    if legacy_present:
        # A legacy PS1-installed block may be the repo's ONLY project/URL
        # binding; replacing it wholesale would silently kill capture. Carry
        # the baked defaults forward unless the caller overrides them.
        if project_id is None:
            match = _PROJECT_LINE_PATTERN.search(existing)
            carried_project = (match.group("value").strip() or None) if match else None
            project_id = carried_project
        if base_url is None:
            match = _BASE_URL_LINE_PATTERN.search(existing)
            carried_base_url = (match.group("value").strip() or None) if match else None
            base_url = carried_base_url
        _require_safe_carried_value("project id", carried_project, "--project")
        _require_safe_carried_value("base URL", carried_base_url, "--base-url")
    config = _resolve_repo_config(repo_root, project_id=project_id, config_path=config_path)
    if config.create:
        plan = repo_capture.plan_post_commit_hook(
            repo_root,
            lt_command=resolved_lt,
            config_path=config.path,
            force=force,
            base_url=base_url,
            migrate_legacy=True,
        )
        if not dry_run:
            repo_capture.init_config(project_id=config.project_id, config_path=config.path)
            repo_capture.write_hook_plan(plan)
        result = repo_capture.hook_plan_payload(plan, dry_run=dry_run)
    else:
        result = repo_capture.install_post_commit_hook(
            repo_root,
            lt_command=resolved_lt,
            config_path=config.path,
            force=force,
            base_url=base_url,
            dry_run=dry_run,
            migrate_legacy=True,
        )
    payload: JsonObject = {
        "command": "hooks-install",
        "repo": str(repo_root),
        "hook_path": result["hook_path"],
        "action": _ACTION_NAMES.get(str(result["action"]), str(result["action"])),
        "lt_path": result["lt_command"],
        "config": result["config"],
        "project_id": config.project_id,
        "dry_run": dry_run,
        "diff": result["diff"],
    }
    if config.create:
        payload["would_create_config" if dry_run else "created_config"] = str(config.path)
    hooks_path_change = _normalize_core_hooks_path(
        repo_root=repo_root,
        hook_path=hook_path,
        dry_run=dry_run,
    )
    if hooks_path_change:
        payload["core_hooks_path"] = hooks_path_change
    if carried_project:
        payload["carried_project_id"] = carried_project
    if carried_base_url:
        payload["carried_base_url"] = carried_base_url
    if dry_run:
        return payload
    from lab_tracker_client.registry import record_repo

    record_repo(repo_root, "hooks-install")
    return payload


@dataclass(frozen=True)
class _RepoConfigChoice:
    """Where the hook's ``repo.json`` is, and whether the install creates it."""

    path: Path
    project_id: str
    create: bool


def _resolve_repo_config(
    repo_root: Path,
    *,
    project_id: str | None,
    config_path: str | Path | None,
) -> _RepoConfigChoice:
    resolved = (
        Path(config_path).expanduser().resolve()
        if config_path
        else repo_capture.find_config_path(start=repo_root)
    )
    if resolved is not None and resolved.exists():
        config = repo_capture.load_config(config_path=resolved)
        if project_id and project_id != config.project_id:
            raise LTValidationError(
                f"--project {project_id!r} conflicts with the project "
                f"{config.project_id!r} recorded in {resolved}. Edit repo.json (or "
                "re-run 'lt repo init --force') to change the project; the hook "
                "never overrides it silently."
            )
        return _RepoConfigChoice(path=resolved, project_id=config.project_id, create=False)
    resolved_project = (
        _optional(project_id)
        or _optional(os.getenv("LAB_TRACKER_PROJECT_ID"))
        or project_from_ids(repo_root)
    )
    if not resolved_project:
        raise LTValidationError(
            "No project id for the commit hook. Pass --project, bind one with "
            "'lt project bind', or run 'lt repo init'."
        )
    path = resolved or repo_root / repo_capture.DEFAULT_CONFIG_RELATIVE_PATH
    return _RepoConfigChoice(path=path, project_id=resolved_project, create=True)


def uninstall_hook(*, repo: str | Path = ".", dry_run: bool = False) -> JsonObject:
    """Strip the REPO HOOK block and, if present, the legacy GRAPH DRAFT block."""

    repo_root, hook_path = hook_path_for_repo(repo)
    existing = _read_hook(hook_path)
    payload: JsonObject = {
        "command": "hooks-uninstall",
        "repo": str(repo_root),
        "hook_path": str(hook_path),
        "dry_run": dry_run,
    }
    removed_blocks: list[str] = []
    remainder = existing
    for name, begin, end in (
        (REMOVED_BLOCK_REPO, REPO_HOOK_BLOCK_BEGIN, REPO_HOOK_BLOCK_END),
        (REMOVED_BLOCK_LEGACY, HOOK_BLOCK_BEGIN, HOOK_BLOCK_END),
    ):
        if not _require_paired_markers(remainder, hook_path, begin, end):
            continue
        pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end) + r"\n?", re.DOTALL)
        remainder = pattern.sub("", remainder, count=1)
        removed_blocks.append(name)
    payload["removed_blocks"] = removed_blocks
    if not removed_blocks:
        payload["action"] = "absent"
        return payload
    only_shebang = not any(
        line.strip() and not line.strip().startswith("#!")
        for line in remainder.splitlines()
    )
    payload["action"] = "removed-hook-file" if only_shebang else "stripped-block"
    payload["diff"] = hook_text_diff(hook_path, existing, "" if only_shebang else remainder)
    if dry_run:
        return payload
    if only_shebang:
        hook_path.unlink()
    else:
        cleaned = remainder.replace("\r\n", "\n").rstrip() + "\n"
        hook_path.write_text(cleaned, encoding="utf-8", newline="\n")
    return payload


def hook_status(*, repo: str | Path = ".") -> JsonObject:
    repo_root, hook_path = hook_path_for_repo(repo)
    existing = _read_hook(hook_path)
    repo_begin = REPO_HOOK_BLOCK_BEGIN in existing
    repo_end = REPO_HOOK_BLOCK_END in existing
    legacy_begin = HOOK_BLOCK_BEGIN in existing
    legacy_end = HOOK_BLOCK_END in existing
    lt_path = hook_lt_path(existing)
    lt_path_exists: bool | None = None
    if lt_path is not None:
        with suppress(OSError):
            lt_path_exists = Path(lt_path).exists()
    config_path = repo_capture.find_config_path(start=repo_root)
    baked_project_id: str | None = None
    if config_path is not None:
        with suppress(Exception):
            baked_project_id = repo_capture.load_config(config_path=config_path).project_id
    if baked_project_id is None:
        project_match = _PROJECT_LINE_PATTERN.search(existing)
        baked_project_id = project_match.group("value") if project_match else None
    base_url_match = _REPO_BASE_URL_LINE_PATTERN.search(existing)
    baked_base_url = _sh_unquote(base_url_match.group("value")) if base_url_match else None
    if baked_base_url is None:
        legacy_url = _BASE_URL_LINE_PATTERN.search(existing)
        baked_base_url = legacy_url.group("value") if legacy_url else None
    return {
        "command": "hooks-status",
        "repo": str(repo_root),
        "hook_path": str(hook_path),
        "hook_present": hook_path.exists(),
        "managed_block_present": repo_begin and repo_end,
        "legacy_block_present": legacy_begin and legacy_end,
        "markers_unpaired": (repo_begin != repo_end) or (legacy_begin != legacy_end),
        "lt_path": lt_path,
        "lt_path_exists": lt_path_exists,
        "config": str(config_path) if config_path is not None else None,
        "baked_project_id": baked_project_id,
        "baked_base_url": baked_base_url,
    }


def _toplevel(repo: str | Path) -> Path:
    completed = subprocess.run(  # noqa: S603 - fixed executable, no shell.
        ["git", "-C", str(Path(repo).expanduser()), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise LTValidationError(
            f"Not a git repository: {Path(repo).expanduser().resolve()}"
        )
    return Path(_normalize_git_path(completed.stdout.strip())).resolve()


def _normalize_core_hooks_path(
    *,
    repo_root: Path,
    hook_path: Path,
    dry_run: bool,
) -> JsonObject | None:
    """Keep Beads-style hook enrollment executable from Windows and POSIX Git.

    Older setup paths sometimes left ``core.hooksPath`` as an absolute MSYS/WSL
    spelling of this repo's ``.beads/hooks`` directory. Git for Windows can
    resolve and print those paths differently than the shell that later runs
    the hook, so once the hook path is known to be the repo's Beads hook
    directory, store the portable repo-relative form.
    """

    desired_dir = (repo_root / ".beads" / "hooks").resolve()
    if hook_path.parent.resolve() != desired_dir:
        return None
    completed = subprocess.run(  # noqa: S603 - fixed executable, no shell.
        ["git", "config", "--get", "core.hooksPath"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    previous = completed.stdout.strip() if completed.returncode == 0 else ""
    desired = ".beads/hooks"
    if previous == desired:
        return {
            "action": "unchanged",
            "previous": previous,
            "desired": desired,
        }
    if not dry_run:
        subprocess.run(  # noqa: S603 - fixed executable, no shell.
            ["git", "config", "core.hooksPath", desired],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    return {
        "action": "would-normalize" if dry_run else "normalized",
        "previous": previous,
        "desired": desired,
    }


def _normalize_git_path(raw: str) -> str:
    # Git run under MSYS/WSL shells can emit /c/... or /mnt/c/... forms for
    # Windows drives; fold them back to drive-letter paths.
    if sys.platform == "win32":
        match = re.match(r"^/(?:mnt/)?([A-Za-z])/(.*)$", raw)
        if match:
            return f"{match.group(1).upper()}:/{match.group(2)}"
    return raw


def _read_hook(hook_path: Path) -> str:
    if not hook_path.exists():
        return ""
    try:
        return hook_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise LTValidationError(
            f"Existing post-commit hook is not UTF-8 text: {hook_path}"
        ) from exc


def _require_paired_markers(existing: str, hook_path: Path, begin: str, end: str) -> bool:
    """True when both markers are present in order; raise on corruption.

    A lone or reversed marker means a hand-mangled hook; rewriting around it
    with a DOTALL regex could delete user content, so refuse instead.
    """

    has_begin = begin in existing
    has_end = end in existing
    if has_begin != has_end or (has_begin and existing.index(begin) > existing.index(end)):
        raise LTValidationError(
            f"post-commit hook has unpaired Lab Tracker markers: {hook_path}. "
            "Repair or remove the markers manually before re-running."
        )
    return has_begin and has_end


def _default_lt_path() -> str:
    # Prefer the sibling of this interpreter: it is guaranteed to be the same
    # environment that provides the capture adapters; PATH may find a
    # different install.
    sibling = Path(sys.executable).parent / ("lt.exe" if sys.platform == "win32" else "lt")
    if sibling.exists():
        return str(sibling)
    found = shutil.which("lt")
    if found:
        return found
    raise LTValidationError(
        "Could not locate the lt executable for the hook body; pass --lt-path."
    )


# Characters that end or escape a double-quoted "${VAR:-default}" expansion, or
# run code inside it. A legacy block's carried values were written in that
# format, so a value containing them was never a plain project id or URL and
# is refused rather than re-baked.
_SH_DEFAULT_UNSAFE = frozenset('"$`\\}\n\r\x00')


def _sh_default_unsafe(value: str) -> list[str]:
    return sorted({repr(ch) for ch in value if ch in _SH_DEFAULT_UNSAFE})


def _require_safe_carried_value(label: str, value: str | None, flag: str) -> None:
    unsafe = _sh_default_unsafe(value) if value else []
    if unsafe:
        raise LTValidationError(
            f"The {label} {value!r} carried forward from the existing post-commit "
            f"hook block cannot be baked into the new block: it contains "
            f"{', '.join(unsafe)}, which sh would expand or execute. Pass {flag} "
            "with a safe value to override it."
        )


def _sh_unquote(value: str) -> str:
    """Reverse ``repo._sh_single_quote`` for a value matched inside its quotes."""

    return value.replace("'\\''", "'")


def _optional(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None
