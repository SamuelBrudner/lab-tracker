"""Behavioral checks for complete skill installation and progressive disclosure."""

from __future__ import annotations

import re
import shutil

from lab_tracker.cli import init_consumer_repo, refresh_setup_skills
from lab_tracker.skill_bundle import skill_resources
from lab_tracker_client.setup import _skills_status
from scripts.generate_lab_tracker_skill_reference import main as generate


def test_installed_research_skill_references_are_resolvable(tmp_path, monkeypatch):
    home = tmp_path / "skills"
    monkeypatch.setenv("LAB_TRACKER_SKILLS_HOME", str(home))
    result = refresh_setup_skills()
    assert set(result.created) == {home / p for p in skill_resources()}
    entry = home / "lab-tracker" / "SKILL.md"
    links = re.findall(r"\]\((references/[^)]+)\)", entry.read_text())
    assert links
    for link in links:
        assert (entry.parent / link).is_file()
    assert _skills_status()["all_resources_up_to_date"] is True


def test_reference_drift_is_reported_and_refresh_preserves_customization(tmp_path, monkeypatch):
    home = tmp_path / "skills"
    monkeypatch.setenv("LAB_TRACKER_SKILLS_HOME", str(home))
    refresh_setup_skills()
    changed = home / "lab-tracker/references/evidence.md"
    removed = home / "lab-tracker/references/api.md"
    changed.write_text("custom evidence instructions")
    removed.unlink()
    status = _skills_status()
    assert status["all_resources_up_to_date"] is False
    assert status["bundles"][0]["missing_files"] == ["lab-tracker/references/api.md"]
    assert status["bundles"][0]["stale_files"] == ["lab-tracker/references/evidence.md"]
    preview = refresh_setup_skills(dry_run=True)
    assert changed in preview.overwritten and removed in preview.created
    assert changed.read_text() == "custom evidence instructions"
    assert not removed.exists()
    refresh_setup_skills()
    assert changed.with_name("evidence.md.bak-lt-update").read_text() == (
        "custom evidence instructions"
    )
    assert _skills_status()["all_resources_up_to_date"] is True


def test_uninstall_removes_managed_references_and_preserves_user_files(tmp_path, monkeypatch):
    home = tmp_path / "skills"
    monkeypatch.setenv("LAB_TRACKER_SKILLS_HOME", str(home))
    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(tmp_path / "config"))
    refresh_setup_skills()
    user_file = home / "lab-tracker/references/custom.md"
    user_file.write_text("user resource")
    init_consumer_repo(tmp_path / "repo", uninstall=True, install_skills=True)
    assert user_file.read_text() == "user resource"
    assert all(not (home / p).exists() for p in skill_resources())
    assert not (home / "lab-tracker-setup").exists()


def test_generator_check_covers_source_and_packaged_reference_drift(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    root = tmp_path / "skills"
    shutil.copytree("skills", root)
    package = tmp_path / "packaged"
    args = ["--skill-path", str(root / "lab-tracker/SKILL.md"), "--package-dir", str(package)]
    assert generate(args) == 0
    assert generate([*args, "--check"]) == 0
    obsolete = package / "lab-tracker/references/obsolete.md"
    obsolete.write_text("removed from the source skill")
    assert generate([*args, "--check"]) == 1
    assert generate(args) == 0
    assert not obsolete.exists()
    assert generate([*args, "--check"]) == 0
    source = root / "lab-tracker/references/api.md"
    source.write_text(source.read_text().replace("`text` (required)", "`text` (optional)"))
    assert generate([*args, "--check"]) == 1
    assert generate(args) == 0
    copied = package / "lab-tracker/references/evidence.md"
    copied.write_text("stale packaged resource")
    assert generate([*args, "--check"]) == 1
    assert generate(args) == 0
    assert generate([*args, "--check"]) == 0
