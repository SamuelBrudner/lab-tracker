// Pure display/format helpers for the graph-draft review surface. Extracted
// from the component so the formatting, region-geometry, speech-script, and
// payload-target logic can be unit-tested without rendering.

const AUDIO_MIME_CANDIDATES = ["audio/webm", "audio/mp4", "audio/ogg"];

function pickAudioMimeType() {
  if (typeof MediaRecorder === "undefined" || typeof MediaRecorder.isTypeSupported !== "function") {
    return "";
  }
  return AUDIO_MIME_CANDIDATES.find((type) => MediaRecorder.isTypeSupported(type)) || "";
}

function audioExtensionForMime(mimeType) {
  if (mimeType.includes("mp4")) {
    return "m4a";
  }
  if (mimeType.includes("ogg")) {
    return "ogg";
  }
  return "webm";
}

function canRecordAudio() {
  return (
    typeof navigator !== "undefined" &&
    Boolean(navigator.mediaDevices?.getUserMedia) &&
    typeof MediaRecorder !== "undefined"
  );
}

function statusClass(status) {
  if (status === "accepted" || status === "applied" || status === "committed") {
    return "pill review-approved";
  }
  if (status === "rejected" || status === "failed") {
    return "pill review-rejected";
  }
  return "pill review-pending";
}

function operationTitle(operation) {
  return operation.semantic_type
    ? operation.semantic_type.replaceAll("_", " ")
    : `${operation.op} ${operation.entity_type}`;
}

function operationIntent(operation) {
  const semanticType = operation.semantic_type || "";
  if (semanticType.startsWith("link_note_to_")) {
    return `Proposed link to existing ${semanticType.replace("link_note_to_", "")}`;
  }
  if (semanticType === "suggest_new_question" || semanticType === "suggest_new_dataset") {
    return `Proposed new ${operation.entity_type}`;
  }
  if (semanticType === "create_note" || semanticType === "suggest_followup") {
    return "Proposed research note";
  }
  if (semanticType === "request_clarification") {
    return "Clarification request";
  }
  if (operation.op === "create") {
    return `Proposed new ${operation.entity_type}`;
  }
  return `Proposed ${operation.entity_type} update`;
}

