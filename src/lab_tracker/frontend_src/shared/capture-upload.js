// Capture-upload adapter: the mobile-capture command surface that talks to the
// network and the offline queue, extracted from the component so it can be
// tested without rendering. Every function takes its inputs explicitly (no
// closure over component state), and the online/offline decision lives here.
import { NetworkError, apiRequest } from "./api.js";
import { getUploadQueue } from "./register-sw.js";
import { TEXT_NOTE_PATH, UPLOAD_FILE_PATH } from "./upload-queue.js";

// Returned by uploadOrQueueRawFile when the network failed and the file was
// handed to the offline queue instead of uploaded.
const OFFLINE_QUEUED = Symbol("offline-queued");

// A client-generated id used both as a capture-bundle id and as the
// client_capture_id idempotency key.
function newCaptureId() {
  if (window.crypto?.randomUUID) {
    return window.crypto.randomUUID();
  }
  return `capture-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

// Derive source-file timing metadata from a File's lastModified stamp.
function sourceFileMetadata(file) {
  if (!file) {
    return {};
  }
  const metadata = {};
  const lastModified = Number(file.lastModified);
  if (Number.isFinite(lastModified) && lastModified > 0) {
    const roundedLastModified = Math.round(lastModified);
    metadata.source_file_last_modified_ms = roundedLastModified;
    metadata.source_file_last_modified_at = new Date(roundedLastModified).toISOString();
  }
  return metadata;
}

// Assemble the capture metadata bag written onto a note. captured_at is the
// composition clock: it is stamped here, before any queueing, so an offline
// capture replayed hours later still says when it was actually composed.
// The clock is injectable so tests can pin it.
function buildCaptureMetadata({
  captureMode,
  kind,
  bundleId = "",
  file = null,
  hint = "",
  voiceNoteType = "",
  now = () => Date.now(),
}) {
  const metadata = {
    capture_source: "mobile_capture",
    capture_mode: captureMode,
    capture_kind: kind,
    capture_review_status: "pending_review",
    captured_at: new Date(now()).toISOString(),
    ...sourceFileMetadata(file),
  };
  if (bundleId) {
    metadata.capture_bundle_id = bundleId;
  }
  if (hint.trim()) {
    metadata.capture_hint = hint.trim();
  }
  if (kind === "voice") {
    metadata.voice_note_type = voiceNoteType;
    metadata.transcript_status = "pending";
  }
  return metadata;
}

// Build the note target links from the selected entity ids.
function buildTargets({ questionId, sessionId, datasetId, analysisId, claimId, noteId }) {
  const targets = [];
  if (questionId) {
    targets.push({ entity_id: questionId, entity_type: "question" });
  }
  if (sessionId) {
    targets.push({ entity_id: sessionId, entity_type: "session" });
  }
  if (datasetId) {
    targets.push({ entity_id: datasetId, entity_type: "dataset" });
  }
  if (analysisId) {
    targets.push({ entity_id: analysisId, entity_type: "analysis" });
  }
  if (claimId) {
    targets.push({ entity_id: claimId, entity_type: "claim" });
  }
  if (noteId) {
    targets.push({ entity_id: noteId, entity_type: "note" });
  }
  return targets;
}

async function uploadRawFileNote({
  token,
  projectId,
  fileToUpload,
  metadata,
  clientCaptureId,
  targets = [],
}) {
  const payload = new FormData();
  payload.append("file", fileToUpload);
  payload.append("project_id", projectId);
  payload.append("metadata", JSON.stringify(metadata));
  payload.append("client_capture_id", clientCaptureId);
  if (targets.length > 0) {
    payload.append("targets", JSON.stringify(targets));
  }
  return apiRequest("/notes/upload-file", {
    body: payload,
    method: "POST",
    token,
  });
}

async function queueRawFileNoteOffline({
  ownerId,
  projectId,
  fileToUpload,
  metadata,
  clientCaptureId,
  targets = [],
  queue = getUploadQueue(),
}) {
  if (!queue) {
    return false;
  }
  const fields = {
    project_id: projectId,
    metadata: JSON.stringify(metadata),
    client_capture_id: clientCaptureId,
  };
  if (targets.length > 0) {
    fields.targets = JSON.stringify(targets);
  }
  await queue.enqueue({
    endpoint: UPLOAD_FILE_PATH,
    file: fileToUpload,
    fields,
    ownerId,
  });
  return true;
}

async function uploadOrQueueRawFile({
  token,
  projectId,
  ownerId,
  fileToUpload,
  metadata,
  targets = [],
  queue = getUploadQueue(),
}) {
  const clientCaptureId = newCaptureId();
  try {
    return await uploadRawFileNote({
      token,
      projectId,
      fileToUpload,
      metadata,
      clientCaptureId,
      targets,
    });
  } catch (err) {
    // Only queue when no HTTP response arrived (offline, DNS, CORS, etc.).
    // Server rejections (ApiError) and malformed 2xx bodies (ContractError)
    // must surface: the latter may already have created the note.
    if (err instanceof NetworkError) {
      const queued = await queueRawFileNoteOffline({
        ownerId,
        projectId,
        fileToUpload,
        metadata,
        clientCaptureId,
        targets,
        queue,
      });
      if (queued) {
        return OFFLINE_QUEUED;
      }
    }
    throw err;
  }
}

function textCaptureBody({ projectId, rawContent, targets = [], metadata, clientCaptureId }) {
  const body = {
    project_id: projectId,
    raw_content: rawContent,
    targets,
    metadata,
  };
  if (clientCaptureId) {
    body.client_capture_id = clientCaptureId;
  }
  return body;
}

async function createTextCapture({
  token,
  projectId,
  rawContent,
  targets = [],
  metadata,
  clientCaptureId = "",
}) {
  return apiRequest(TEXT_NOTE_PATH, {
    body: textCaptureBody({ projectId, rawContent, targets, metadata, clientCaptureId }),
    method: "POST",
    token,
  });
}

// Text captures follow the same online/offline rule as files: a server answer
// (success or rejection) is final, while a request that never reached the
// server is queued and replayed under the same client_capture_id.
async function createOrQueueTextCapture({
  token,
  projectId,
  ownerId,
  rawContent,
  targets = [],
  metadata,
  queue = getUploadQueue(),
}) {
  const clientCaptureId = newCaptureId();
  try {
    return await createTextCapture({
      token,
      projectId,
      rawContent,
      targets,
      metadata,
      clientCaptureId,
    });
  } catch (err) {
    if (err instanceof NetworkError && queue) {
      await queue.enqueue({
        endpoint: TEXT_NOTE_PATH,
        json: textCaptureBody({ projectId, rawContent, targets, metadata, clientCaptureId }),
        ownerId,
      });
      return OFFLINE_QUEUED;
    }
    throw err;
  }
}

export {
  OFFLINE_QUEUED,
  buildCaptureMetadata,
  buildTargets,
  createOrQueueTextCapture,
  createTextCapture,
  newCaptureId,
  queueRawFileNoteOffline,
  sourceFileMetadata,
  uploadOrQueueRawFile,
  uploadRawFileNote,
};
