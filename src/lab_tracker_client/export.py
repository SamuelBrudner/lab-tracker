"""Co-located provenance export for the Lab Tracker consumer client.

The durable artifact in a lab is the data file (``.nwb``), which long outlives
any running Lab Tracker instance. This module writes the *reasoning* — the
PROV-O/JSON-LD provenance of each committed record — to readable sidecar files
that ride next to the data, so the "why" survives without the service.

The server stores only logical file paths, not absolute locations; the consumer
machine is the one that knows where the data lives. So co-location happens here,
on the client, optionally resolving a dataset's logical file paths under a
caller-supplied ``data_root``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lab_tracker_client.client import LabTracker, LTRecord

# Entity kinds exported, as (singular tag, plural route segment).
_EXPORTED_KINDS = (
    ("dataset", "datasets"),
    ("analysis", "analyses"),
    ("claim", "claims"),
)


@dataclass
class ExportResult:
    """Summary of a provenance export, suitable for JSON output."""

    out_dir: str
    files: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    identifier_root: str | None = None
    identifier_note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "out_dir": self.out_dir,
            "counts": self.counts,
            "files": self.files,
        }
        if self.identifier_root is not None:
            result["identifier_root"] = self.identifier_root
        if self.identifier_note is not None:
            result["identifier_note"] = self.identifier_note
        return result


def _sidecar_name(kind: str, entity_id: str) -> str:
    return f"{kind}-{entity_id}.prov.jsonld"


def _write_jsonld(path: Path, document: Any) -> None:
    path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")


def _identifier_root(document: Any, route: str, entity_id: str) -> str | None:
    """Base URL the document's ``@id`` identifiers are rooted at, if findable."""
    graph = document.get("@graph") if isinstance(document, dict) else None
    if not isinstance(graph, list):
        return None
    suffix = f"/{route}/{entity_id}"
    for node in graph:
        if isinstance(node, dict):
            node_id = node.get("@id")
            if isinstance(node_id, str) and node_id.endswith(suffix):
                return node_id.removesuffix(suffix)
    return None


def _dataset_file_paths(dataset: LTRecord) -> list[str]:
    manifest = dataset.get("commit_manifest") or {}
    files = manifest.get("files") or []
    paths: list[str] = []
    for entry in files:
        path = entry.get("path") if isinstance(entry, dict) else None
        if path:
            paths.append(str(path))
    return paths


def export_project_provenance(
    client: LabTracker,
    *,
    project_id: str,
    out_dir: str = "lab-tracker-export",
    since: str | None = None,
    until: str | None = None,
    data_root: str | None = None,
) -> ExportResult:
    """Write provenance sidecars for a project's committed records.

    Datasets and claims are exported in full; analyses are windowed by
    ``since``/``until`` when given (the "advances since last July" surface).
    When ``data_root`` is set, each dataset's sidecar is *also* written next to
    its resolved data file(s), so the reasoning lands beside the data.
    """

    out = Path(out_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    result = ExportResult(out_dir=str(out))

    records: dict[str, list[LTRecord]] = {
        "dataset": client.list_datasets(project_id=project_id, status="committed"),
        "analysis": client.list_analyses(
            project_id=project_id,
            status="committed",
            since=since,
            until=until,
        ),
        "claim": client.list_claims(project_id=project_id, status="supported"),
    }

    root = Path(data_root).expanduser() if data_root else None
    for kind, route in _EXPORTED_KINDS:
        items = records.get(kind, [])
        result.counts[kind] = len(items)
        for record in items:
            entity_id = str(record.id)
            document = client.provenance(route, entity_id)
            if result.identifier_root is None:
                result.identifier_root = _identifier_root(document, route, entity_id)
            sidecar = out / _sidecar_name(kind, entity_id)
            _write_jsonld(sidecar, document)
            result.files.append(str(sidecar))
            if kind == "dataset" and root is not None:
                for logical_path in _dataset_file_paths(record):
                    data_file = root / logical_path
                    co_located = data_file.parent / _sidecar_name(kind, entity_id)
                    if data_file.parent.is_dir():
                        _write_jsonld(co_located, document)
                        result.files.append(str(co_located))
    if result.identifier_root is not None and result.identifier_root == client.base_url:
        result.identifier_note = (
            f"Provenance @id identifiers are rooted at {result.identifier_root}, the URL "
            "this export connected to. If that is not your lab's permanent URL, set "
            "LAB_TRACKER_BASE_URL on the server before archiving sidecars so "
            "identifiers stay stable across hosts."
        )
    return result