function normalizeSpeechText(value) {
  return String(value || "")
    .replace(/[`*_#>]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function operationProposalText(operation, payloadTextById = {}) {
  let payload = operation?.payload || {};
  const editedPayload = payloadTextById[operation?.operation_id];
  if (typeof editedPayload === "string") {
    try {
      const parsed = JSON.parse(editedPayload);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        payload = parsed;
      }
    } catch {
      // Narrate the last valid server payload while an edit is incomplete.
    }
  }
  return normalizeSpeechText(
    payload.text ||
      payload.raw_content ||
      payload.label ||
      payload.prompt ||
      payload.statement ||
      operationTitle(operation)
  );
}

function spokenReviewScript(changeSet, payloadTextById = {}) {
  if (!changeSet) {
    return "";
  }
  const operations = changeSet.operations || [];
  const sections = [];
  const summary = normalizeSpeechText(changeSet.summary);
  if (summary) {
    sections.push(`Review summary. ${summary}`);
  }
  sections.push(
    operations.length === 1 ? "There is 1 proposal." : `There are ${operations.length} proposals.`
  );
  operations.forEach((operation, index) => {
    const proposal = [
      `Proposal ${index + 1}.`,
      `${normalizeSpeechText(operationIntent(operation))}.`,
      operationProposalText(operation, payloadTextById),
    ];
    const rationale = normalizeSpeechText(operation.rationale);
    if (rationale) {
      proposal.push(`Model inference. ${rationale}`);
    }
    if (operation.confidence !== null && operation.confidence !== undefined) {
      proposal.push(`${Math.round(operation.confidence * 100)} percent confidence.`);
    }
    sections.push(proposal.join(" "));
  });
  const uncertainties = [
    ...(changeSet.clarification_requests || []),
    ...(changeSet.uncertain_fields || []),
  ]
    .map(normalizeSpeechText)
    .filter(Boolean);
  if (uncertainties.length) {
    sections.push(`Questions for you. ${uncertainties.join(". ")}.`);
  }
  sections.push("You can dictate feedback now, or review each proposal below.");
  return sections.join(" ");
}

function canSpeakReview() {
  const speech = typeof window !== "undefined" ? window.speechSynthesis : null;
  return (
    Boolean(speech) &&
    typeof SpeechSynthesisUtterance !== "undefined" &&
    ["cancel", "pause", "resume", "speak"].every((method) => typeof speech[method] === "function")
  );
}

function payloadText(changeSet) {
  const entries = {};
  for (const operation of changeSet?.operations || []) {
    entries[operation.operation_id] = JSON.stringify(operation.payload || {}, null, 2);
  }
  return entries;
}

function operationReviewNoteText(changeSet) {
  const entries = {};
  for (const operation of changeSet?.operations || []) {
    entries[operation.operation_id] = operation.review_note || "";
  }
  return entries;
}

function imageDataUrl(raw) {
  if (!raw || !raw.content_base64 || !raw.content_type) {
    return "";
  }
  return `data:${raw.content_type};base64,${raw.content_base64}`;
}

function sourceRegionStyle(region) {
  if (!region || typeof region !== "object") {
    return null;
  }
  const { height, width, x, y } = region;
  const values = [x, y, width, height];
  if (values.some((value) => typeof value !== "number" || Number.isNaN(value) || value < 0)) {
    return null;
  }
  const multiplier = values.every((value) => value <= 1) ? 100 : 1;
  const left = x * multiplier;
  const top = y * multiplier;
  const boxWidth = width * multiplier;
  const boxHeight = height * multiplier;
  if (left > 100 || top > 100 || boxWidth <= 0 || boxHeight <= 0) {
    return null;
  }
  return {
    height: `${Math.min(boxHeight, 100 - top)}%`,
    left: `${left}%`,
    top: `${top}%`,
    width: `${Math.min(boxWidth, 100 - left)}%`,
  };
}

function sourceRegions(changeSet) {
  const regions = [];
  for (const operation of changeSet?.operations || []) {
    for (const ref of operation.source_refs || []) {
      const style = sourceRegionStyle(ref?.region);
      if (style) {
        regions.push({ operation, ref, style });
      }
    }
  }
  return regions;
}

function sourceRefText(ref) {
  const label = ref?.label ? `${ref.label}: ` : "";
  const quote = ref?.quote || "";
  return `${label}${quote}` || "Source reference";
}

function contextCountLabel(key) {
  return key.replaceAll("_", " ");
}

function canContributeWithRole(user, role) {
  return user?.role === "admin" || role === "contributor" || role === "owner";
}

function canManageWithRole(user, role) {
  return user?.role === "admin" || role === "owner";
}

function semanticLinkTargetType(operation) {
  if (operation.semantic_type === "link_note_to_question") {
    return "question";
  }
  if (operation.semantic_type === "link_note_to_session") {
    return "session";
  }
  if (operation.semantic_type === "link_note_to_dataset") {
    return "dataset";
  }
  if (operation.semantic_type === "link_note_to_analysis") {
    return "analysis";
  }
  return "";
}

function contextOptions(changeSet, entityType) {
  const context = changeSet?.context_packet || {};
  const batchProjects = Array.isArray(context.projects) ? context.projects : [];
  const batchValues = (field) =>
    batchProjects.flatMap((project) => (Array.isArray(project[field]) ? project[field] : []));
  if (entityType === "question") {
    return context.active_or_staged_questions || batchValues("active_or_staged_questions");
  }
  if (entityType === "session") {
    return context.recent_sessions || batchValues("recent_sessions");
  }
  if (entityType === "dataset") {
    return context.recent_datasets || batchValues("recent_datasets");
  }
  if (entityType === "analysis") {
    return context.recent_analyses || batchValues("recent_analyses");
  }
  return [];
}

function payloadTargetId(payload, entityType) {
  const targets = Array.isArray(payload?.targets) ? payload.targets : [];
  const match = targets.find((target) => target.entity_type === entityType);
  return typeof match?.entity_id === "string" ? match.entity_id : "";
}

function nextPayloadWithTarget(payload, entityType, entityId) {
  const targets = Array.isArray(payload?.targets) ? [...payload.targets] : [];
  const filtered = targets.filter((target) => target.entity_type !== entityType);
  if (entityId) {
    filtered.push({ entity_id: entityId, entity_type: entityType });
  }
  return { ...(payload || {}), targets: filtered };
}

function parsedPayloadFromText(raw) {
  try {
    const parsed = JSON.parse(raw || "{}");
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

export {
  audioExtensionForMime,
  canContributeWithRole,
  canManageWithRole,
  canRecordAudio,
  canSpeakReview,
  contextCountLabel,
  contextOptions,
  imageDataUrl,
  nextPayloadWithTarget,
  normalizeSpeechText,
  operationIntent,
  operationProposalText,
  operationReviewNoteText,
  operationTitle,
  parsedPayloadFromText,
  payloadTargetId,
  payloadText,
  pickAudioMimeType,
  semanticLinkTargetType,
  sourceRefText,
  sourceRegionStyle,
  sourceRegions,
  spokenReviewScript,
  statusClass,
};
