"""Deterministic keys that dedupe graph-draft generation requests."""

from __future__ import annotations

import hashlib
import json

from lab_tracker.errors import ValidationError
from lab_tracker.models import GraphChangeSet, GraphDraftMode, Note


def text_checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def note_generation_key(
    *,
    note: Note,
    source_notes: list[Note],
    mode: GraphDraftMode,
    prompt_version: str,
    user_hint: str | None,
    evidence_checksum: str | None,
    kind: str = "note",
) -> str:
    """Identify one exact note-source generation request across retries."""

    payload = {
        "version": "v1",
        "kind": kind,
        "project_id": str(note.project_id),
        "note_id": str(note.note_id),
        "source_versions": [
            {"note_id": str(item.note_id), "updated_at": item.updated_at.isoformat()}
            for item in source_notes
        ],
        "mode": mode.value,
        "prompt_version": prompt_version,
        "user_hint": user_hint,
        "evidence_checksum": evidence_checksum,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"generation:{digest[:48]}"


def successor_generation_key(rejected: GraphChangeSet) -> str:
    if rejected.batch_key is None:
        raise ValidationError("Rejected graph draft has no generation key.")
    digest = hashlib.sha256(
        f"{rejected.batch_key}|rejected:{rejected.change_set_id}".encode()
    ).hexdigest()
    return f"generation:{digest[:48]}"
