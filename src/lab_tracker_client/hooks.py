"""Cross-platform enrollment of the Lab Tracker post-commit snapshot hook.

Pure-Python replacement for ``scripts/install-git-graph-draft-hook.ps1``. The
managed block keeps the exact same BEGIN/END markers, so repos enrolled by the
legacy PowerShell installer upgrade in place. The new hook body invokes the
packaged ``lt git snapshot --fail-silent`` (outbox-backed, so unreachable-server
commits queue instead of being lost) rather than a source-checkout script, and
always exits 0 so a commit is never blocked.
"""

from __future__ import annotations

import difflib
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from lab_tracker_client.client import LTValidationError
from lab_tracker_client.repo import HOOK_BEGIN_MARKER as REPO_HOOK_BLOCK_BEGIN

JsonObject = dict[str, Any]

HOOK_BLOCK_BEGIN = "# --- BEGIN LAB TRACKER GRAPH DRAFT HOOK ---"
HOOK_BLOCK_END = "# --- END LAB TRACKER GRAPH DRAFT HOOK ---"
_SHEBANG = "#!/usr/bin/env sh"
_LT_LINE_PATTERN = re.compile(r'LAB_TRACKER_LT="\$\{LAB_TRACKER_LT:-(?P<path>[^}]*)\}"')
_PROJECT_LINE_PATTERN = re.compile(
    r'LAB_TRACKER_PROJECT_ID="\$\{LAB_TRACKER_PROJECT_ID:-(?P<value>[^}]*)\}"'
)
_BASE_URL_LINE_PATTERN = re.compile(
    r'LAB_TRACKER_BASE_URL="\$\{LAB_TRACKER_BASE_URL:-(?P<value>[^}]*)\}"'
)


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


def managed_hook_block(
    *,
    lt_path: str,
    project_id: str | None = None,
    base_url: str | None = None,
    request_draft: bool = True,
) -> str:
    """Render the POSIX-sh managed block (LF only; runs under Git-for-Windows sh)."""

    lines = [
        HOOK_BLOCK_BEGIN,
        'LAB_TRACKER_GIT_DRAFT_ENABLED="${LAB_TRACKER_GIT_DRAFT_ENABLED:-1}"',
        'if [ "$LAB_TRACKER_GIT_DRAFT_ENABLED" != "0" ]; then',
        f'  LAB_TRACKER_LT="${{LAB_TRACKER_LT:-{_hook_path_text(lt_path)}}}"',
    ]
    if base_url:
        lines.extend(
            [
                f'  LAB_TRACKER_BASE_URL="${{LAB_TRACKER_BASE_URL:-{base_url}}}"',
                "  export LAB_TRACKER_BASE_URL",
            ]
        )
    if project_id:
        lines.extend(
            [
                f'  LAB_TRACKER_PROJECT_ID="${{LAB_TRACKER_PROJECT_ID:-{project_id}}}"',
                "  export LAB_TRACKER_PROJECT_ID",
            ]
        )
    draft_flag = " --request-draft" if request_draft else ""
    # No --fail-silent here: a nonzero lt exit (snapshot failed, or queued but
    # not yet synced) must reach the || so the one-line warning stays
    # reachable. post-commit hooks are advisory — git never blocks the commit
    # on them — and the || echo keeps the block's own status 0 regardless.
    warning = (
        "lab-tracker: commit snapshot did not fully sync; "
        "queued events retry on the next sync. Commit kept."
    )
    lines.extend(
        [
            f'  "$LAB_TRACKER_LT" git snapshot{draft_flag} >/dev/null 2>&1 || \\',
            f'    echo "{warning}" >&2',
            "fi",
            HOOK_BLOCK_END,
        ]
    )
    return "\n".join(lines)


