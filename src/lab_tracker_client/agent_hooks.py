"""Consent-gated install of the coding-agent lifecycle hooks: ``lt setup agent-hooks``.

Two Claude Code hook entries, added by default to the checkout's personal
``.claude/settings.local.json`` (Claude Code merges it with the shared file;
it is meant to stay out of git), or, only with ``--shared``, to the usually
committed ``.claude/settings.json``, which enrolls everyone who clones the
repository with ``lt`` configured:

* ``SessionEnd`` -> ``lt agent session-end --fail-silent``: one bounded,
  redacted retrospective of the finished session, queued as a staged note
  that asks for human-reviewed drafts. Its ``timeout`` raises Claude Code's
  shared 1.5-second SessionEnd budget so the capture can finish.
* ``PostToolUse`` (matcher ``Write|Edit|MultiEdit|NotebookEdit``) ->
  ``lt watch touch --fail-silent``, run ``async`` so an edit never waits on
  it: a write into a configured watch folder is queued right away instead of
  at the next scheduled ``lt watch run``.

Because the first one captures agent conversations it is never installed by
``lt setup init``; it is its own explicit opt-in. JSON has no comments, so a
managed entry is recognised by its command (``lt agent session-end ...`` or
``lt watch touch ...``, with or without a path to ``lt``). Everything else in
the file -- the scaffolded ``SessionStart``/``UserPromptSubmit`` hooks and any
hook a person added -- is preserved; installing twice changes nothing, and
``--uninstall`` removes only the managed entries (from the file the scope
names; the other file is reported). ``lt update`` never touches the personal
file and carries managed entries in the shared file forward when it refreshes
the scaffold (:func:`carry_forward_agent_hooks`).
"""

from __future__ import annotations

import copy
import difflib
import json
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lab_tracker_client import gitinfo
from lab_tracker_client.client import LTValidationError
from lab_tracker_client.watch_touch import find_checkout_watch_config

JsonObject = dict[str, Any]

SETTINGS_RELATIVE = Path(".claude") / "settings.json"
LOCAL_SETTINGS_RELATIVE = Path(".claude") / "settings.local.json"
SCOPE_LOCAL = "local"
SCOPE_SHARED = "shared"
SCOPE_FILES: tuple[tuple[str, Path], ...] = (
    (SCOPE_LOCAL, LOCAL_SETTINGS_RELATIVE),
    (SCOPE_SHARED, SETTINGS_RELATIVE),
)
SHARED_SCOPE_WARNING = (
    "--shared writes .claude/settings.json, which is usually committed: once it is, "
    "EVERYONE who clones this repository and has `lt` configured will have their "
    "coding-agent sessions captured (a retrospective of each session staged into the "
    "bound project) without opting in themselves. Leave out --shared to keep the hooks "
    "in your personal .claude/settings.local.json."
)
# SessionEnd hooks share a 1.5 s budget unless a hook's own timeout raises it
# (Claude Code caps the raise at 60 s).
SESSION_END_TIMEOUT_SECONDS = 60
WRITE_TOOLS_MATCHER = "Write|Edit|MultiEdit|NotebookEdit"


@dataclass(frozen=True)
class ManagedHook:
    """One hook entry ``lt setup agent-hooks`` owns."""

    event: str
    matcher: str
    command: str
    identity: tuple[str, str]
    options: tuple[tuple[str, Any], ...] = ()

    def hook(self) -> JsonObject:
        return {"type": "command", "command": self.command, **dict(self.options)}

    def group(self) -> JsonObject:
        return {"matcher": self.matcher, "hooks": [self.hook()]}

    def describe(self) -> JsonObject:
        return {"event": self.event, "matcher": self.matcher, **self.hook()}


