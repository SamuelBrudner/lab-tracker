"""Repository hygiene: ignore rules and agent instructions stay consistent."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# L3: review reports under docs/runs/ are tracked, so new ones must be addable.
@pytest.mark.parametrize(
    "report",
    ["docs/runs/deep-review-2099-01-01.md", "docs/runs/re-review-2099-01-01.md"],
)
def test_new_review_reports_are_not_git_ignored(report: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "check-ignore", "--no-index", "-v", report],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, (
        f"{report} is git-ignored, so `git add` silently skips new review "
        f"reports: {result.stdout.strip()}"
    )


def test_generated_timestamped_run_logs_stay_git_ignored() -> None:
    # External runners drop timestamped "Run Report" logs in docs/runs/; only
    # those, not the review reports, should stay out of `git status`.
    result = subprocess.run(
        [
            "git",
            "-C",
            str(_REPO_ROOT),
            "check-ignore",
            "--no-index",
            "docs/runs/20990101-000000-00000000.md",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, "timestamped run logs in docs/runs/ are not git-ignored"


# L4: AGENTS.md must not contradict its own profile-based git policy.
def test_agents_md_leaves_push_policy_to_the_session_completion_profiles() -> None:
    text = _read(_REPO_ROOT / "AGENTS.md")
    preamble = text.split("<!-- BEGIN BEADS INTEGRATION", 1)[0]
    assert "git push" not in preamble, (
        "AGENTS.md instructions outside the Beads block mandate `git push`; the "
        "Agent Context Profiles make pushing opt-in (conservative by default)."
    )
    assert "Agent Context Profiles" in text


def test_agents_md_links_only_existing_docs() -> None:
    # Deliberately scans the bd-managed Beads block too: agents follow links
    # there as much as anywhere. If `bd setup` regenerates the block with its
    # upstream docs/QUICKSTART.md link, this fails on purpose; replace the link
    # with `bd prime` again rather than narrowing the scan.
    text = _read(_REPO_ROOT / "AGENTS.md")
    referenced = set(re.findall(r"(?<![\w/.-])docs/[\w./-]+\.md\b", text))
    missing = sorted(path for path in referenced if not (_REPO_ROOT / path).is_file())
    assert not missing, f"AGENTS.md links nonexistent docs: {missing}"
