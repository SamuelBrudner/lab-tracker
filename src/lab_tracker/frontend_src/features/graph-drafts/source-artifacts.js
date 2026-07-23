// Pure source-artifact association helpers for the daily-review surface.
// New drafts carry precise source note ids on each source ref. Older batch
// drafts attached every batch note id to every proposal; those are deliberately
// treated as shared evidence so the UI never invents a precise relationship.

function isRecord(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function uniqueStrings(values) {
  return [...new Set(values.filter((value) => typeof value === "string" && value))];
}

function refSourceNoteIds(ref) {
  if (!isRecord(ref)) {
    return [];
  }
  return uniqueStrings([
    typeof ref.source_note_id === "string" ? ref.source_note_id : "",
    typeof ref.note_id === "string" ? ref.note_id : "",
    ...(Array.isArray(ref.source_note_ids) ? ref.source_note_ids : []),
  ]);
}

function metadataFlag(metadata, key) {
  const value = metadata?.[key];
  return value === true || value === "true" || value === 1 || value === "1";
}

function sanitizeDisplayUri(value) {
  if (typeof value !== "string" || !value.trim()) {
    return "";
  }
  const trimmed = value.trim();
  try {
    const parsed = new URL(trimmed);
    parsed.username = "";
    parsed.password = "";
    if (["http:", "https:", "ssh:", "git:"].includes(parsed.protocol)) {
      parsed.search = "";
      parsed.hash = "";
    }
    return parsed.toString();
  } catch {
    // Also cover unusual scp-like remotes containing explicit user:password
    // userinfo. Standard git@host:path remotes remain intact.
    return trimmed.replace(/^[^/@\s]+:[^/@\s]+@/, "");
  }
}

function isFigureArtifact(artifact) {
  if (artifact?.missing) {
    return true;
  }
  const metadata = isRecord(artifact?.metadata) ? artifact.metadata : {};
  return (
    String(artifact?.content_type || "").startsWith("image/") ||
    artifact?.type === "image" ||
    metadata.evidence_capture_kind === "figure" ||
    metadataFlag(metadata, "figure_no_preview") ||
    metadataFlag(metadata, "figure_review_bytes_stale")
  );
}

function sourceArtifacts(changeSet) {
  const contextArtifacts = Array.isArray(changeSet?.context_packet?.source_artifacts)
    ? changeSet.context_packet.source_artifacts.filter(isRecord)
    : [];
  const artifacts = [...contextArtifacts];
  const primaryNoteId = changeSet?.source_note_id;
  const primaryContentType = String(changeSet?.source_content_type || "");
  if (
    typeof primaryNoteId === "string" &&
    primaryNoteId &&
    primaryContentType.startsWith("image/") &&
    !artifacts.some((artifact) => artifact.note_id === primaryNoteId)
  ) {
    artifacts.push({
      content_type: primaryContentType,
      filename: changeSet.source_filename || "Source image",
      legacy_primary: true,
      metadata: {},
      note_id: primaryNoteId,
      type: "image",
    });
  }
  return artifacts;
}

function missingArtifact(noteId, operation) {
  const matchingRef = (operation?.source_refs || []).find((ref) =>
    refSourceNoteIds(ref).includes(noteId)
  );
  return {
    filename: matchingRef?.label || noteId,
    metadata: {},
    missing: true,
    note_id: noteId,
    type: "missing",
  };
}

function buildSourceArtifactReview(changeSet) {
  const artifacts = sourceArtifacts(changeSet);
  const figureArtifacts = artifacts.filter(isFigureArtifact);
  const operations = Array.isArray(changeSet?.operations) ? changeSet.operations : [];
  const byNoteId = new Map(
    artifacts
      .filter((artifact) => typeof artifact.note_id === "string" && artifact.note_id)
      .map((artifact) => [artifact.note_id, artifact])
  );
  const availableNoteIds = new Set(byNoteId.keys());
  const byOperationId = {};
  let hasAmbiguousLegacyMapping = false;

  for (const operation of operations) {
    const refs = Array.isArray(operation.source_refs) ? operation.source_refs : [];
    const citedIds = uniqueStrings(refs.flatMap(refSourceNoteIds));
    const hasSingularMapping = refs.some(
      (ref) =>
        typeof ref?.source_note_id === "string" || typeof ref?.note_id === "string"
    );
    const resolutions = new Set(
      refs
        .map((ref) => ref?.source_note_ids_resolution)
        .filter((value) => typeof value === "string" && value)
    );
    const hasExplicitResolution =
      hasSingularMapping || resolutions.has("explicit") || resolutions.has("single_source_fallback");
    const hasAmbiguousResolution = resolutions.has("ambiguous_bundle");
    const matched = citedIds
      .map((noteId) => byNoteId.get(noteId) || missingArtifact(noteId, operation));
    const matchedFigures = matched.filter(isFigureArtifact);
    const matchedAvailableIds = new Set(
      citedIds.filter((noteId) => availableNoteIds.has(noteId))
    );
    const isLegacyAllNotesMapping =
      hasAmbiguousResolution ||
      (!hasExplicitResolution &&
        artifacts.length > 1 &&
        matchedAvailableIds.size === availableNoteIds.size &&
        [...availableNoteIds].every((noteId) => matchedAvailableIds.has(noteId)));

    if (isLegacyAllNotesMapping) {
      const hasSharedFigureEvidence = figureArtifacts.length > 0;
      hasAmbiguousLegacyMapping ||= hasSharedFigureEvidence;
      byOperationId[operation.operation_id] = {
        artifacts: [],
        ambiguous: hasSharedFigureEvidence,
      };
      continue;
    }
    if (matched.length > 0 && !isLegacyAllNotesMapping) {
      byOperationId[operation.operation_id] = {
        artifacts: matchedFigures,
        ambiguous: false,
      };
      continue;
    }
    if (artifacts.length === 1) {
      byOperationId[operation.operation_id] = {
        artifacts: figureArtifacts,
        ambiguous: false,
      };
      continue;
    }
    if (artifacts.length > 1) {
      const hasSharedFigureEvidence = figureArtifacts.length > 0;
      hasAmbiguousLegacyMapping ||= hasSharedFigureEvidence;
      byOperationId[operation.operation_id] = {
        artifacts: [],
        ambiguous: hasSharedFigureEvidence,
      };
      continue;
    }
    byOperationId[operation.operation_id] = {
      artifacts: matchedFigures,
      ambiguous: false,
    };
  }

  const sharedArtifacts = hasAmbiguousLegacyMapping ? figureArtifacts : [];
  const uniqueArtifacts = new Map();
  for (const artifact of [
    ...sharedArtifacts,
    ...Object.values(byOperationId).flatMap((entry) => entry.artifacts),
  ]) {
    const key = artifact.note_id || artifact.artifact_id || artifact.filename;
    if (key && !uniqueArtifacts.has(key)) {
      uniqueArtifacts.set(key, artifact);
    }
  }

  return {
    artifactsToLoad: [...uniqueArtifacts.values()],
    byOperationId,
    hasPreciseMappings: Object.values(byOperationId).some(
      (entry) => !entry.ambiguous && entry.artifacts.length > 0
    ),
    sharedArtifacts,
  };
}

function sourceRefsForArtifact(operation, artifact, operationArtifactCount) {
  const refs = Array.isArray(operation?.source_refs) ? operation.source_refs : [];
  const matching = refs.filter((ref) => refSourceNoteIds(ref).includes(artifact.note_id));
  if (matching.length > 0) {
    return matching;
  }
  return operationArtifactCount === 1 ? refs : [];
}

function isPointerArtifact(artifact) {
  const metadata = isRecord(artifact?.metadata) ? artifact.metadata : {};
  return (
    metadataFlag(metadata, "figure_no_preview") ||
    metadataFlag(metadata, "no_preview") ||
    (metadata.evidence_capture_kind === "figure" &&
      !String(artifact?.content_type || "").startsWith("image/"))
  );
}

function isStaleArtifact(artifact) {
  const metadata = isRecord(artifact?.metadata) ? artifact.metadata : {};
  return (
    metadataFlag(metadata, "figure_review_bytes_stale") ||
    metadataFlag(metadata, "stale_review_bytes")
  );
}

function artifactDisplayName(artifact) {
  return artifact?.filename || artifact?.metadata?.evidence_title || artifact?.note_id || "Figure";
}

export {
  artifactDisplayName,
  buildSourceArtifactReview,
  isFigureArtifact,
  isPointerArtifact,
  isStaleArtifact,
  refSourceNoteIds,
  sanitizeDisplayUri,
  sourceRefsForArtifact,
};