SESSION_END_HOOK = ManagedHook(
    event="SessionEnd",
    matcher="",
    command="lt agent session-end --fail-silent",
    identity=("agent", "session-end"),
    options=(("timeout", SESSION_END_TIMEOUT_SECONDS),),
)
WATCH_TOUCH_HOOK = ManagedHook(
    event="PostToolUse",
    matcher=WRITE_TOOLS_MATCHER,
    command="lt watch touch --fail-silent",
    identity=("watch", "touch"),
    options=(("async", True),),
)
MANAGED_HOOKS: tuple[ManagedHook, ...] = (SESSION_END_HOOK, WATCH_TOUCH_HOOK)


def settings_path(target: str | Path = ".", *, shared: bool = False) -> Path:
    """The settings file ``lt setup agent-hooks`` edits: personal unless ``shared``."""

    root = Path(target).expanduser().resolve()
    return root / (SETTINGS_RELATIVE if shared else LOCAL_SETTINGS_RELATIVE)


def is_managed_hook(entry: Any, managed: ManagedHook | None = None) -> bool:
    """True for a command hook whose command is ``lt agent session-end`` / ``lt watch touch``."""

    if not isinstance(entry, Mapping) or entry.get("type", "command") != "command":
        return False
    command = entry.get("command")
    if not isinstance(command, str):
        return False
    tokens = command.split()
    if len(tokens) < 3 or Path(tokens[0].strip("'\"")).stem.lower() != "lt":
        return False
    identities = [managed.identity] if managed else [hook.identity for hook in MANAGED_HOOKS]
    return (tokens[1], tokens[2]) in identities


