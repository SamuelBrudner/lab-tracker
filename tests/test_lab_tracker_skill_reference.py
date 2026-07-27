from __future__ import annotations

from pathlib import Path

from scripts.generate_lab_tracker_skill_reference import (
    build_openapi_schema,
    generate_reference,
)
from scripts.generate_lab_tracker_skill_reference import (
    main as generate_skill_reference,
)


def test_repo_owned_lab_tracker_skill_has_current_generated_reference(monkeypatch) -> None:
    monkeypatch.setenv("LAB_TRACKER_AUTH_ENABLED", "true")
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY", "test-skill-reference-secret")
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("LAB_TRACKER_FILE_STORAGE_PATH", ".test-file-storage")
    monkeypatch.setenv("LAB_TRACKER_NOTE_STORAGE_PATH", ".test-note-storage")
    skill_path = Path("skills/lab-tracker/SKILL.md")

    current = skill_path.read_text(encoding="utf-8")
    expected = generate_reference(build_openapi_schema())

    assert expected in current
    assert "title" not in _question_create_block(expected)
    assert "`text` (required)" in _question_create_block(expected)
    assert "descriptive, hypothesis_driven, method_dev, other" in expected
    assert "limit` (optional): integer; default 50; maximum 200" in expected
    data_store_block = _data_store_create_block(expected)
    assert "provide exactly one of `project_id` or `group_id`" in data_store_block
    assert "provide `authority_grant_id`" in data_store_block
    assert "structurally optional only" in data_store_block
    assert "`object_table` and `database` appear" in data_store_block
    assert "registration rejects them" in data_store_block


def test_skill_reference_check_mode_passes(monkeypatch) -> None:
    monkeypatch.setenv("LAB_TRACKER_AUTH_ENABLED", "true")
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY", "test-skill-reference-secret")
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", "sqlite+pysqlite:///:memory:")

    assert generate_skill_reference(["--check"]) == 0


def _question_create_block(reference: str) -> str:
    start = reference.index("#### Questions")
    end = reference.index("#### Notes")
    return reference[start:end]


def _data_store_create_block(reference: str) -> str:
    start = reference.index("#### Data Stores")
    end = reference.index("#### Analyses")
    return reference[start:end]
