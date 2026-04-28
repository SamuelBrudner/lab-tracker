"""Export Lab Tracker records into a local Dolt mirror."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

from sqlalchemy import Table, select

from lab_tracker.db import Base, get_engine

DEFAULT_MIRROR_PATH = ".lab-tracker-dolt"
DEFAULT_EXPORT_DIR = "_export"
EXCLUDED_TABLES = frozenset({"users"})


class DoltMirrorError(RuntimeError):
    """Raised when the Dolt mirror export cannot complete."""


@dataclass(frozen=True)
class TableExport:
    name: str
    csv_path: Path
    primary_keys: tuple[str, ...]
    row_count: int


@dataclass(frozen=True)
class ExportResult:
    mirror_path: Path
    exports: tuple[TableExport, ...]
    commit_created: bool


class DoltRunner:
    def __init__(self, dolt_bin: str, cwd: Path) -> None:
        self._dolt_bin = dolt_bin
        self._cwd = cwd

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self._dolt_bin, *args],
                cwd=self._cwd,
                check=check,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise DoltMirrorError(
                f"Dolt executable not found: {self._dolt_bin}. Set LAB_TRACKER_DOLT_BIN "
                "to the full path of the dolt executable."
            ) from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            command = " ".join([self._dolt_bin, *args])
            raise DoltMirrorError(f"Dolt command failed: {command}\n{detail}") from exc


def retained_tables() -> list[Table]:
    tables = [
        table
        for table in Base.metadata.sorted_tables
        if table.name not in EXCLUDED_TABLES and list(table.primary_key.columns)
    ]
    return sorted(tables, key=lambda table: table.name)


def export_tables(export_dir: Path) -> tuple[TableExport, ...]:
    export_dir.mkdir(parents=True, exist_ok=True)
    engine = get_engine()
    exports: list[TableExport] = []
    with engine.connect() as connection:
        for table in retained_tables():
            primary_keys = tuple(column.name for column in table.primary_key.columns)
            csv_path = export_dir / f"{table.name}.csv"
            order_by = [table.c[name] for name in primary_keys]
            rows = connection.execute(select(table).order_by(*order_by)).mappings()
            row_count = _write_csv(table, rows, csv_path)
            exports.append(
                TableExport(
                    name=table.name,
                    csv_path=csv_path,
                    primary_keys=primary_keys,
                    row_count=row_count,
                )
            )
    engine.dispose()
    return tuple(exports)


def export_to_dolt(
    *,
    mirror_path: Path,
    message: str,
    dolt_bin: str,
) -> ExportResult:
    mirror_path.mkdir(parents=True, exist_ok=True)
    runner = DoltRunner(dolt_bin=dolt_bin, cwd=mirror_path)
    if not (mirror_path / ".dolt").exists():
        runner.run("init")

    export_dir = mirror_path / DEFAULT_EXPORT_DIR
    if export_dir.exists():
        shutil.rmtree(export_dir)
    exports = export_tables(export_dir)
    existing_tables = _dolt_tables(runner)

    for table_export in exports:
        relative_csv = table_export.csv_path.relative_to(mirror_path)
        if table_export.name in existing_tables:
            runner.run("table", "import", "-r", table_export.name, str(relative_csv))
        else:
            runner.run(
                "table",
                "import",
                "-c",
                f"--pk={','.join(table_export.primary_keys)}",
                table_export.name,
                str(relative_csv),
            )

    runner.run("add", ".")
    status = runner.run("status", "--porcelain", check=False)
    if not status.stdout.strip():
        return ExportResult(mirror_path=mirror_path, exports=exports, commit_created=False)
    runner.run("commit", "-m", message)
    return ExportResult(mirror_path=mirror_path, exports=exports, commit_created=True)


def _write_csv(table: Table, rows: Any, csv_path: Path) -> int:
    fieldnames = [column.name for column in table.columns]
    count = 0
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _serialize(row[field]) for field in fieldnames})
            count += 1
    return count


def _serialize(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return str(value)


def _dolt_tables(runner: DoltRunner) -> set[str]:
    result = runner.run("table", "ls", check=False)
    if result.returncode != 0:
        return set()
    return {
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() and not line.lower().startswith("tables")
    }


def _default_mirror_path() -> Path:
    configured = os.getenv("LAB_TRACKER_DOLT_MIRROR_PATH", DEFAULT_MIRROR_PATH)
    return Path(configured)


def _default_dolt_bin() -> str:
    return os.getenv("LAB_TRACKER_DOLT_BIN", "dolt")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export Lab Tracker data to a Dolt mirror.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    export_parser = subparsers.add_parser("export", help="Export the live database to Dolt.")
    export_parser.add_argument(
        "--message",
        default="Export Lab Tracker snapshot",
        help="Dolt commit message for changed exports.",
    )
    export_parser.add_argument(
        "--mirror-path",
        type=Path,
        default=_default_mirror_path(),
        help="Local Dolt mirror directory.",
    )
    export_parser.add_argument(
        "--dolt-bin",
        default=_default_dolt_bin(),
        help="Path to the dolt executable.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "export":
        try:
            result = export_to_dolt(
                mirror_path=args.mirror_path,
                message=args.message,
                dolt_bin=args.dolt_bin,
            )
        except DoltMirrorError as exc:
            print(str(exc), file=os.sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "mirror_path": str(result.mirror_path),
                    "tables": [
                        {
                            "name": export.name,
                            "rows": export.row_count,
                            "primary_keys": list(export.primary_keys),
                        }
                        for export in result.exports
                    ],
                    "commit_created": result.commit_created,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
