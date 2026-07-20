import * as React from "react";

import { formatDate } from "../../shared/formatters.js";
import { hasTranscript, isAudioCapture, missingBundleTranscript } from "./capture-helpers.js";

// Presentational "Pending review" queue: recent graph drafts and mobile-capture
// notes for the project, with per-note transcribe/review affordances.
function PendingReviewList({
  pendingError,
  pendingDrafts,
  pendingNotes,
  pendingActionById,
  pendingActionErrors,
  canWrite,
  navigate,
  onTranscribe,
}) {
  return (
    <section className="capture-pending">
      <h3>Pending review</h3>
      {pendingError ? <p className="flash error">{pendingError}</p> : null}
      <div className="stack">
        {pendingDrafts.map((draft) => (
          <button
            className="review-queue-item"
            key={draft.change_set_id}
            onClick={() => navigate(`/app/graph-drafts/${draft.change_set_id}`)}
            type="button"
          >
            <span className={draft.status === "failed" ? "pill review-rejected" : "pill"}>
              {draft.status}
            </span>
            <strong>{draft.summary || draft.source_filename || "Graph draft"}</strong>
            <span className="subtle">{formatDate(draft.created_at)}</span>
          </button>
        ))}
        {pendingNotes.map((note) => {
          const action = pendingActionById[note.note_id] || "";
          const audioCapture = isAudioCapture(note);
          const transcriptReady = hasTranscript(note);
          const bundleBlocked = missingBundleTranscript(note, pendingNotes);
          return (
            <article className="review-queue-item" key={note.note_id}>
              <div className="inline">
                <span className="pill">{note.metadata?.capture_kind || "capture"}</span>
                {audioCapture ? (
                  <span className={transcriptReady ? "pill review-approved" : "pill"}>
                    {transcriptReady ? "transcript ready" : "needs transcript"}
                  </span>
                ) : null}
                {bundleBlocked && !audioCapture ? (
                  <span className="pill review-pending">voice transcript needed</span>
                ) : null}
              </div>
              <strong>{note.raw_asset?.filename || note.raw_content || "Captured note"}</strong>
              <span className="subtle">{formatDate(note.created_at)}</span>
              {transcriptReady ? <p className="source-snippet">{note.transcribed_text}</p> : null}
              {pendingActionErrors[note.note_id] ? (
                <p className="flash error">{pendingActionErrors[note.note_id]}</p>
              ) : null}
              <div className="inline">
                {audioCapture && !transcriptReady ? (
                  <button
                    className="btn-primary"
                    disabled={!canWrite || Boolean(action)}
                    onClick={() => onTranscribe(note)}
                    type="button"
                  >
                    {action === "transcribing" ? "Transcribing..." : "Transcribe"}
                  </button>
                ) : null}
                {audioCapture ? (
                  <button
                    className="btn-secondary"
                    onClick={() => navigate(`/app/notes/${note.note_id}`)}
                    type="button"
                  >
                    Review transcript
                  </button>
                ) : (
                  <button
                    className="btn-secondary"
                    onClick={() => navigate(`/app/notes/${note.note_id}`)}
                    type="button"
                  >
                    Review
                  </button>
                )}
              </div>
            </article>
          );
        })}
        {pendingDrafts.length === 0 && pendingNotes.length === 0 ? (
          <p className="subtle">No recent captures for this project.</p>
        ) : null}
      </div>
    </section>
  );
}

export { PendingReviewList };
