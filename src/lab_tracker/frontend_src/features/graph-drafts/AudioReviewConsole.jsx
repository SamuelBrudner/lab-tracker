import * as React from "react";

// Presentational "Listen & respond" console. All media state and commands come
// from the dictation hook; the revise submit is orchestrated by the parent.
function AudioReviewConsole({
  speechStatus,
  speechSupported,
  recordingSupported,
  isRecording,
  canEditDraft,
  spokenReview,
  reviseAudio,
  reviseFeedback,
  setReviseFeedback,
  reviseAttachments,
  reviseInFlight,
  onToggleSpeech,
  onStopSpeech,
  onToggleRecording,
  onClearReviseAudio,
  onAttachmentChange,
  onRemoveAttachment,
  onRevise,
}) {
  const speechButtonLabel =
    speechStatus === "speaking"
      ? "Pause audio review"
      : speechStatus === "paused"
        ? "Resume audio review"
        : "Listen to review";
  return (
    <section className="ai-revise audio-review-console" aria-labelledby="audio-review-title">
      <div className="audio-review-heading">
        <div>
          <h3 id="audio-review-title">Listen &amp; respond</h3>
          <p className="subtle">
            Hear the summary and proposals, then speak or type corrections for the AI.
          </p>
        </div>
        {speechStatus !== "idle" ? (
          <span className="pill audio-review-status" role="status">
            {speechStatus === "paused" ? "Audio paused" : "Reading review"}
          </span>
        ) : null}
      </div>

      <div className="audio-review-primary-actions">
        {speechSupported ? (
          <button
            type="button"
            className="btn-primary"
            disabled={!spokenReview}
            onClick={onToggleSpeech}
            aria-pressed={speechStatus === "speaking"}
          >
            {speechButtonLabel}
          </button>
        ) : (
          <p className="subtle audio-review-unavailable">
            Spoken playback is unavailable in this browser; the review remains below.
          </p>
        )}
        {speechStatus !== "idle" ? (
          <button type="button" className="btn-secondary" onClick={() => onStopSpeech()}>
            Stop audio
          </button>
        ) : null}
        <button
          type="button"
          className={`btn-secondary${isRecording ? " recording" : ""}`}
          disabled={isRecording ? false : !canEditDraft || !recordingSupported}
          title={
            recordingSupported
              ? undefined
              : "Microphone recording isn't supported in this browser."
          }
          onClick={onToggleRecording}
          aria-pressed={isRecording}
        >
          {isRecording ? "Stop recording" : "Dictate feedback"}
        </button>
      </div>

      {isRecording ? (
        <p className="audio-recording-status" role="status">
          Recording feedback… tap Stop recording when you&apos;re finished.
        </p>
      ) : null}
      {reviseAudio ? (
        <div className="ai-revise-attachment audio-review-recording">
          <audio
            aria-label="Recorded feedback preview"
            controls
            src={reviseAudio.url}
            className="ai-revise-audio"
          />
          <button
            type="button"
            className="btn-link"
            onClick={onClearReviseAudio}
            disabled={!canEditDraft}
          >
            Remove voice note
          </button>
        </div>
      ) : null}

      <details className="context-details audio-review-more">
        <summary>Type feedback or attach an image</summary>
        <div className="audio-review-more-body">
          <textarea
            className="ai-revise-input"
            rows={2}
            placeholder="Tell the AI how to revise these proposals — e.g. 'drop the dataset link; the claim isn't supported yet, make it a clarification instead'. You can also dictate feedback or attach an image."
            value={reviseFeedback}
            disabled={!canEditDraft || isRecording}
            onChange={(event) => setReviseFeedback(event.target.value)}
          />
          <label className={`btn-secondary ai-revise-attach${canEditDraft ? "" : " disabled"}`}>
            Attach image
            <input
              type="file"
              accept="image/*"
              multiple
              className="sr-only"
              disabled={!canEditDraft}
              onChange={onAttachmentChange}
            />
          </label>
          {reviseAttachments.length ? (
            <ul className="ai-revise-files">
              {reviseAttachments.map((file, index) => (
                <li key={`${file.name}-${index}`} className="ai-revise-attachment">
                  <span className="ai-revise-file-name">{file.name}</span>
                  <button
                    type="button"
                    className="btn-link"
                    onClick={() => onRemoveAttachment(index)}
                    disabled={!canEditDraft}
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      </details>
      <button
        type="button"
        className="btn-primary audio-review-submit"
        disabled={
          !canEditDraft ||
          isRecording ||
          reviseInFlight ||
          (!reviseFeedback.trim() && !reviseAudio && reviseAttachments.length === 0)
        }
        onClick={onRevise}
      >
        Revise with AI
      </button>
    </section>
  );
}

export { AudioReviewConsole };
