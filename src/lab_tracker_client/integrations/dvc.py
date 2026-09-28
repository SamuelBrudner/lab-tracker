"""``dvc.lock`` reading for ``lt pipeline dvc``.

DVC records, for every stage it last ran, the command and the md5 (or cloud
etag/checksum) of each dependency and output in ``dvc.lock``. That lock *is*
the run's declaration, already hashed, so this adapter re-hashes nothing: each
dependency and output becomes a pointer carrying DVC's own hash
(``md5:<hex>``; a directory's hash ends in ``.dir``) and size. Outputs are
every stage's ``outs``; declared inputs are the ``deps`` that no stage
produces (the pipeline's free inputs). Stage commands and parameters go into
the note body, bounded and credential-scrubbed.

``dvc.lock`` is YAML. PyYAML is not a Lab Tracker dependency, so the file is
read with :func:`lab_tracker_client.yaml_subset.load_yaml` (PyYAML when it is
installed, otherwise the built-in reader for the block-style subset DVC
writes). Both the ``schema: '2.0'`` layout (``stages:``) and the older
top-level-stages layout are accepted. Paths are resolved relative to the lock
file's directory; a stage with a ``wdir`` in ``dvc.yaml`` records paths
relative to that ``wdir``, which the lock does not say.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lab_tracker_client.pipeline_capture import PipelineRun, pointer_location
from lab_tracker_client.redaction import redact_capture_text
from lab_tracker_client.yaml_subset import load_yaml

MAX_STAGES_LISTED = 50
MAX_CMD_CHARS = 300
MAX_PARAMS_PER_STAGE = 20
_HASH_KEYS = ("md5", "etag", "checksum", "version_id")


@dataclass(frozen=True)
class DvcEntry:
    """One ``deps``/``outs`` entry of a stage."""

    path: str
    hash_name: str | None = None
    hash_value: str | None = None
    size: int | None = None
    nfiles: int | None = None

    @property
    def is_directory(self) -> bool:
        return bool(self.hash_value and self.hash_value.endswith(".dir")) or self.nfiles is not None


@dataclass(frozen=True)
class DvcStage:
    """One stage of ``dvc.lock``."""

    name: str
    cmd: tuple[str, ...]
    deps: tuple[DvcEntry, ...] = ()
    outs: tuple[DvcEntry, ...] = ()
    params: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    frozen: bool = False


def parse_dvc_lock(text: str) -> list[DvcStage]:
    """Parse the stages of a ``dvc.lock`` document."""

    document = load_yaml(text)
    if not isinstance(document, Mapping):
        raise ValueError("dvc.lock must be a YAML mapping")
    stages = document.get("stages") if "stages" in document else document
    if not isinstance(stages, Mapping):
        raise ValueError("dvc.lock 'stages' must be a mapping")
    parsed: list[DvcStage] = []
    for name, stage in stages.items():
        if name == "schema" or not isinstance(stage, Mapping):
            continue
        raw_cmd = stage.get("cmd")
        commands = raw_cmd if isinstance(raw_cmd, list) else [raw_cmd] if raw_cmd else []
        params = stage.get("params")
        parsed.append(
            DvcStage(
                name=str(name),
                cmd=tuple(str(command) for command in commands),
                deps=tuple(_entries(stage.get("deps"))),
                outs=tuple(_entries(stage.get("outs"))),
                params={
                    str(file): dict(values)
                    for file, values in (params.items() if isinstance(params, Mapping) else [])
                    if isinstance(values, Mapping)
                },
                frozen=bool(stage.get("frozen")),
            )
        )
    return parsed


def load_dvc_lock(path: str | Path) -> list[DvcStage]:
    """Read and parse a ``dvc.lock`` file."""

    return parse_dvc_lock(Path(path).expanduser().read_text(encoding="utf-8"))


def entry_pointer(entry: DvcEntry, *, root: Path, stage: str, role: str) -> dict[str, Any]:
    """A prebuilt pointer for one lock entry, carrying DVC's own hash."""

    title, uri, local = pointer_location(entry.path, base=root, root=root)
    kind = "remote" if local is None else ("directory" if entry.is_directory else "file")
    pointer: dict[str, Any] = {
        "title": title,
        "uri": uri,
        "kind": kind,
        "summary": f"DVC stage {stage} {'output' if role == 'output' else 'dependency'}"
        + (f"; {entry.nfiles} files" if entry.nfiles is not None else ""),
    }
    if entry.hash_value:
        pointer["content_hash"] = f"{entry.hash_name or 'md5'}:{entry.hash_value}"
    if entry.size is not None:
        pointer["size_bytes"] = entry.size
    return pointer


