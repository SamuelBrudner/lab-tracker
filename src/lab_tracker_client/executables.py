"""Resolve the ``lt``/``lt-mcp`` console scripts and classify how they are installed.

Read-only and dependency-free (stdlib only): safe to import from the read-only
``setup_status`` inventory and the ``lt doctor`` command, both of which run
unprompted for agents and session hooks. Nothing here writes, prompts, or does
network I/O, and no function raises — callers get a populated-but-degraded
record instead.

The one load-bearing subtlety is the *symlink/realpath trap*. A ``uv tool``
install places a launcher (a symlink on POSIX) at ``~/.local/bin/lt`` that
points into ``~/.local/share/uv/tools/<pkg>/bin/lt``, whose grandparent is
itself a virtualenv (it has a ``pyvenv.cfg``). Classifying the *resolved* path
would therefore mislabel a stable global tool as a fragile per-project venv. So
``classify_install`` always runs on the launcher's own (un-resolved) location
and keeps the realpath purely informational.

Resolution order mirrors ``hooks._default_lt_path``: prefer the executable next
to the running interpreter (guaranteed the same environment that provides the
capture code), then fall back to ``PATH``.
"""

from __future__ import annotations

import os
import shutil
import sys
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path

JsonObject = dict[str, object]

_EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""

# POSIX system bin dirs, used only to phrase a nicer detail string; anything
# unrecognised still defaults to "stable" so an unfamiliar-but-working install
# is never nagged about.
_SYSTEM_BIN_DIRS = (
    "/usr/bin",
    "/usr/local/bin",
    "/bin",
    "/opt/homebrew/bin",
    "/opt/local/bin",
)

_INSTALL_HINT = (
    "install it as a uv tool (`uv tool install -e .` from a lab-tracker checkout, "
    'or `uv tool install "git+https://github.com/SamuelBrudner/lab-tracker"`)'
)


@dataclass(frozen=True)
class ResolvedExecutable:
    """How one console script (``lt`` or ``lt-mcp``) resolves on this machine."""

    name: str
    path: str | None  # chosen location (invoking env preferred), None if unresolved
    source: str  # "sibling" | "path" | "not-found"
    on_path: str | None  # shutil.which(name): what PATH consumers (editors, hooks) get
    matches_path: bool | None  # chosen and on_path are the same file
    real_path: str | None  # os.path.realpath(path); informational only
    install_kind: str  # global-tool | system | project-venv | venv | unknown | not-found
    stable: bool  # survives a project .venv rebuild
    detail: str

    def as_dict(self) -> JsonObject:
        return asdict(self)


def _norm(path: object) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _same_file(a: object, b: object) -> bool:
    with suppress(OSError):
        return Path(a).samefile(Path(b))
    return _norm(a) == _norm(b)


def _has_consecutive(parts: tuple[str, ...], first: str, second: str) -> bool:
    lowered = [part.lower() for part in parts]
    return any(
        lowered[i] == first and lowered[i + 1] == second
        for i in range(len(lowered) - 1)
    )


def _home_local_bin() -> Path | None:
    # Path.home() raises RuntimeError when the home dir is undeterminable
    # (service account, minimal container, cron unit with no HOME/USERPROFILE).
    # Swallow it so classification degrades to the "unknown" fallback rather
    # than breaking the module's never-raises contract.
    with suppress(Exception):
        return Path.home() / ".local" / "bin"
    return None


def classify_install(path: object) -> tuple[str, bool, str]:
    """Return ``(install_kind, stable, detail)`` for an *un-resolved* path.

    See the module docstring on why the path must not be ``realpath``-ed first.
    """

    p = Path(path)
    parent = p.parent
    parts_lower = [part.lower() for part in p.parts]

    # 1) Stable global tool stores. Checked BEFORE any pyvenv.cfg probe because a
    #    uv/pipx tool environment is itself a venv and would otherwise look
    #    fragile.
    home_local_bin = _home_local_bin()
    if home_local_bin is not None and _norm(parent) == _norm(home_local_bin):
        return "global-tool", True, f"Installed in {parent} (uv tool / pipx bin dir)."
    if _has_consecutive(p.parts, "uv", "tools") or _has_consecutive(
        p.parts, "pipx", "venvs"
    ):
        return "global-tool", True, "Installed in a uv/pipx tool environment."

    # 2) Fragile per-project virtualenv.
    if ".venv" in parts_lower:
        return (
            "project-venv",
            False,
            "Resolves from a project .venv; recreating that venv breaks it.",
        )
    with suppress(OSError):
        if (parent.parent / "pyvenv.cfg").exists():
            return (
                "venv",
                False,
                "Resolves from a virtualenv; recreating it breaks the commit hook "
                "and MCP configs.",
            )

    # 3) System locations are stable.
    if sys.platform != "win32" and _norm(parent) in {_norm(d) for d in _SYSTEM_BIN_DIRS}:
        return "system", True, f"Installed in a system location ({parent})."

    # 4) Unknown: assume stable so an unfamiliar-but-working install is not nagged.
    return "unknown", True, f"Resolved at {p}."


