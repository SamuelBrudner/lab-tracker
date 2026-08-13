"""Canonical, versioned text documents for graph retrieval.

The graph query, semantic index, and evaluation paths all consume this module so
labels, searchable fields, snippets, and embedding inputs cannot drift apart.
Only intrinsic record text is rendered: graph-neighbor content and resolved
artifact bytes are deliberately excluded.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from lab_tracker.models import encode_session_link_code
from lab_tracker.schemas import GraphNodeSummary

GRAPH_DOCUMENT_RENDERER_VERSION = "graph-document-v1"
GRAPH_DOCUMENT_CHUNK_SIZE = 4_000
GRAPH_DOCUMENT_CHUNK_OVERLAP = 400
GRAPH_DOCUMENT_MAX_CHUNKS = 8
GRAPH_LABEL_LIMIT = 180

NodeKey = tuple[str, str]
LexicalFieldInput = tuple[str, object, bool]
SectionInput = tuple[str, object]
RenderedParts = tuple[
    NodeKey,
    GraphNodeSummary,
    tuple[LexicalFieldInput, ...],
    tuple[SectionInput, ...],
    tuple[SectionInput, ...],
]


@dataclass(frozen=True)
class GraphLexicalField:
    name: str
    value: str
    title: bool = False


@dataclass(frozen=True)
class GraphNodeDocument:
    key: NodeKey
    summary: GraphNodeSummary
    lexical_fields: tuple[GraphLexicalField, ...]
    semantic_text: str | None
    content: str
    document_hash: str
    status_snapshot: str | None
    source_updated_at: Any
    renderer_version: str = GRAPH_DOCUMENT_RENDERER_VERSION

    def chunks(self) -> tuple[GraphDocumentChunk, ...]:
        return chunk_graph_document(self.semantic_text or "")


@dataclass(frozen=True)
class GraphDocumentChunk:
    index: int
    start_char: int
    end_char: int
    text: str


class GraphNodeDocumentRenderer:
    """Render one persisted graph ORM row into a deterministic retrieval document."""

    version = GRAPH_DOCUMENT_RENDERER_VERSION

    def render(self, entity_type: str, row: Any) -> GraphNodeDocument:
        method = getattr(self, f"_render_{entity_type}", None)
        if method is None:
            raise ValueError(f"Unsupported graph entity type: {entity_type}")
        key, summary, fields, semantic_sections, content_sections = method(row)
        lexical_fields = tuple(
            GraphLexicalField(name=name, value=_normalize_text(value), title=title)
            for name, value, title in fields
            if _normalize_text(value)
        )
        semantic_text = _section_text(semantic_sections) or None
        content = _section_text(content_sections)
        digest_source = semantic_text or ""
        document_hash = hashlib.sha256(
            f"{self.version}\0{digest_source}".encode()
        ).hexdigest()
        return GraphNodeDocument(
            key=key,
            summary=summary,
            lexical_fields=lexical_fields,
            semantic_text=semantic_text,
            content=content,
            document_hash=document_hash,
            status_snapshot=summary.status,
            source_updated_at=summary.updated_at,
        )

    def _render_question(self, row: Any) -> RenderedParts:
        entity_id = str(row.question_id)
        status = _enum_value(row.status)
        fields = (("title", row.text, True), ("hypothesis", row.hypothesis, False))
        sections = (("Question", row.text), ("Hypothesis", row.hypothesis))
        return (
            ("question", entity_id),
            GraphNodeSummary(
                id=f"question:{entity_id}",
                entity_type="question",
                entity_id=entity_id,
                label=_compact(row.text),
                detail=_enum_value(row.question_type),
                status=status,
                route=f"/app/questions/{entity_id}",
                updated_at=row.updated_at,
                metadata={
                    "hypothesis": row.hypothesis or "",
                    "question_type": _enum_value(row.question_type) or "",
                },
            ),
            fields,
            sections,
            sections,
        )

    def _render_session(self, row: Any) -> RenderedParts:
        entity_id = str(row.session_id)
        link_code = encode_session_link_code(UUID(entity_id))
        session_type = _enum_value(row.session_type) or "session"
        fields = (("session_type", session_type, False), ("link_code", link_code, False))
        semantic = (("Session type", session_type),)
        content = (
            *semantic,
            ("Link code", link_code),
            ("Status", _enum_value(row.status)),
        )
        return (
            ("session", entity_id),
            GraphNodeSummary(
                id=f"session:{entity_id}",
                entity_type="session",
                entity_id=entity_id,
                label=f"{session_type} session",
                detail=link_code,
                status=_enum_value(row.status),
                route=f"/app/sessions/{entity_id}",
                updated_at=row.updated_at,
            ),
            fields,
            semantic,
            content,
        )

    def _render_note(self, row: Any) -> RenderedParts:
        entity_id = str(row.note_id)
        preferred = row.transcribed_text or row.raw_content or row.raw_filename or ""
        fields = (
            ("transcribed_text", row.transcribed_text, False),
            ("raw_content", row.raw_content, False),
            ("raw_filename", row.raw_filename, False),
            ("metadata", _natural_structure_text(row.note_metadata), False),
        )
        semantic = (
            ("Transcript", row.transcribed_text),
            ("Note", row.raw_content),
            ("Metadata", _natural_structure_text(row.note_metadata)),
        )
        content = (*semantic, ("Filename", row.raw_filename))
        return (
            ("note", entity_id),
            GraphNodeSummary(
                id=f"note:{entity_id}",
                entity_type="note",
                entity_id=entity_id,
                label=_compact(preferred or f"Note {_short_id(entity_id)}"),
                status=_enum_value(row.status),
                route=f"/app/notes/{entity_id}",
                updated_at=row.updated_at,
            ),
            fields,
            semantic,
            content,
        )

    def _render_dataset(self, row: Any) -> RenderedParts:
        entity_id = str(row.dataset_id)
        name = _dataset_name(row)
        semantic_name = _normalize_text(
            (row.manifest_metadata or {}).get("dataset_name") or ""
        )
        metadata = _natural_structure_text(row.manifest_metadata)
        nwb = _natural_structure_text(row.manifest_nwb_metadata)
        bids = _natural_structure_text(row.manifest_bids_metadata)
        artifacts = _natural_structure_text(row.manifest_external_artifacts)
        files = _bounded_structure_text(row.manifest_files)
        fields = (
            ("title", name, True),
            ("commit_hash", row.commit_hash, False),
            ("metadata", metadata, False),
            ("nwb_metadata", nwb, False),
            ("bids_metadata", bids, False),
            ("external_artifacts", _bounded_structure_text(row.manifest_external_artifacts), False),
            ("files", files, False),
        )
        semantic = (
            ("Dataset", semantic_name),
            ("Metadata", metadata),
            ("NWB metadata", nwb),
            ("BIDS metadata", bids),
            ("External artifact metadata", artifacts),
        )
        content = (
            *semantic,
            ("Commit hash", row.commit_hash),
            ("Files", files),
        )
        return (
            ("dataset", entity_id),
            GraphNodeSummary(
                id=f"dataset:{entity_id}",
                entity_type="dataset",
                entity_id=entity_id,
                label=_compact(name or f"Dataset {row.commit_hash or _short_id(entity_id)}"),
                detail=row.commit_hash,
                status=_enum_value(row.status),
                route=f"/app/datasets/{entity_id}",
                updated_at=row.updated_at,
            ),
            fields,
            semantic,
            content,
        )

    def _render_analysis(self, row: Any) -> RenderedParts:
        entity_id = str(row.analysis_id)
        artifacts = _natural_structure_text(row.external_artifacts)
        fields = (
            ("title", f"Analysis {row.code_version}", True),
            ("code_version", row.code_version, False),
            ("method_hash", row.method_hash, False),
            ("environment_hash", row.environment_hash, False),
            ("external_artifacts", _bounded_structure_text(row.external_artifacts), False),
        )
        semantic = (
            ("External artifact metadata", artifacts),
        )
        content = (
            *semantic,
            ("Code version", row.code_version),
            ("Method hash", row.method_hash),
            ("Environment hash", row.environment_hash),
        )
        return (
            ("analysis", entity_id),
            GraphNodeSummary(
                id=f"analysis:{entity_id}",
                entity_type="analysis",
                entity_id=entity_id,
                label=_compact(f"Analysis {row.code_version or _short_id(entity_id)}"),
                detail=row.method_hash,
                status=_enum_value(row.status),
                updated_at=row.updated_at,
                metadata={"code_version": row.code_version},
            ),
            fields,
            semantic,
            content,
        )

    def _render_claim(self, row: Any) -> RenderedParts:
        entity_id = str(row.claim_id)
        citations = _natural_structure_text(row.external_citations)
        fields = (
            ("statement", row.statement, True),
            ("falsification_criteria", row.falsification_criteria, False),
            ("verification_plan", row.verification_plan, False),
            ("refuting_outcome", row.refuting_outcome, False),
            ("external_citations", _bounded_structure_text(row.external_citations), False),
        )
        sections = (
            ("Claim", row.statement),
            ("Falsification criteria", row.falsification_criteria),
            ("Verification plan", row.verification_plan),
            ("Refuting outcome", row.refuting_outcome),
            ("Citation metadata", citations),
        )
        return (
            ("claim", entity_id),
            GraphNodeSummary(
                id=f"claim:{entity_id}",
                entity_type="claim",
                entity_id=entity_id,
                label=_compact(row.statement),
                detail=f"confidence {row.confidence:g}",
                status=_enum_value(row.status),
                updated_at=row.updated_at,
            ),
            fields,
            sections,
            sections,
        )

    def _render_exploration_node(self, row: Any) -> RenderedParts:
        entity_id = str(row.node_id)
        alternatives = _plain_list_text(row.alternatives_considered)
        fields = (
            ("title", row.title, True),
            ("choice", row.choice, False),
            ("alternatives_considered", alternatives, False),
            ("rationale", row.rationale, False),
            ("hypothesis", row.hypothesis, False),
            ("failure_mode", row.failure_mode, False),
            ("lesson", row.lesson, False),
            ("tooling_context", row.tooling_context, False),
            ("trigger", row.trigger, False),
        )
        sections = tuple((name.replace("_", " ").title(), value) for name, value, _ in fields)
        return (
            ("exploration_node", entity_id),
            GraphNodeSummary(
                id=f"exploration_node:{entity_id}",
                entity_type="exploration_node",
                entity_id=entity_id,
                label=_compact(row.title),
                detail=(_enum_value(row.node_type) or "").replace("_", " "),
                status=_enum_value(row.status),
                updated_at=row.updated_at,
                metadata={
                    "target_entity_type": _enum_value(row.target_entity_type) or "",
                    "target_entity_id": str(row.target_entity_id),
                    "origin": row.origin or "",
                },
            ),
            fields,
            sections,
            sections,
        )

    def _render_visualization(self, row: Any) -> RenderedParts:
        entity_id = str(row.viz_id)
        fields = (
            ("caption", row.caption, True),
            ("viz_type", row.viz_type, False),
            ("file_path", row.file_path, False),
            ("asset_filename", row.asset_filename, False),
            ("asset_checksum", row.asset_checksum, False),
        )
        semantic = (("Visualization", row.caption or row.viz_type), ("Type", row.viz_type))
        content = (*semantic, ("File path", row.file_path), ("Asset filename", row.asset_filename))
        return (
            ("visualization", entity_id),
            GraphNodeSummary(
                id=f"visualization:{entity_id}",
                entity_type="visualization",
                entity_id=entity_id,
                label=_compact(row.caption or row.viz_type),
                detail=row.file_path,
                route=f"/app/visualizations/{entity_id}",
                updated_at=row.updated_at,
                metadata={"viz_type": row.viz_type},
            ),
            fields,
            semantic,
            content,
        )

    def _render_goal(self, row: Any) -> RenderedParts:
        entity_id = str(row.goal_id)
        attributes = _natural_structure_text(row.attributes)
        fields = (
            ("title", row.title, True),
            ("summary", row.summary, False),
            ("external_ref", row.external_ref, False),
            ("attributes", _bounded_structure_text(row.attributes), False),
        )
        semantic = (("Goal", row.title), ("Summary", row.summary), ("Attributes", attributes))
        content = (*semantic, ("External reference", row.external_ref))
        return (
            ("goal", entity_id),
            GraphNodeSummary(
                id=f"goal:{entity_id}",
                entity_type="goal",
                entity_id=entity_id,
                label=_compact(row.title),
                detail=_enum_value(row.goal_type),
                status=_enum_value(row.status),
                route=f"/app/goals/{entity_id}",
                updated_at=row.updated_at,
                metadata={
                    "target_date": row.target_date.isoformat() if row.target_date else "",
                    "external_ref": row.external_ref or "",
                },
            ),
            fields,
            semantic,
            content,
        )


def chunk_graph_document(
    text: str,
    *,
    chunk_size: int = GRAPH_DOCUMENT_CHUNK_SIZE,
    overlap: int = GRAPH_DOCUMENT_CHUNK_OVERLAP,
    max_chunks: int = GRAPH_DOCUMENT_MAX_CHUNKS,
) -> tuple[GraphDocumentChunk, ...]:
    """Split normalized text deterministically, retaining the tail when capped."""

    normalized = _normalize_text(text)
    if not normalized:
        return ()
    if chunk_size < 1 or overlap < 0 or overlap >= chunk_size or max_chunks < 1:
        raise ValueError("Invalid graph document chunking limits.")
    starts: list[int] = []
    cursor = 0
    while cursor < len(normalized):
        starts.append(cursor)
        if cursor + chunk_size >= len(normalized):
            break
        proposed = cursor + chunk_size - overlap
        paragraph = normalized.rfind("\n\n", cursor + 1, proposed + 1)
        cursor = paragraph + 2 if paragraph > cursor else proposed
    if len(starts) > max_chunks:
        tail_start = max(0, len(normalized) - chunk_size)
        starts = starts[: max_chunks - 1] + [tail_start]
    unique_starts = sorted(set(starts))
    return tuple(
        GraphDocumentChunk(
            index=index,
            start_char=start,
            end_char=min(len(normalized), start + chunk_size),
            text=normalized[start : start + chunk_size],
        )
        for index, start in enumerate(unique_starts)
    )


def _section_text(sections: Iterable[tuple[str, object]]) -> str:
    rendered: list[str] = []
    for label, raw_value in sections:
        value = _normalize_text(raw_value)
        if value:
            rendered.append(f"{label}: {value}")
    return "\n\n".join(rendered)


def _normalize_text(value: object) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFC", str(value)).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x00", "")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def _natural_structure_text(value: object) -> str:
    pairs: list[str] = []
    for key, item in _flatten_structure(value):
        folded = key.casefold()
        key_tokens = {token for token in re.split(r"[^a-z0-9]+", folded) if token}
        if key_tokens.intersection(
            {
                "credential",
                "credentials",
                "id",
                "uuid",
                "hash",
                "checksum",
                "password",
                "path",
                "secret",
                "token",
                "uri",
                "url",
                "locator",
            }
        ) or ("key" in key_tokens and {"api", "access", "private"} & key_tokens):
            continue
        pairs.append(f"{key}: {item}" if key else item)
        if len(pairs) >= 50 or sum(map(len, pairs)) >= 8_000:
            break
    return "\n".join(pairs)


def _bounded_structure_text(value: object) -> str:
    try:
        rendered = json.dumps(
            value or {},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except TypeError:
        rendered = str(value or "")
    return _normalize_text(rendered)[:8_000]


def _flatten_structure(value: object, prefix: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        for key in sorted(value, key=lambda item: str(item).casefold()):
            child = f"{prefix}.{key}" if prefix else str(key)
            yield from _flatten_structure(value[key], child)
    elif isinstance(value, list):
        for item in value[:50]:
            yield from _flatten_structure(item, prefix)
    elif isinstance(value, (str, int, float, bool)):
        rendered = _normalize_text(value)
        if rendered:
            yield prefix, rendered


def _plain_list_text(values: object) -> str:
    if not isinstance(values, list):
        return ""
    return "\n".join(_normalize_text(value) for value in values[:50] if _normalize_text(value))


def _dataset_name(row: Any) -> str:
    metadata = row.manifest_metadata or {}
    name = _normalize_text(metadata.get("dataset_name") or "")
    if not name and row.manifest_files:
        path = str(row.manifest_files[0].get("path") or "")
        name = path.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name


def _enum_value(value: object) -> str | None:
    if value is None:
        return None
    enum_value = getattr(value, "value", None)
    return str(enum_value if enum_value is not None else value)


def _short_id(value: str) -> str:
    return value.split("-", 1)[0]


def _compact(value: object) -> str:
    return _normalize_text(value)[:GRAPH_LABEL_LIMIT]
