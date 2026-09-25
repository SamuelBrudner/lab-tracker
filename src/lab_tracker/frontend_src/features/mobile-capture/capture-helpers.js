// Display/query helpers shared by the mobile-capture controller and its
// presentational sections, plus the small per-device capture-context store.

import { DRAFT_KEY_PREFIX } from "../../hooks/useLocalDraft.js";

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
      sessionId: params.get("session_id") || "",
    };
  } catch {
    return { checkpointNoteId: "", projectId: "", returnPath: "", sessionId: "" };
  }
}

// The question and session a person last captured against, per project, kept
// on this device so the next bench capture starts from the same context. It
// lives under the local-draft prefix so signing out drops it with the drafts.
function rememberedContextKey(projectId) {
  return `${DRAFT_KEY_PREFIX}capture-context:${projectId}`;
}

function readRememberedCaptureContext(projectId) {
  if (!projectId) {
    return { questionId: "", sessionId: "" };
  }
  try {
    const raw = globalThis.localStorage?.getItem(rememberedContextKey(projectId));
    const parsed = raw ? JSON.parse(raw) : null;
    return {
      questionId: typeof parsed?.questionId === "string" ? parsed.questionId : "",
      sessionId: typeof parsed?.sessionId === "string" ? parsed.sessionId : "",
    };
  } catch {
    return { questionId: "", sessionId: "" };
  }
}

function writeRememberedCaptureContext(projectId, { questionId = "", sessionId = "" }) {
  if (!projectId) {
    return;
  }
  try {
    const storage = globalThis.localStorage;
    if (!storage) {
      return;
    }
    if (!questionId && !sessionId) {
      storage.removeItem(rememberedContextKey(projectId));
      return;
    }
    storage.setItem(rememberedContextKey(projectId), JSON.stringify({ questionId, sessionId }));
  } catch {
    // Storage may be unavailable (private mode, quota); the capture still saved.
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
  readRememberedCaptureContext,
  writeRememberedCaptureContext,
};