def resolve_executable(name: str, *, prefer_sibling: bool = True) -> ResolvedExecutable:
    """Find *name* the way capture does — invoking env first, then PATH — and classify it.

    Never raises. ``prefer_sibling`` is disabled automatically for a frozen host
    (PyInstaller etc.), where ``sys.executable`` is the frozen app rather than a
    Python ``bin`` and the sibling probe would be meaningless.
    """

    exe_name = name + _EXE_SUFFIX
    frozen = bool(getattr(sys, "frozen", False))

    sibling: Path | None = None
    if prefer_sibling and not frozen:
        candidate = Path(sys.executable).parent / exe_name
        with suppress(OSError):
            if candidate.exists():
                sibling = candidate

    which = shutil.which(name)
    on_path = Path(which) if which else None

    if sibling is not None:
        chosen: Path | None = sibling
        source = "sibling"
    elif on_path is not None:
        chosen = on_path
        source = "path"
    else:
        chosen = None
        source = "not-found"

    matches_path: bool | None = None
    if chosen is not None and on_path is not None:
        matches_path = _same_file(chosen, on_path)

    real_path: str | None = None
    if chosen is not None:
        with suppress(OSError):
            real_path = str(Path(os.path.realpath(chosen)))

    if chosen is None:
        install_kind, stable, detail = "not-found", False, f"{name} is not on PATH."
    else:
        install_kind, stable, detail = classify_install(chosen)

    return ResolvedExecutable(
        name=name,
        path=str(chosen) if chosen is not None else None,
        source=source,
        on_path=str(on_path) if on_path is not None else None,
        matches_path=matches_path,
        real_path=real_path,
        install_kind=install_kind,
        stable=stable,
        detail=detail,
    )


def installation_report() -> JsonObject:
    """Structured, read-only view of the lt/lt-mcp install plus advisory strings.

    ``problems`` are full non-imperative suggestion sentences (they name what a
    command does); the caller appends them to the setup-status suggestions so
    they reach ``--brief`` / the SessionStart hook. Everything is best-effort and
    never raises.
    """

    lt = resolve_executable("lt")
    lt_mcp = resolve_executable("lt-mcp")
    problems: list[str] = []

    if lt.on_path is None:
        problems.append(
            f"`lt` is not on PATH; {_INSTALL_HINT} so capture works from any repo "
            "and shell."
        )
    if lt_mcp.on_path is None:
        if lt.on_path is not None:
            problems.append(
                "`lt-mcp` is not on PATH even though `lt` is; MCP client configs "
                "(.mcp.json, .cursor/mcp.json) invoke `lt-mcp`, so the MCP server "
                "will not launch. `uv tool install` provides both console scripts."
            )
        else:
            problems.append(
                "`lt-mcp` is not on PATH; MCP clients cannot launch the Lab Tracker "
                "server. `uv tool install` provides it."
            )

    # Fragile-install advisory: one line even if both scripts are fragile.
    fragile = next(
        (exe for exe in (lt, lt_mcp) if exe.path is not None and not exe.stable),
        None,
    )
    if fragile is not None:
        problems.append(
            f"`{fragile.name}` resolves from a {fragile.install_kind}: "
            f"{fragile.detail} A `uv tool install` puts it in a stable ~/.local/bin "
            "so the commit hook and MCP configs survive a .venv rebuild."
        )

    # Invoking copy differs from the PATH copy in a way that matters (one stable,
    # one fragile). Same-stability differences — e.g. a uv-tool launcher shim vs
    # its tool-env executable on Windows — are normal and not flagged. Note the
    # managed `lt hooks install` bakes the copy you ran (the sibling), while
    # editors and MCP configs resolve bare `lt`/`lt-mcp` from PATH — so the two
    # can diverge, which is exactly the confusion worth surfacing.
    if lt.path is not None and lt.on_path is not None and lt.matches_path is False:
        _, on_path_stable, _ = classify_install(lt.on_path)
        if on_path_stable != lt.stable:
            problems.append(
                f"The `lt` you ran ({lt.path}) differs from the `lt` on PATH "
                f"({lt.on_path}): `lt hooks install` bakes the copy you ran, while "
                "editors and MCP configs resolve bare `lt`/`lt-mcp` from PATH. A "
                "`uv tool install` puts one stable copy on PATH so they can't diverge."
            )

    return {
        "lt": lt.as_dict(),
        "lt_mcp": lt_mcp.as_dict(),
        "python": sys.executable,
        "frozen": bool(getattr(sys, "frozen", False)),
        "ok": not problems,
        "problems": problems,
    }
