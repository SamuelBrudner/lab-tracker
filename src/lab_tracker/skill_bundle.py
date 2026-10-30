"""Complete, package-pinned skill resources for consumer installations."""

from __future__ import annotations

from importlib.resources import files
from importlib.resources.abc import Traversable

from lab_tracker.setup_guide import setup_skill_markdown

SETUP_SKILL = "lab-tracker-setup"


def skill_resources() -> dict[str, str]:
    """Map skill-relative paths to UTF-8 text, including supporting references.

    Every skill tree packaged under ``skill_data`` ships; the setup skill is
    rendered from the setup guide rather than read from its packaged copy.
    """
    resources = {f"{SETUP_SKILL}/SKILL.md": setup_skill_markdown()}
    root = files("lab_tracker").joinpath("skill_data")
    for skill in sorted(root.iterdir(), key=lambda p: p.name):
        if skill.is_dir() and skill.name != SETUP_SKILL:
            resources.update(_skill_tree(skill))
    return resources


def _skill_tree(skill: Traversable) -> dict[str, str]:
    """Return one skill's ``SKILL.md`` and ``references/*.md`` keyed by relative path."""
    resources = {f"{skill.name}/SKILL.md": skill.joinpath("SKILL.md").read_text(encoding="utf-8")}
    references = skill.joinpath("references")
    if references.is_dir():
        for path in sorted(references.iterdir(), key=lambda p: p.name):
            if path.is_file() and path.name.endswith(".md"):
                resources[f"{skill.name}/references/{path.name}"] = path.read_text(
                    encoding="utf-8"
                )
    return resources
