"""Concurrent ``alembic upgrade head`` runs against one Postgres database.

Every container built from the image runs ``alembic upgrade head`` in its
entrypoint, so replicas that start together must not race the same DDL.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, make_url, text

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PY = REPO_ROOT / "src" / "lab_tracker" / "alembic" / "env.py"


def _migration_lock_key() -> int:
    match = re.search(
        r"^_POSTGRES_MIGRATION_LOCK_KEY = (\d+)$",
        ENV_PY.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert match is not None, "env.py must define _POSTGRES_MIGRATION_LOCK_KEY"
    key = int(match.group(1))
    # pg_locks splits a bigint key into classid (high) and objid (low) words.
    assert 0 < key < 2**31
    return key


@pytest.fixture()
def empty_postgres_database_url() -> Iterator[str]:
    base_database_url = os.getenv("LAB_TRACKER_POSTGRES_TEST_DATABASE_URL")
    if not base_database_url:
        pytest.skip("LAB_TRACKER_POSTGRES_TEST_DATABASE_URL is not set")
    base_url = make_url(base_database_url.strip())
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    database_name = f"lab_tracker_test_{uuid4().hex}"
    admin_url = base_url.set(database=base_url.database or "postgres")
    admin_engine = create_engine(admin_url, future=True, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    try:
        yield base_url.set(database=database_name).render_as_string(hide_password=False)
    finally:
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'))
        admin_engine.dispose()


def _upgrade_process(database_url: str, tmp_path: Path) -> subprocess.Popen[str]:
    environment = {
        **os.environ,
        "LAB_TRACKER_DATABASE_URL": database_url,
        "LAB_TRACKER_ENVIRONMENT": "local",
        "LAB_TRACKER_FILE_STORAGE_PATH": str(tmp_path / "file-storage"),
        "LAB_TRACKER_NOTE_STORAGE_PATH": str(tmp_path / "note-storage"),
        "LAB_TRACKER_AUTH_SECRET_KEY": "test-secret",
    }
    return subprocess.Popen(
        [sys.executable, "-m", "alembic", "-c", str(REPO_ROOT / "alembic.ini"), "upgrade", "head"],
        cwd=REPO_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


@pytest.mark.postgres
def test_postgres_upgrade_waits_for_the_migration_advisory_lock(
    empty_postgres_database_url: str, tmp_path: Path
) -> None:
    key = _migration_lock_key()
    engine = create_engine(empty_postgres_database_url, future=True)
    try:
        with engine.connect() as holder:
            holder.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
            holder.commit()
            upgrade = _upgrade_process(empty_postgres_database_url, tmp_path)
            try:
                deadline = time.monotonic() + 60
                waiting = 0
                with engine.connect() as observer:
                    while waiting == 0 and time.monotonic() < deadline:
                        assert upgrade.poll() is None, upgrade.communicate()[0]
                        waiting = observer.execute(
                            text(
                                "SELECT count(*) FROM pg_locks "
                                "WHERE locktype = 'advisory' AND NOT granted "
                                "AND classid = 0 AND objid = :key "
                                "AND database = (SELECT oid FROM pg_database "
                                "WHERE datname = current_database())"
                            ),
                            {"key": key},
                        ).scalar_one()
                        observer.rollback()
                        if waiting == 0:
                            time.sleep(0.1)
                    assert waiting == 1, "alembic upgrade never waited for the lock"
                    assert "alembic_version" not in inspect(observer).get_table_names()
            finally:
                holder.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                holder.commit()
                output, _ = upgrade.communicate(timeout=300)
        assert upgrade.returncode == 0, output
        with engine.connect() as connection:
            heads = connection.execute(text("SELECT version_num FROM alembic_version")).all()
        assert len(heads) == 1
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_concurrent_postgres_upgrades_both_succeed(
    empty_postgres_database_url: str, tmp_path: Path
) -> None:
    upgrades = [
        _upgrade_process(empty_postgres_database_url, tmp_path / str(index))
        for index in range(2)
    ]
    outputs = [upgrade.communicate(timeout=300)[0] for upgrade in upgrades]

    for upgrade, output in zip(upgrades, outputs, strict=True):
        assert upgrade.returncode == 0, output
