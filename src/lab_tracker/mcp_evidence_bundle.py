"""Composite evidence-bundle orchestration for MCP clients."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

JsonObject = dict[str, Any]

IDEMPOTENCY_METADATA_KEY = "lab_tracker_evidence_bundle_idempotency_key"


class EvidenceBundleClient(Protocol):
    def list_notes(self, **kwargs: Any) -> JsonObject: ...
    def list_datasets(self, **kwargs: Any) -> JsonObject: ...
    def list_analyses(self, **kwargs: Any) -> JsonObject: ...
    def list_claims(self, **kwargs: Any) -> JsonObject: ...
    def list_visualizations(self, **kwargs: Any) -> JsonObject: ...
    def create_note(self, **kwargs: Any) -> JsonObject: ...
    def create_dataset(self, **kwargs: Any) -> JsonObject: ...
    def create_analysis(self, **kwargs: Any) -> JsonObject: ...
    def create_claim(self, **kwargs: Any) -> JsonObject: ...
    def create_visualization(self, **kwargs: Any) -> JsonObject: ...
    def upload_visualization_file(self, **kwargs: Any) -> JsonObject: ...


def record_evidence_bundle(
    client: EvidenceBundleClient,
    *,
    project_id: str,
    primary_question_id: str | None = None,
    dataset: JsonObject | None = None,
    analysis: JsonObject | None = None,
    claim: JsonObject | None = None,
    visualization: JsonObject | None = None,
    source_note: JsonObject | None = None,
    dry_run: bool = True,
    idempotency_key: str | None = None,
) -> JsonObject:
    """Plan or record a dataset-analysis-claim-visualization evidence bundle."""
    if not str(project_id or "").strip():
        return _bundle_error("invalid_request", "project_id is required.")
    context = _BundleContext(
        client=client,
        project_id=str(project_id),
        primary_question_id=primary_question_id,
        idempotency_key=_clean(idempotency_key),
        dry_run=dry_run,
    )
    dataset = _object_or_none(dataset)
    analysis = _object_or_none(analysis)
    claim = _object_or_none(claim)
    visualization = _object_or_none(visualization)
    source_note = _object_or_none(source_note)

    if dataset:
        _plan_dataset(context, dataset)
    elif primary_question_id:
        context.warnings.append(
            "No dataset payload supplied; claims can only be supported by provided "
            "or existing dataset/analysis IDs."
        )
    if analysis:
        _plan_analysis(context, analysis)
    if claim:
        _plan_claim(context, claim)
    if visualization:
        _plan_visualization(context, visualization)
    if source_note:
        _plan_source_note(context, source_note)

    return {
        "data": {
            "dry_run": dry_run,
            "idempotency_key": context.idempotency_key,
            "project_id": context.project_id,
            "plan": context.plan,
            "created": context.created,
            "reused": context.reused,
            "warnings": context.warnings,
        }
    }


class _BundleContext:
    def __init__(
        self,
        *,
        client: EvidenceBundleClient,
        project_id: str,
        primary_question_id: str | None,
        idempotency_key: str | None,
        dry_run: bool,
    ) -> None:
        self.client = client
        self.project_id = project_id
        self.primary_question_id = _clean(primary_question_id)
        self.idempotency_key = idempotency_key
        self.dry_run = dry_run
        self.plan: list[JsonObject] = []
        self.created: dict[str, list[str]] = {
            "notes": [],
            "datasets": [],
            "analyses": [],
            "claims": [],
            "visualizations": [],
        }
        self.reused: dict[str, list[str]] = {
            "notes": [],
            "datasets": [],
            "analyses": [],
            "claims": [],
            "visualizations": [],
        }
        self.warnings: list[str] = []
        self.dataset_id: str | None = None
        self.analysis_id: str | None = None
        self.claim_id: str | None = None
        self.visualization_id: str | None = None


def _plan_dataset(context: _BundleContext, payload: JsonObject) -> None:
    explicit_id = _clean(payload.get("dataset_id"))
    if explicit_id:
        context.dataset_id = explicit_id
        _reused(context, "datasets", explicit_id, reason="provided_dataset_id")
        return
    primary_question_id = _clean(payload.get("primary_question_id")) or context.primary_question_id
    if not primary_question_id:
        _create_planned(
            context,
            "dataset",
            {},
            warning="Dataset create requires primary_question_id.",
        )
        return
    request = {
        "project_id": context.project_id,
        "primary_question_id": primary_question_id,
        "secondary_question_ids": _string_list(payload.get("secondary_question_ids")),
        "commit_manifest": _dataset_manifest(payload, context.idempotency_key),
        "commit_hash": _clean(payload.get("commit_hash")),
        "status": _clean(payload.get("status")) or "staged",
    }
    existing = _find_existing_dataset(context, request)
    if existing is not None:
        dataset_id = str(existing["dataset_id"])
        context.dataset_id = dataset_id
        _reused(context, "datasets", dataset_id, reason="idempotency_or_commit_match")
        return
    if context.dry_run:
        _create_planned(context, "dataset", request)
        return
    created = context.client.create_dataset(**request)
    context.dataset_id = _created_id(context, "datasets", "dataset", created, "dataset_id")


def _plan_analysis(context: _BundleContext, payload: JsonObject) -> None:
    explicit_id = _clean(payload.get("analysis_id"))
    if explicit_id:
        context.analysis_id = explicit_id
        _reused(context, "analyses", explicit_id, reason="provided_analysis_id")
        return
    dataset_ids = _string_list(payload.get("dataset_ids"))
    if not dataset_ids and context.dataset_id:
        dataset_ids = [context.dataset_id]
    method_hash = _clean(payload.get("method_hash"))
    code_version = _clean(payload.get("code_version"))
    if not dataset_ids or method_hash is None or code_version is None:
        _create_planned(
            context,
            "analysis",
            {},
            warning=(
                "Analysis create requires dataset_ids or a bundle dataset plus "
                "method_hash and code_version."
            ),
        )
        return
    if payload.get("derive_code_provenance") is True:
        context.warnings.append(
            "Code provenance derivation is not automatic in this tool; provide "
            "method_hash, code_version, and environment_hash explicitly."
        )
    request = {
        "project_id": context.project_id,
        "dataset_ids": dataset_ids,
        "method_hash": method_hash,
        "code_version": code_version,
        "environment_hash": _clean(payload.get("environment_hash")),
        "status": _clean(payload.get("status")) or "staged",
    }
    existing = _find_existing_analysis(context, request)
    if existing is not None:
        analysis_id = str(existing["analysis_id"])
        context.analysis_id = analysis_id
        _reused(context, "analyses", analysis_id, reason="method_dataset_match")
        return
    if context.dry_run:
        _create_planned(context, "analysis", request)
        return
    created = context.client.create_analysis(**request)
    context.analysis_id = _created_id(context, "analyses", "analysis", created, "analysis_id")


def _plan_claim(context: _BundleContext, payload: JsonObject) -> None:
    explicit_id = _clean(payload.get("claim_id"))
    if explicit_id:
        context.claim_id = explicit_id
        _reused(context, "claims", explicit_id, reason="provided_claim_id")
        return
    statement = _clean(payload.get("statement"))
    confidence = payload.get("confidence")
    if statement is None or confidence is None:
        _create_planned(
            context,
            "claim",
            {},
            warning="Claim create requires statement and confidence.",
        )
        return
    dataset_ids = _string_list(payload.get("supported_by_dataset_ids"))
    analysis_ids = _string_list(payload.get("supported_by_analysis_ids"))
    answer_ids = _string_list(payload.get("answers_question_ids"))
    if context.dataset_id and context.dataset_id not in dataset_ids:
        dataset_ids.append(context.dataset_id)
    if context.analysis_id and context.analysis_id not in analysis_ids:
        analysis_ids.append(context.analysis_id)
    if context.primary_question_id and context.primary_question_id not in answer_ids:
        answer_ids.append(context.primary_question_id)
    request = {
        "project_id": context.project_id,
        "statement": statement,
        "confidence": confidence,
        "status": _clean(payload.get("status")) or "proposed",
        "supported_by_dataset_ids": dataset_ids or None,
        "supported_by_analysis_ids": analysis_ids or None,
        "answers_question_ids": answer_ids or None,
    }
    existing = _find_existing_claim(context, statement)
    if existing is not None:
        claim_id = str(existing["claim_id"])
        context.claim_id = claim_id
        _reused(context, "claims", claim_id, reason="statement_match")
        return
    if context.dry_run:
        _create_planned(context, "claim", request)
        return
    created = context.client.create_claim(**request)
    context.claim_id = _created_id(context, "claims", "claim", created, "claim_id")


def _plan_visualization(context: _BundleContext, payload: JsonObject) -> None:
    explicit_id = _clean(payload.get("viz_id")) or _clean(payload.get("visualization_id"))
    upload_file = payload.get("upload_file") is True
    upload_file_path = _clean(payload.get("upload_file_path")) or _clean(payload.get("file_path"))
    if explicit_id:
        context.visualization_id = explicit_id
        _reused(context, "visualizations", explicit_id, reason="provided_visualization_id")
        if upload_file:
            _plan_visualization_upload(context, explicit_id, upload_file_path, payload)
        return
    analysis_id = _clean(payload.get("analysis_id")) or context.analysis_id
    viz_type = _clean(payload.get("viz_type"))
    file_path = _clean(payload.get("file_path"))
    if analysis_id is None or viz_type is None or file_path is None:
        _create_planned(
            context,
            "visualization",
            {},
            warning="Visualization create requires analysis_id, viz_type, and file_path.",
        )
        return
    related_claim_ids = _string_list(payload.get("related_claim_ids"))
    if context.claim_id and context.claim_id not in related_claim_ids:
        related_claim_ids.append(context.claim_id)
    request = {
        "analysis_id": analysis_id,
        "viz_type": viz_type,
        "file_path": file_path,
        "caption": _clean(payload.get("caption")),
        "related_claim_ids": related_claim_ids or None,
    }
    existing = _find_existing_visualization(context, request)
    if existing is not None:
        visualization_id = str(existing["viz_id"])
        context.visualization_id = visualization_id
        _reused(context, "visualizations", visualization_id, reason="analysis_file_match")
        if upload_file:
            if isinstance(existing.get("asset"), dict):
                context.plan.append(
                    {
                        "action": "reuse",
                        "entity_type": "visualization_asset",
                        "entity_id": str(existing["asset"].get("storage_id") or ""),
                        "reason": "existing_managed_asset",
                    }
                )
            else:
                _plan_visualization_upload(context, visualization_id, upload_file_path, payload)
        return
    if context.dry_run:
        _create_planned(context, "visualization", request)
        if upload_file:
            _plan_visualization_upload(context, "<new visualization>", upload_file_path, payload)
        elif file_path and _looks_like_local_path(file_path):
            context.warnings.append(
                "Visualization file_path looks local; set upload_file=true to copy it "
                "into managed Lab Tracker storage after creating the visualization."
            )
        return
    created = context.client.create_visualization(**request)
    context.visualization_id = _created_id(
        context,
        "visualizations",
        "visualization",
        created,
        "viz_id",
    )
    if upload_file:
        _plan_visualization_upload(context, context.visualization_id, upload_file_path, payload)


def _plan_source_note(context: _BundleContext, payload: JsonObject) -> None:
    explicit_id = _clean(payload.get("note_id"))
    if explicit_id:
        _reused(context, "notes", explicit_id, reason="provided_note_id")
        return
    raw_content = _clean(payload.get("raw_content"))
    if raw_content is None:
        _create_planned(
            context,
            "note",
            {},
            warning="Source note create requires raw_content.",
        )
        return
    existing = _find_existing_note(context)
    if existing is not None:
        _reused(context, "notes", str(existing["note_id"]), reason="idempotency_match")
        return
    request = {
        "project_id": context.project_id,
        "raw_content": raw_content,
        "transcribed_text": _clean(payload.get("transcribed_text")),
        "targets": payload.get("targets") or _default_note_targets(context),
        "metadata": _note_metadata(payload, context.idempotency_key),
        "status": _clean(payload.get("status")) or "staged",
    }
    if context.dry_run:
        _create_planned(context, "note", request)
        return
    created = context.client.create_note(**request)
    _created_id(context, "notes", "note", created, "note_id")


def _plan_visualization_upload(
    context: _BundleContext,
    viz_id: str | None,
    file_path: str | None,
    payload: JsonObject,
) -> None:
    if file_path is None:
        context.warnings.append("Visualization upload requested but no file path was supplied.")
        return
    request = {
        "viz_id": viz_id,
        "file_path": file_path,
        "content_type": _clean(payload.get("content_type")),
    }
    if context.dry_run or not viz_id or viz_id == "<new visualization>":
        context.plan.append(
            {
                "action": "upload",
                "entity_type": "visualization_asset",
                "request": request,
            }
        )
        return
    upload = context.client.upload_visualization_file(**request)
    context.plan.append(
        {
            "action": "uploaded",
            "entity_type": "visualization_asset",
            "entity_id": _response_id(upload, "storage_id"),
            "request": request,
        }
    )


def _find_existing_dataset(context: _BundleContext, request: JsonObject) -> JsonObject | None:
    datasets = _items(context.client.list_datasets(project_id=context.project_id, limit=200))
    commit_hash = _clean(request.get("commit_hash"))
    for dataset in datasets:
        if context.idempotency_key and _manifest_has_idempotency(dataset, context.idempotency_key):
            return dataset
        if commit_hash and _clean(dataset.get("commit_hash")) == commit_hash:
            return dataset
    return None


def _find_existing_analysis(context: _BundleContext, request: JsonObject) -> JsonObject | None:
    analyses = _items(context.client.list_analyses(project_id=context.project_id, limit=200))
    dataset_ids = set(_string_list(request.get("dataset_ids")))
    for analysis in analyses:
        if (
            set(_string_list(analysis.get("dataset_ids"))) == dataset_ids
            and _clean(analysis.get("method_hash")) == request["method_hash"]
            and _clean(analysis.get("code_version")) == request["code_version"]
        ):
            return analysis
    return None


def _find_existing_claim(context: _BundleContext, statement: str) -> JsonObject | None:
    claims = _items(context.client.list_claims(project_id=context.project_id, limit=200))
    normalized = statement.casefold().strip()
    for claim in claims:
        if _clean(claim.get("statement")) == statement:
            return claim
        existing = _clean(claim.get("statement"))
        if existing is not None and existing.casefold().strip() == normalized:
            return claim
    return None


def _find_existing_visualization(context: _BundleContext, request: JsonObject) -> JsonObject | None:
    visualizations = _items(
        context.client.list_visualizations(project_id=context.project_id, limit=200)
    )
    for visualization in visualizations:
        if (
            _clean(visualization.get("analysis_id")) == request["analysis_id"]
            and _clean(visualization.get("viz_type")) == request["viz_type"]
            and _clean(visualization.get("file_path")) == request["file_path"]
        ):
            return visualization
    return None


def _find_existing_note(context: _BundleContext) -> JsonObject | None:
    if context.idempotency_key is None:
        return None
    notes = _items(context.client.list_notes(project_id=context.project_id, limit=200))
    for note in notes:
        metadata = note.get("metadata")
        if (
            isinstance(metadata, dict)
            and metadata.get(IDEMPOTENCY_METADATA_KEY) == context.idempotency_key
        ):
            return note
    return None


def _dataset_manifest(payload: JsonObject, idempotency_key: str | None) -> JsonObject | None:
    manifest = payload.get("commit_manifest")
    if manifest is None:
        manifest = {}
    if not isinstance(manifest, dict):
        return None
    resolved = dict(manifest)
    if idempotency_key:
        metadata = dict(resolved.get("metadata") or {})
        metadata.setdefault(IDEMPOTENCY_METADATA_KEY, idempotency_key)
        resolved["metadata"] = metadata
    return resolved


def _note_metadata(payload: JsonObject, idempotency_key: str | None) -> JsonObject | None:
    metadata = payload.get("metadata")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        return None
    resolved = dict(metadata)
    if idempotency_key:
        resolved.setdefault(IDEMPOTENCY_METADATA_KEY, idempotency_key)
    return resolved


def _manifest_has_idempotency(dataset: JsonObject, idempotency_key: str) -> bool:
    manifest = dataset.get("commit_manifest")
    if not isinstance(manifest, dict):
        return False
    metadata = manifest.get("metadata")
    return isinstance(metadata, dict) and metadata.get(IDEMPOTENCY_METADATA_KEY) == idempotency_key


def _default_note_targets(context: _BundleContext) -> list[JsonObject]:
    if context.visualization_id:
        return [{"entity_type": "visualization", "entity_id": context.visualization_id}]
    if context.claim_id:
        return [{"entity_type": "claim", "entity_id": context.claim_id}]
    if context.analysis_id:
        return [{"entity_type": "analysis", "entity_id": context.analysis_id}]
    if context.dataset_id:
        return [{"entity_type": "dataset", "entity_id": context.dataset_id}]
    if context.primary_question_id:
        return [{"entity_type": "question", "entity_id": context.primary_question_id}]
    return []


def _created_id(
    context: _BundleContext,
    bucket: str,
    entity_type: str,
    response: JsonObject,
    id_key: str,
) -> str:
    entity_id = _response_id(response, id_key)
    if entity_id is None:
        context.warnings.append(f"{entity_type} create response did not include {id_key}.")
        entity_id = ""
    context.created[bucket].append(entity_id)
    context.plan.append(
        {
            "action": "created",
            "entity_type": entity_type,
            "entity_id": entity_id,
        }
    )
    return entity_id


def _reused(
    context: _BundleContext,
    bucket: str,
    entity_id: str,
    *,
    reason: str,
) -> None:
    context.reused[bucket].append(entity_id)
    context.plan.append(
        {
            "action": "reuse",
            "entity_type": bucket[:-1] if bucket.endswith("s") else bucket,
            "entity_id": entity_id,
            "reason": reason,
        }
    )


def _create_planned(
    context: _BundleContext,
    entity_type: str,
    request: JsonObject,
    *,
    warning: str | None = None,
) -> None:
    if warning:
        context.warnings.append(warning)
    context.plan.append(
        {
            "action": "create" if context.dry_run else "skipped",
            "entity_type": entity_type,
            "request": request,
        }
    )


def _response_id(response: JsonObject, id_key: str) -> str | None:
    data = response.get("data")
    if isinstance(data, dict) and data.get(id_key) is not None:
        return str(data[id_key])
    return None


def _items(payload: JsonObject) -> list[JsonObject]:
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [dict(item) for item in data if isinstance(item, dict)]


def _object_or_none(value: object) -> JsonObject | None:
    return dict(value) if isinstance(value, dict) else None


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, str):
        return []
    return [str(item) for item in value if str(item).strip()]


def _clean(value: object) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _looks_like_local_path(value: str) -> bool:
    return value.startswith(("/", "./", "../", "~")) or ":" not in value


def _bundle_error(code: str, message: str) -> JsonObject:
    return {"error": {"code": code, "message": message}}
