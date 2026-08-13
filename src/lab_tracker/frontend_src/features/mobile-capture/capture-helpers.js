// Pure display/query helpers shared by the mobile-capture controller and its
// presentational sections.

function captureNotes(notes) {
  return notes.filter((note) => note.metadata?.capture_source === "mobile_capture");
}

function captureHint(note) {
  return String(note?.metadata?.capture_hint || "").trim();
}

function compactLabel(value, fallback = "(untitled)") {
  const text = String(value || fallback);
  return text.length > 90 ? `${text.slice(0, 87)}...` : text;
}

function isAudioCapture(note) {
  return Boolean(note?.raw_asset?.content_type?.startsWith("audio/"));
}

function hasTranscript(note) {
  return Boolean(String(note?.transcribed_text || "").trim());
}

function bundleAudioNotes(note, notes) {
  const bundleId = note?.metadata?.capture_bundle_id;
  if (!bundleId) {
    return [];
  }
  return notes.filter(
    (candidate) => candidate.metadata?.capture_bundle_id === bundleId && isAudioCapture(candidate)
  );
}

function missingBundleTranscript(note, notes) {
  return bundleAudioNotes(note, notes).some((candidate) => !hasTranscript(candidate));
}

function readCaptureLaunchContext(search = window.location.search) {
  try {
    const params = new URLSearchParams(search || "");
    const returnPath = params.get("return_to") || "";
    return {
      checkpointNoteId: params.get("checkpoint_note_id") || "",
      projectId: params.get("project_id") || "",
      returnPath: returnPath.startsWith("/app/") ? returnPath : "",
    };
  } catch {
    return { checkpointNoteId: "", projectId: "", returnPath: "" };
  }
}

export {
  bundleAudioNotes,
  captureHint,
  captureNotes,
  compactLabel,
  hasTranscript,
  isAudioCapture,
  missingBundleTranscript,
  readCaptureLaunchContext,
};