def install_hook(
    *,
    repo: str | Path = ".",
    project_id: str | None = None,
    base_url: str | None = None,
    request_draft: bool = True,
    lt_path: str | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> JsonObject:
    repo_root, hook_path = hook_path_for_repo(repo)
    resolved_lt = lt_path or _default_lt_path()
    existing = _read_hook(hook_path)
    has_block = _require_paired_markers(existing, hook_path)
    carried_project: str | None = None
    carried_base_url: str | None = None
    if has_block:
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
    block = managed_hook_block(
        lt_path=resolved_lt,
        project_id=project_id,
        base_url=base_url,
        request_draft=request_draft,
    )
    if REPO_HOOK_BLOCK_BEGIN in existing and not has_block and not force:
        # Both Lab Tracker capture hooks in one repo record two staged notes
        # per commit (lt-81s6.17). One adapter per repo unless forced.
        raise LTValidationError(
            "This repo already has the 'lt repo' capture hook installed. "
            "Running both capture hooks records every commit twice. Remove the "
            "REPO HOOK block (or uninstall it) first, or pass --force only if "
            "you deliberately want both."
        )
    if existing.strip() and not has_block and not force:
        raise LTValidationError(
            "Existing post-commit hook is not Lab Tracker-managed. "
            "Pass --force to append the managed block."
        )
    if has_block:
        action = "updated"
        pattern = re.compile(
            re.escape(HOOK_BLOCK_BEGIN) + r".*?" + re.escape(HOOK_BLOCK_END),
            re.DOTALL,
        )
        updated = pattern.sub(lambda _match: block, existing, count=1)
    elif existing.strip():
        action = "appended"
        updated = f"{existing.rstrip()}\n\n{block}\n"
    else:
        action = "created"
        updated = f"{_SHEBANG}\n{block}\n"
    updated = updated.replace("\r\n", "\n")
    if not updated.endswith("\n"):
        updated += "\n"
    payload: JsonObject = {
        "command": "hooks-install",
        "repo": str(repo_root),
        "hook_path": str(hook_path),
        "action": action,
        "lt_path": resolved_lt,
        "request_draft": request_draft,
        "dry_run": dry_run,
        "diff": _text_diff(hook_path, existing, updated),
    }
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
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(updated, encoding="utf-8", newline="\n")
    if sys.platform != "win32":
        hook_path.chmod(0o755)
    from lab_tracker_client.registry import record_repo

    record_repo(repo_root, "hooks-install")
    return payload


def uninstall_hook(*, repo: str | Path = ".", dry_run: bool = False) -> JsonObject:
    repo_root, hook_path = hook_path_for_repo(repo)
    existing = _read_hook(hook_path)
    payload: JsonObject = {
        "command": "hooks-uninstall",
        "repo": str(repo_root),
        "hook_path": str(hook_path),
        "dry_run": dry_run,
    }
    if not _require_paired_markers(existing, hook_path):
        payload["action"] = "absent"
        return payload
    pattern = re.compile(
        re.escape(HOOK_BLOCK_BEGIN) + r".*?" + re.escape(HOOK_BLOCK_END) + r"\n?",
        re.DOTALL,
    )
    remainder = pattern.sub("", existing, count=1)
    only_shebang = not any(
        line.strip() and not line.strip().startswith("#!")
        for line in remainder.splitlines()
    )
    payload["action"] = "removed-hook-file" if only_shebang else "stripped-block"
    payload["diff"] = _text_diff(hook_path, existing, "" if only_shebang else remainder)
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
    has_begin = HOOK_BLOCK_BEGIN in existing
    has_end = HOOK_BLOCK_END in existing
    lt_path: str | None = None
    lt_path_exists: bool | None = None
    match = _LT_LINE_PATTERN.search(existing)
    if match:
        lt_path = match.group("path")
        lt_path_exists = Path(lt_path).exists()
    project_match = _PROJECT_LINE_PATTERN.search(existing)
    base_url_match = _BASE_URL_LINE_PATTERN.search(existing)
    return {
        "command": "hooks-status",
        "repo": str(repo_root),
        "hook_path": str(hook_path),
        "hook_present": hook_path.exists(),
        "managed_block_present": has_begin and has_end,
        "markers_unpaired": has_begin != has_end,
        "lt_path": lt_path,
        "lt_path_exists": lt_path_exists,
        "baked_project_id": project_match.group("value") if project_match else None,
        "baked_base_url": base_url_match.group("value") if base_url_match else None,
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


def _require_paired_markers(existing: str, hook_path: Path) -> bool:
    """True when both markers are present in order; raise on corruption.

    A lone or reversed marker means a hand-mangled hook; rewriting around it
    with a DOTALL regex could delete user content, so refuse instead.
    """

    has_begin = HOOK_BLOCK_BEGIN in existing
    has_end = HOOK_BLOCK_END in existing
    if has_begin != has_end or (
        has_begin and existing.index(HOOK_BLOCK_BEGIN) > existing.index(HOOK_BLOCK_END)
    ):
        raise LTValidationError(
            f"post-commit hook has unpaired Lab Tracker markers: {hook_path}. "
            "Repair or remove the markers manually before re-running."
        )
    return has_begin and has_end


def _default_lt_path() -> str:
    # Prefer the sibling of this interpreter: it is guaranteed to be the same
    # environment that provides git_capture; PATH may find a different install.
    sibling = Path(sys.executable).parent / ("lt.exe" if sys.platform == "win32" else "lt")
    if sibling.exists():
        return str(sibling)
    found = shutil.which("lt")
    if found:
        return found
    raise LTValidationError(
        "Could not locate the lt executable for the hook body; pass --lt-path."
    )


def _hook_path_text(value: str) -> str:
    # The hook body runs under sh even on Windows; forward slashes everywhere.
    return value.replace("\\", "/")


def _text_diff(path: Path, existing: str, proposed: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            existing.splitlines(),
            proposed.splitlines(),
            fromfile=f"{path} (current)",
            tofile=f"{path} (proposed)",
            lineterm="",
        )
    )