def load_settings(path: Path) -> tuple[str, JsonObject]:
    """Current text and parsed object; refuses (never clobbers) a file it cannot edit safely."""

    if not path.exists():
        return "", {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return text, {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LTValidationError(
            f"{path} is not valid JSON ({exc}); fix it by hand before adding hooks."
        ) from exc
    if not isinstance(parsed, dict):
        raise LTValidationError(f"{path} must contain a JSON object.")
    hooks = parsed.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise LTValidationError(f"{path}: `hooks` must be a JSON object.")
    for event, groups in (hooks or {}).items():
        if not isinstance(groups, list):
            raise LTValidationError(f"{path}: `hooks.{event}` must be a list.")
    return text, parsed


def with_managed_hooks(settings: Mapping[str, Any]) -> JsonObject:
    """A copy of ``settings`` with exactly one canonical entry per managed hook.

    A canonical group already present stays where it is; stale variants of a
    managed command are removed (dropping a group only when that emptied it);
    a missing entry is appended to its event's list.
    """

    updated: JsonObject = copy.deepcopy(dict(settings))
    hooks = updated.setdefault("hooks", {})
    for managed in MANAGED_HOOKS:
        groups, kept_canonical = _without_managed(hooks.get(managed.event) or [], managed)
        if not kept_canonical:
            groups.append(managed.group())
        hooks[managed.event] = groups
    return updated


def without_managed_hooks(settings: Mapping[str, Any]) -> JsonObject:
    """A copy of ``settings`` with every managed entry removed.

    Event lists and the ``hooks`` object are dropped only when removing the
    managed entries is what emptied them.
    """

    updated: JsonObject = copy.deepcopy(dict(settings))
    hooks = updated.get("hooks")
    if not isinstance(hooks, dict):
        return updated
    for managed in MANAGED_HOOKS:
        if managed.event not in hooks:
            continue
        original = hooks[managed.event]
        groups, _kept = _without_managed(original, managed, keep_canonical=False)
        if groups:
            hooks[managed.event] = groups
        elif original:
            del hooks[managed.event]
    if not hooks and settings.get("hooks"):
        del updated["hooks"]
    return updated


def _without_managed(
    groups: list[Any], managed: ManagedHook, *, keep_canonical: bool = True
) -> tuple[list[Any], bool]:
    kept: list[Any] = []
    kept_canonical = False
    canonical = managed.group()
    for group in groups:
        if keep_canonical and not kept_canonical and group == canonical:
            kept.append(group)
            kept_canonical = True
            continue
        if not isinstance(group, Mapping) or not isinstance(group.get("hooks"), list):
            kept.append(group)
            continue
        entries = [entry for entry in group["hooks"] if not is_managed_hook(entry, managed)]
        if len(entries) == len(group["hooks"]):
            kept.append(group)
        elif entries:
            kept.append({**group, "hooks": entries})
    return kept, kept_canonical


def render_settings(settings: Mapping[str, Any]) -> str:
    """Serialize like the ``lab_tracker init`` scaffold (two-space indent, LF)."""

    return json.dumps(settings, indent=2, ensure_ascii=False) + "\n"


def managed_hooks_present(settings: Mapping[str, Any]) -> dict[str, bool]:
    """Which managed hooks a parsed settings object already carries."""

    hooks = settings.get("hooks") if isinstance(settings.get("hooks"), Mapping) else {}
    present: dict[str, bool] = {}
    for managed in MANAGED_HOOKS:
        groups = hooks.get(managed.event) if isinstance(hooks, Mapping) else None
        present[managed.event] = any(
            is_managed_hook(entry, managed)
            for group in (groups if isinstance(groups, list) else [])
            if isinstance(group, Mapping) and isinstance(group.get("hooks"), list)
            for entry in group["hooks"]
        )
    return present


def install_agent_hooks(
    target: str | Path = ".",
    *,
    shared: bool = False,
    uninstall: bool = False,
    dry_run: bool = False,
) -> JsonObject:
    """Add (or with ``uninstall`` remove) the managed hook entries; returns a diff payload.

    The personal ``.claude/settings.local.json`` is the default; ``shared``
    edits the committed ``.claude/settings.json`` instead.
    """

    root = Path(target).expanduser().resolve()
    scope = SCOPE_SHARED if shared else SCOPE_LOCAL
    path = settings_path(root, shared=shared)
    existing_text, settings = load_settings(path)
    proposed = without_managed_hooks(settings) if uninstall else with_managed_hooks(settings)
    proposed_text = render_settings(proposed)
    present = any(managed_hooks_present(settings).values())
    if uninstall:
        action = "absent" if not present else ("would-remove" if dry_run else "removed")
    elif existing_text.strip() and proposed == settings:
        # Semantic comparison: a person's own formatting is never rewritten for nothing.
        action = "current"
    elif present:
        action = "would-update" if dry_run else "updated"
    else:
        action = "would-install" if dry_run else "installed"
    changes = action not in {"absent", "current"}
    payload: JsonObject = {
        "command": "setup-agent-hooks",
        "action": action,
        "settings_path": str(path),
        "scope": scope,
        "dry_run": dry_run,
        "hooks": [managed.describe() for managed in MANAGED_HOOKS],
        "diff": _diff(path, existing_text, proposed_text) if changes else "",
    }
    warnings = [] if uninstall else _install_warnings(root, shared=shared)
    other = _other_scope_note(root, scope, uninstall=uninstall)
    if other:
        warnings.append(other)
    payload["warnings"] = warnings
    if changes and not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(proposed_text, encoding="utf-8", newline="\n")
    return payload


def agent_hooks_status(target: str | Path = ".") -> JsonObject:
    """Read-only: whether the managed hooks are present in either settings file."""

    root = Path(target).expanduser().resolve()
    files: list[JsonObject] = []
    for scope, relative in SCOPE_FILES:
        path = root / relative
        entry: JsonObject = {"scope": scope, "path": str(path), "present": path.exists()}
        if path.exists():
            try:
                _text, parsed = load_settings(path)
            except (LTValidationError, OSError, UnicodeDecodeError) as exc:
                entry["error"] = str(exc)
            else:
                entry["hooks"] = managed_hooks_present(parsed)
        files.append(entry)
    installed = {
        managed.event: any(bool(item.get("hooks", {}).get(managed.event)) for item in files)
        for managed in MANAGED_HOOKS
    }
    return {
        "installed": all(installed.values()),
        "session_end": installed[SESSION_END_HOOK.event],
        "watch_touch": installed[WATCH_TOUCH_HOOK.event],
        "scopes": [str(item["scope"]) for item in files if any((item.get("hooks") or {}).values())],
        "files": files,
    }


def carry_forward_agent_hooks(canonical_text: str, existing_path: Path) -> str:
    """Canonical scaffold settings plus the managed entries a person already opted into.

    Used by ``lab_tracker update`` / ``init --force`` when they rewrite
    ``.claude/settings.json`` so a refresh never silently drops the
    agent-hooks consent. Returns ``canonical_text`` unchanged when the file
    has no managed entry or cannot be read.
    """

    try:
        _text, existing = load_settings(existing_path)
    except (LTValidationError, OSError, UnicodeDecodeError):
        return canonical_text
    if not any(managed_hooks_present(existing).values()):
        return canonical_text
    canonical = json.loads(canonical_text)
    return render_settings(with_managed_hooks(canonical))


def _install_warnings(root: Path, *, shared: bool) -> list[str]:
    warnings: list[str] = [SHARED_SCOPE_WARNING] if shared else []
    if shutil.which("lt") is None:
        warnings.append(
            "The hooks call `lt`, but no `lt` executable is on the current PATH; they "
            "do nothing until the Lab Tracker client's bin directory is on the PATH "
            "your agent uses."
        )
    if find_checkout_watch_config(root) is None:
        warnings.append(
            "No .lab-tracker/watch.json in this checkout: `lt watch touch` has nothing to "
            "capture until `lt watch add <folder>` registers a watch folder."
        )
    if not shared and local_settings_tracked_risk(root):
        warnings.append(
            ".claude/settings.local.json is not ignored by git in this checkout, so a "
            "`git add -A` could commit this personal opt-in for everyone; add "
            "`.claude/settings.local.json` to .gitignore."
        )
    return warnings


def local_settings_tracked_risk(root: Path) -> bool:
    """True when ``root`` is a git checkout that does not ignore the personal settings file.

    One bounded ``git check-ignore`` probe; ``False`` outside a checkout or
    when git cannot answer (nothing to warn about with confidence).
    """

    probe = gitinfo.run_git(root, "check-ignore", "-q", LOCAL_SETTINGS_RELATIVE.as_posix())
    if probe.ok:
        return False
    # Exit 1 (not ignored) has empty stderr; exit 128 (not a repository) says why.
    return not probe.stderr and not probe.timed_out and not probe.unavailable


def _other_scope_note(root: Path, scope: str, *, uninstall: bool) -> str:
    """Name managed entries in the file this run does not edit, so none are forgotten."""

    other_scope, relative = next(item for item in SCOPE_FILES if item[0] != scope)
    other_path = root / relative
    try:
        _text, parsed = load_settings(other_path)
    except (LTValidationError, OSError, UnicodeDecodeError):
        return ""
    if not any(managed_hooks_present(parsed).values()):
        return ""
    flag = " --shared" if other_scope == SCOPE_SHARED else ""
    verb = "still has" if uninstall else "also has"
    return (
        f"{relative.as_posix()} {verb} the agent hooks; "
        f"`lt setup agent-hooks{flag} --uninstall --yes` removes them from it."
    )


def _diff(path: Path, existing: str, proposed: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            existing.splitlines(),
            proposed.splitlines(),
            fromfile=f"{path} (current)",
            tofile=f"{path} (proposed)",
            lineterm="",
        )
    )


__all__ = [
    "MANAGED_HOOKS",
    "SESSION_END_HOOK",
    "WATCH_TOUCH_HOOK",
    "agent_hooks_status",
    "carry_forward_agent_hooks",
    "install_agent_hooks",
    "is_managed_hook",
    "with_managed_hooks",
    "without_managed_hooks",
]
