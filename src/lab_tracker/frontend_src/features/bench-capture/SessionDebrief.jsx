import * as React from "react";

import { useAudioRecorder } from "../../hooks/useAudioRecorder.js";
import {
  OFFLINE_QUEUED,
  buildCaptureMetadata,
  newCaptureId,
  uploadOrQueueRawFile,
} from "../../shared/capture-upload.js";
import { getUploadQueue } from "../../shared/register-sw.js";
import {
  CAPTURE_CHANNEL,
  SESSION_DEBRIEF_PURPOSE,
  errorMessage,
  sessionTargets,
} from "./bench-helpers.js";

const { useMemo, useRef, useState } = React;

const DEBRIEF_PROMPTS = Object.freeze([
  "What happened?",
  "What surprised you?",
  "What would you change next time?",
]);
// Passed to transcription as the capture hint, so the provider knows the shape.
const DEBRIEF_HINT =
  "Session debrief: what happened, what surprised you, what would you change next time.";
const DEBRIEF_VOICE_NOTE_TYPE = "Session debrief";

/**
 * One voice debrief per session: three prompts and one record button. The
 * memo uploads as a staged voice note targeting the session
 * (capture_purpose=session_debrief, capture_channel=debrief) through the
 * offline-aware queue. It never blocks anything: Skip is one tap, and the
 * session is already closed (or stays open) whatever happens here. Drafting
 * from the memo happens in the ordinary daily review or from the note, like
 * any other voice capture.
 */
function SessionDebrief({
  token,
  ownerId = "",
  projectId,
  session,
  canWrite,
  heading = "Session debrief",
  onDone,
  queue: queueOverride = undefined,
  now = Date.now,
}) {
  const queue = useMemo(
    () => (queueOverride === undefined ? getUploadQueue() : queueOverride),
    [queueOverride]
  );
  // idle | uploading | saved | queued | failed
  const [status, setStatus] = useState("idle");
  const [error, setError] = useState("");
  const [canRetry, setCanRetry] = useState(false);
  // One recording is one capture: its file, client_capture_id, and metadata
  // (including the captured_at clock) are fixed when it is recorded, so a
  // retry after an ambiguous failure is an exact replay the server accepts.
  const captureRef = useRef(null);
  // Skip while recording throws the recording away instead of saving it.
  const discardRef = useRef(false);

  async function sendDebrief() {
    const capture = captureRef.current;
    if (!capture || !session) {
      return;
    }
    setStatus("uploading");
    setError("");
    setCanRetry(false);
    try {
      const result = await uploadOrQueueRawFile({
        token,
        projectId: capture.projectId,
        ownerId,
        fileToUpload: capture.file,
        metadata: capture.metadata,
        targets: capture.targets,
        queue,
        clientCaptureId: capture.clientCaptureId,
      });
      setStatus(result === OFFLINE_QUEUED ? "queued" : "saved");
    } catch (uploadError) {
      setStatus("failed");
      setCanRetry(true);
      setError(errorMessage(uploadError, "The debrief could not be uploaded."));
    }
  }

  function captureDebrief(file) {
    if (!file || !session) {
      return;
    }
    // A new recording is a new capture, not a replay of the last one.
    captureRef.current = {
      clientCaptureId: newCaptureId(),
      file,
      metadata: {
        ...buildCaptureMetadata({
          captureMode: "voice",
          kind: "voice",
          file,
          hint: DEBRIEF_HINT,
          voiceNoteType: DEBRIEF_VOICE_NOTE_TYPE,
          captureChannel: CAPTURE_CHANNEL.DEBRIEF,
          now,
        }),
        capture_purpose: SESSION_DEBRIEF_PURPOSE,
      },
      projectId,
      targets: sessionTargets(session.session_id),
    };
    sendDebrief();
  }

  const recorder = useAudioRecorder({
    enabled: canWrite,
    filenameBase: "session-debrief",
    onRecorded: (file) => {
      if (discardRef.current) {
        return;
      }
      captureDebrief(file);
    },
    setFlash: (_message, recorderError = "") => setError(recorderError),
  });

  if (!session) {
    return null;
  }

  const finished = status === "saved" || status === "queued";

  return (
    <section className="card-inset stack session-debrief" aria-labelledby="session-debrief-title">
      <h3 id="session-debrief-title">{heading}</h3>
      <ol className="stack">
        {DEBRIEF_PROMPTS.map((prompt) => (
          <li key={prompt}>{prompt}</li>
        ))}
      </ol>
      {finished ? (
        <p role="status">
          {status === "saved"
            ? "Debrief saved for review."
            : "Debrief queued. It uploads when this device is back online."}
        </p>
      ) : null}
      {status === "uploading" ? <p role="status">Saving the debrief…</p> : null}
      {error ? <p className="flash error">{error}</p> : null}
      <div className="inline">
        {!finished && status !== "uploading" ? (
          recorder.recordingSupported ? (
            <button
              type="button"
              className={recorder.isRecording ? "btn-danger" : "btn-primary"}
              disabled={!canWrite}
              onClick={recorder.toggleRecording}
            >
              {recorder.isRecording ? "Stop and save" : "Record debrief"}
            </button>
          ) : (
            <label className="btn-primary">
              Record debrief
              <input
                accept="audio/*"
                capture
                className="sr-only"
                disabled={!canWrite}
                onChange={(event) => {
                  const file = event.target.files?.[0] || null;
                  event.target.value = "";
                  captureDebrief(file);
                }}
                type="file"
              />
            </label>
          )
        ) : null}
        {status === "failed" && canRetry ? (
          <button type="button" className="btn-secondary" onClick={sendDebrief}>
            Retry upload
          </button>
        ) : null}
        <button
          type="button"
          className="btn-secondary"
          onClick={() => {
            if (recorder.isRecording) {
              discardRef.current = true;
              recorder.stopRecording();
            }
            onDone?.(finished ? "saved" : "skipped");
          }}
        >
          {finished ? "Done" : "Skip"}
        </button>
      </div>
    </section>
  );
}

export { DEBRIEF_PROMPTS, SessionDebrief };
