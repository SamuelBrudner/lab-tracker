"""Every test that needs the Postgres fixtures must carry ``@pytest.mark.postgres``.

The default CI job has no ``LAB_TRACKER_POSTGRES_TEST_DATABASE_URL``, so the
Postgres fixtures skip there, and the dedicated Postgres job runs
``pytest -m postgres``, which deselects unmarked tests. An unmarked Postgres
test therefore never runs anywhere. Collection resolves the full fixture
closure, so indirect users of the Postgres fixtures are caught too.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

_AUDIT_PLUGIN = '''
import json

POSTGRES_FIXTURES = {
    "migrated_postgres_database_url",
    "postgres_app",
    "postgres_client",
    "postgres_admin_auth_headers",
}


def pytest_collection_finish(session):
    unmarked = sorted(
        item.nodeid
        for item in session.items
        if POSTGRES_FIXTURES & set(getattr(item, "fixturenames", ()))
        and item.get_closest_marker("postgres") is None
    )
    print("POSTGRES-MARKER-AUDIT " + json.dumps(unmarked))
'''


def test_every_postgres_fixture_user_is_marked_postgres(tmp_path: Path) -> None:
    (tmp_path / "postgres_marker_audit.py").write_text(_AUDIT_PLUGIN)
    python_path = [str(tmp_path), os.environ.get("PYTHONPATH", "")]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, python_path))}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-n0",
            "--no-cov",
            "-p",
            "no:cacheprovider",
            "-p",
            "postgres_marker_audit",
            str(TESTS_DIR),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=TESTS_DIR.parent,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
    audit_lines = [
        line for line in result.stdout.splitlines() if line.startswith("POSTGRES-MARKER-AUDIT ")
    ]
    assert len(audit_lines) == 1, result.stdout[-4000:]
    unmarked = json.loads(audit_lines[0].removeprefix("POSTGRES-MARKER-AUDIT "))
    assert unmarked == [], "Add @pytest.mark.postgres to: " + ", ".join(unmarked)
