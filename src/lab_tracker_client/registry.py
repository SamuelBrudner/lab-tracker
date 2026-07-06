"""Client-side registry of Lab Tracker-enrolled repos on this machine.

Answers "which repos did lab-tracker touch here" so `lt doctor --all` can
sweep them after a package upgrade. Pure metadata for suggestions — nothing
reads it to auto-apply anything, and recording is fail-soft so it can never
break the enrollment command that triggered it.
"""

from __future__ import annotations

import json
import os
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]

REGISTRY_VERSION = 1


def registry_path() -> Path:
    base = os.getenv("LAB_TRACKER_CONFIG_DIR")
    root = Path(base).expanduser() if base else Path.home() / ".lab-tracker"
    return root / "applied-repos.json"


def record_repo(root: str | Path, action: str) -> None:
    """Fail-soft upsert of a repo root with the enrollment action."""

    with suppress(Exception):
        resolved = str(Path(root).expanduser().resolve())
        payload = _load()
        repos = payload["repos"]
        entry = next((item for item in repos if item.get("root") == resolved), None)
        if entry is None:
            entry = {"root": resolved, "actions": []}
            repos.append(entry)
        if action not in entry["actions"]:
            entry["actions"].append(action)
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        path = registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        tmp_path.replace(path)


def list_repos() -> list[JsonObject]:
    return list(_load()["repos"])


def remove_repo(root: str | Path) -> bool:
    """Drop a registry entry; True when something was removed."""

    with suppress(Exception):
        resolved = str(Path(root).expanduser().resolve())
        payload = _load()
        kept = [item for item in payload["repos"] if item.get("root") != resolved]
        if len(kept) == len(payload["repos"]):
            return False
        payload["repos"] = kept
        path = registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        tmp_path.replace(path)
        return True
    return False


def repo_root_for_config(config_path: str | Path) -> Path:
    """The repo a watch config belongs to.

    Only the canonical ``<repo>/.lab-tracker/watch.json`` layout implies a
    two-level parent; a custom --config location records its own directory
    rather than an unrelated grandparent.
    """

    resolved = Path(config_path).expanduser().resolve()
    if resolved.parent.name == ".lab-tracker":
        return resolved.parent.parent
    return resolved.parent


def _load() -> JsonObject:
    with suppress(Exception):
        payload = json.loads(registry_path().read_text(encoding="utf-8"))
        if (
            isinstance(payload, dict)
            and int(payload.get("version") or 0) == REGISTRY_VERSION
            and isinstance(payload.get("repos"), list)
        ):
            payload["repos"] = [
                item for item in payload["repos"] if isinstance(item, dict)
            ]
            return payload
    return {"version": REGISTRY_VERSION, "repos": []}
