"""Command-line utilities for Lab Tracker packaging and consumer setup."""

from __future__ import annotations

import argparse
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

from alembic.config import Config

from alembic import command
from lab_tracker.config import get_settings
from lab_tracker.decision_context_constants import (
    CLAUDE_BLOCK_BEGIN,
    CLAUDE_BLOCK_END,
    managed_claude_block,
)


@dataclass
class InitResult:
    created: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    overwritten: list[Path] = field(default_factory=list)

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "created": [str(path) for path in self.created],
            "skipped": [str(path) for path in self.skipped],
            "overwritten": [str(path) for path in self.overwritten],
        }


def init_consumer_repo(
    target: str | Path = ".",
    *,
    project_name: str | None = None,
    force: bool = False,
) -> InitResult:
    root = Path(target).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    result = InitResult()
    files = {
        root / ".mcp.json": _mcp_json(),
        root / ".claude" / "settings.json": _claude_settings_json(),
        root / "scripts" / "lt.py": _lt_shim(),
        root / "AGENTS.lt.md": _agents_fragment(),
        root / "lt_ids.json": _ids_placeholder(project_name),
    }
    for path, content in files.items():
        _write_scaffold_file(path, content, force=force, result=result)
    _write_managed_claude_block(root / "CLAUDE.md", result=result)
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

    args = parser.parse_args(argv)
    if args.command == "init":
        result = init_consumer_repo(
            args.target,
            project_name=args.project_name,
            force=args.force,
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
) -> None:
    if path.exists() and not force:
        result.skipped.append(path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    path.write_text(content, encoding="utf-8")
    if existed:
        result.overwritten.append(path)
    else:
        result.created.append(path)


def _write_managed_claude_block(path: Path, *, result: InitResult) -> None:
    block = managed_claude_block()
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        content = _upsert_managed_block(existing, block)
        if content == existing:
            result.skipped.append(path)
            return
        path.write_text(content, encoding="utf-8")
        result.overwritten.append(path)
        return
    path.write_text(block, encoding="utf-8")
    result.created.append(path)


def _upsert_managed_block(existing: str, block: str) -> str:
    if CLAUDE_BLOCK_BEGIN in existing and CLAUDE_BLOCK_END in existing:
        prefix, rest = existing.split(CLAUDE_BLOCK_BEGIN, 1)
        _old_block, suffix = rest.split(CLAUDE_BLOCK_END, 1)
        return f"{prefix.rstrip()}\n\n{block}{suffix.lstrip()}"
    if existing.strip():
        return f"{existing.rstrip()}\n\n{block}"
    return block


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
