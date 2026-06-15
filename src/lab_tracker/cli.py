"""Command-line utilities for Lab Tracker packaging and consumer setup."""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from textwrap import dedent

from alembic.config import Config

from alembic import command


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
        root / "scripts" / "lt.py": _lt_shim(),
        root / "AGENTS.lt.md": _agents_fragment(),
        root / "lt_ids.json": _ids_placeholder(project_name),
    }
    for path, content in files.items():
        _write_scaffold_file(path, content, force=force, result=result)
    return result


def serve_app(
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    reload: bool = False,
    run_migrations: bool = True,
    open_browser: bool = True,
    server_runner: Callable[..., None] | None = None,
    browser_opener: Callable[[str], object] | None = None,
) -> None:
    if run_migrations:
        alembic_config = Config(str(_repo_root() / "alembic.ini"))
        command.upgrade(alembic_config, "head")

    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{browser_host}:{port}/app"
    if open_browser:
        opener = browser_opener or webbrowser.open
        opener(url)

    if server_runner is None:
        import uvicorn

        server_runner = uvicorn.run
    server_runner("lab_tracker.asgi:app", host=host, port=port, reload=reload)


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
            )
        except Exception as exc:
            print(f"Failed to start Lab Tracker: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc


def _repo_root() -> Path:
    cwd = Path.cwd()
    if (cwd / "alembic.ini").exists():
        return cwd
    for parent in Path(__file__).resolve().parents:
        if (parent / "alembic.ini").exists():
            return parent
    return cwd


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

        Notes are idempotent by the first non-blank line of the note body. Treat that
        first line as a stable marker; change it intentionally when you mean to create
        a new Lab Tracker note.

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
