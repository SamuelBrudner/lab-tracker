"""Complete, package-pinned skill resources for consumer installations."""

from __future__ import annotations

from importlib.resources import files

from lab_tracker.setup_guide import setup_skill_markdown


def skill_resources() -> dict[str, str]:
    """Map skill-relative paths to UTF-8 text, including supporting references."""
    root = files("lab_tracker").joinpath("skill_data", "lab-tracker")
    resources = {
        "lab-tracker-setup/SKILL.md": setup_skill_markdown(),
        "lab-tracker/SKILL.md": root.joinpath("SKILL.md").read_text(encoding="utf-8"),
    }
    for path in sorted(root.joinpath("references").iterdir(), key=lambda p: p.name):
        if path.is_file() and path.name.endswith(".md"):
            resources[f"lab-tracker/references/{path.name}"] = path.read_text(encoding="utf-8")
    return resources