def stage_details(stages: Sequence[DvcStage]) -> list[str]:
    """Markdown lines for each stage's command, dependencies, outputs and params."""

    lines: list[str] = []
    for stage in stages[:MAX_STAGES_LISTED]:
        frozen = " (frozen)" if stage.frozen else ""
        lines.append(
            f"- Stage `{stage.name}`{frozen}: {len(stage.deps)} deps, {len(stage.outs)} outs"
        )
        for command in stage.cmd:
            cleaned = redact_capture_text(" ".join(command.split()))
            if len(cleaned) > MAX_CMD_CHARS:
                cleaned = cleaned[: MAX_CMD_CHARS - 1] + "…"
            lines.append(f"  - cmd: `{cleaned}`")
        for file, values in stage.params.items():
            items = list(values.items())
            shown = ", ".join(
                f"{key}={redact_capture_text(str(value))[:80]}"
                for key, value in items[:MAX_PARAMS_PER_STAGE]
            )
            more = (
                f", … {len(items) - MAX_PARAMS_PER_STAGE} more"
                if len(items) > MAX_PARAMS_PER_STAGE
                else ""
            )
            lines.append(f"  - params ({file}): {shown}{more}")
    if len(stages) > MAX_STAGES_LISTED:
        lines.append(f"- … and {len(stages) - MAX_STAGES_LISTED} more stages")
    return lines


def pipeline_run_from_lock(
    lock_path: str | Path,
    *,
    status: str | None = None,
    run_id: str | None = None,
    inputs: Sequence[str | Mapping[str, Any]] = (),
    outputs: Sequence[str | Mapping[str, Any]] = (),
    logs: Sequence[str | Path] = (),
    label: str | None = None,
    summary: str | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
    tags: Sequence[str] = (),
) -> PipelineRun:
    """A :class:`PipelineRun` for the pipeline state recorded in ``dvc.lock``.

    The default run id is derived from the lock's content, so reporting the
    same lock twice records one run.
    """

    path = Path(lock_path).expanduser().resolve()
    raw = path.read_bytes()
    stages = parse_dvc_lock(raw.decode("utf-8"))
    root = path.parent
    produced = {entry.path for stage in stages for entry in stage.outs}
    declared_outputs: list[str | Mapping[str, Any]] = [
        entry_pointer(entry, root=root, stage=stage.name, role="output")
        for stage in stages
        for entry in stage.outs
    ]
    free_inputs: list[str | Mapping[str, Any]] = []
    seen: set[str] = set()
    for stage in stages:
        for entry in stage.deps:
            if entry.path in produced or entry.path in seen:
                continue
            seen.add(entry.path)
            free_inputs.append(entry_pointer(entry, root=root, stage=stage.name, role="input"))
    lock_hash = hashlib.sha256(raw).hexdigest()
    return PipelineRun(
        engine="dvc",
        status=status or "unknown",
        run_id=run_id or f"dvc-lock-{lock_hash[:16]}",
        started_at=started_at,
        ended_at=ended_at,
        inputs=[*free_inputs, *inputs],
        outputs=[*declared_outputs, *outputs],
        logs=logs,
        label=label,
        summary=summary,
        details=stage_details(stages),
        metadata={
            "pipeline_dvc_stage_count": len(stages),
            "pipeline_dvc_lock_sha256": lock_hash,
            "pipeline_dvc_lock": path.name,
        },
        tags=tags,
    )


def _entries(value: Any) -> list[DvcEntry]:
    if not isinstance(value, list):
        return []
    entries: list[DvcEntry] = []
    for item in value:
        if not isinstance(item, Mapping) or not item.get("path"):
            continue
        hash_name = None
        hash_value = None
        for key in _HASH_KEYS:
            if item.get(key):
                hash_name, hash_value = key, str(item[key])
                break
        entries.append(
            DvcEntry(
                path=str(item["path"]),
                hash_name=hash_name,
                hash_value=hash_value,
                size=_int_or_none(item.get("size")),
                nfiles=_int_or_none(item.get("nfiles")),
            )
        )
    return entries


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "DvcEntry",
    "DvcStage",
    "entry_pointer",
    "load_dvc_lock",
    "parse_dvc_lock",
    "pipeline_run_from_lock",
    "stage_details",
]
