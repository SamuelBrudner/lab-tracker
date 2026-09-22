"""Persistent, incrementally refreshed evidence-key index for outbox syncs.

The watch, repo and HPC adapters dedupe every upload against the project's
existing evidence notes, keyed by ``(evidence_source_provider,
evidence_source_external_id, evidence_content_hash)``. The server's note list
has no filter for that key, so the index used to be rebuilt from a full
``/notes?project_id=`` listing on every sync, including the post-commit hook,
which made each commit cost ``ceil(notes / 200)`` sequential page requests.

This cache keeps the index on disk beside the adapter's outbox and bounds the
per-sync cost:

* **Incremental refresh.** Each load lists only notes created since the
  newest ``created_at`` seen (minus :data:`REFRESH_OVERLAP`, so a note whose
  transaction committed late is still picked up). A note missed anyway can
  only cause a duplicate upload, never a skipped one.
* **Verified hits.** A key found only in the on-disk cache is confirmed with a
  narrow ``since``/``until`` lookup around that note's ``created_at`` before an
  upload is skipped on its strength. A note that was deleted, or whose evidence
  metadata changed, is evicted and reported as a miss, so the cache can never
  cause evidence to be silently dropped.
* **Bounded staleness.** The whole index is rebuilt from a full listing when it
  is older than :data:`FULL_REFRESH_INTERVAL`, when its file is missing or
  unreadable as a cache (it is derived state; the server is authoritative), or
  when no watermark could be recorded.

The cache holds only evidence notes, as ``key -> (note_id, created_at)``, one
file per (server, project) under :func:`evidence_index_cache_dir`.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lab_tracker_client.client import (
    EvidenceNoteIndex,
    EvidenceNoteKey,
    LTRecord,
    _evidence_note_key,
)

if TYPE_CHECKING:
    from lab_tracker_client.client import LabTracker

CACHE_VERSION = 1
EVIDENCE_INDEX_CACHE_DIRNAME = ".evidence-index"
FULL_REFRESH_INTERVAL = timedelta(hours=24)
REFRESH_OVERLAP = timedelta(minutes=10)
VERIFY_WINDOW = timedelta(seconds=1)


def evidence_index_cache_dir(outbox: str | Path) -> Path:
    """Where an outbox keeps its evidence index cache.

    A subdirectory is outside the outbox's non-recursive ``*.json`` drain glob,
    so cache files are never mistaken for events.
    """

    return Path(outbox).expanduser() / EVIDENCE_INDEX_CACHE_DIRNAME


def outbox_note_index(
    client: LabTracker,
    note_indexes: dict[str, EvidenceNoteIndex],
    *,
    project_id: str,
    outbox: Path,
    dry_run: bool,
) -> EvidenceNoteIndex:
    """Return the per-sync evidence index for ``project_id``, loading it once.

    Real syncs use the outbox's persistent cache; a dry run lists notes
    without touching it, since a dry run must not change local state.
    """

    if project_id not in note_indexes:
        note_indexes[project_id] = client.build_evidence_note_index(
            project_id=project_id,
            cache_dir=None if dry_run else evidence_index_cache_dir(outbox),
        )
    return note_indexes[project_id]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(frozen=True)
class _Entry:
    note_id: str
    created_at: datetime


class CachedEvidenceNoteIndex:
    """Evidence-key lookup for one project, backed by the on-disk cache.

    ``get`` returns full note records: either one listed during this load, or
    one re-fetched from the server to verify a cached key.
    """

    def __init__(
        self,
        *,
        client: LabTracker,
        project_id: str,
        cache_path: Path,
        entries: dict[EvidenceNoteKey, _Entry],
        fresh: dict[EvidenceNoteKey, LTRecord],
        watermark: datetime | None,
        full_refreshed_at: datetime,
    ) -> None:
        self._client = client
        self._project_id = project_id
        self._cache_path = cache_path
        self._entries = entries
        self._fresh = fresh
        self._watermark = watermark
        self._full_refreshed_at = full_refreshed_at

    @classmethod
    def load(
        cls,
        client: LabTracker,
        *,
        project_id: str,
        cache_dir: str | Path,
    ) -> CachedEvidenceNoteIndex:
        project = str(project_id)
        cache_path = Path(cache_dir).expanduser() / _cache_filename(client.base_url, project)
        now = _utc_now()
        cached = _read_cache(cache_path, base_url=client.base_url, project_id=project)
        entries: dict[EvidenceNoteKey, _Entry] = {}
        watermark: datetime | None = None
        full_refreshed_at = now
        params: dict[str, Any] = {"project_id": project}
        if cached is not None:
            cached_entries, cached_watermark, cached_full_at = cached
            age = now - cached_full_at
            if cached_watermark is not None and timedelta(0) <= age <= FULL_REFRESH_INTERVAL:
                entries = cached_entries
                watermark = cached_watermark
                full_refreshed_at = cached_full_at
                params["since"] = (cached_watermark - REFRESH_OVERLAP).isoformat()
        fresh: dict[EvidenceNoteKey, LTRecord] = {}
        for note in client._iter_all("/notes", params=params):
            created_at = _parse_timestamp(note.get("created_at"))
            if created_at is not None and (watermark is None or created_at > watermark):
                watermark = created_at
            key = _evidence_note_key(note)
            if key is None:
                continue
            # First (oldest) listed note wins, matching the uncached full index;
            # an already cached entry for the key is kept on disk.
            fresh.setdefault(key, note)
            if created_at is not None:
                entries.setdefault(key, _Entry(note_id=str(note.id), created_at=created_at))
        index = cls(
            client=client,
            project_id=project,
            cache_path=cache_path,
            entries=entries,
            fresh=fresh,
            watermark=watermark,
            full_refreshed_at=full_refreshed_at,
        )
        index._save()
        return index

    def get(self, key: EvidenceNoteKey, /) -> LTRecord | None:
        note = self._fresh.get(key)
        if note is not None:
            return note
        entry = self._entries.get(key)
        if entry is None:
            return None
        verified = self._verify(key, entry)
        if verified is None:
            del self._entries[key]
            self._save()
            return None
        self._fresh[key] = verified
        return verified

    def __setitem__(self, key: EvidenceNoteKey, note: LTRecord, /) -> None:
        # In memory only: the next load's incremental refresh lists this note,
        # because it was created after the recorded watermark.
        self._fresh[key] = note

    def _verify(self, key: EvidenceNoteKey, entry: _Entry) -> LTRecord | None:
        params = {
            "project_id": self._project_id,
            "since": (entry.created_at - VERIFY_WINDOW).isoformat(),
            "until": (entry.created_at + VERIFY_WINDOW).isoformat(),
        }
        for note in self._client._iter_all("/notes", params=params):
            if str(note.id) == entry.note_id:
                return note if _evidence_note_key(note) == key else None
        return None

    def _save(self) -> None:
        payload = {
            "version": CACHE_VERSION,
            "base_url": self._client.base_url,
            "project_id": self._project_id,
            "full_refreshed_at": self._full_refreshed_at.isoformat(),
            "watermark": self._watermark.isoformat() if self._watermark else None,
            "entries": [
                {
                    "key": list(key),
                    "note_id": entry.note_id,
                    "created_at": entry.created_at.isoformat(),
                }
                for key, entry in sorted(self._entries.items())
            ],
        }
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._cache_path.with_name(f".{self._cache_path.name}.{uuid.uuid4().hex}.tmp")
        tmp_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp_path, self._cache_path)


def _cache_filename(base_url: str, project_id: str) -> str:
    digest = hashlib.sha256(f"{base_url}\x1f{project_id}".encode()).hexdigest()
    return f"{digest[:32]}.json"


def _read_cache(
    path: Path,
    *,
    base_url: str,
    project_id: str,
) -> tuple[dict[EvidenceNoteKey, _Entry], datetime | None, datetime] | None:
    """Decode a cache file, or ``None`` when it must be rebuilt from the server.

    A missing file, or one that is not a cache for this server and project in
    the current format, is rebuilt; other read errors propagate.
    """

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("version") != CACHE_VERSION
        or payload.get("base_url") != base_url
        or payload.get("project_id") != project_id
    ):
        return None
    full_refreshed_at = _parse_timestamp(payload.get("full_refreshed_at"))
    raw_entries = payload.get("entries")
    if full_refreshed_at is None or not isinstance(raw_entries, list):
        return None
    entries: dict[EvidenceNoteKey, _Entry] = {}
    for item in raw_entries:
        if not isinstance(item, dict):
            return None
        key = item.get("key")
        note_id = item.get("note_id")
        created_at = _parse_timestamp(item.get("created_at"))
        if (
            not isinstance(key, list)
            or len(key) != 3
            or not all(isinstance(part, str) and part for part in key)
            or not isinstance(note_id, str)
            or not note_id
            or created_at is None
        ):
            return None
        entries[(key[0], key[1], key[2])] = _Entry(note_id=note_id, created_at=created_at)
    return entries, _parse_timestamp(payload.get("watermark")), full_refreshed_at
