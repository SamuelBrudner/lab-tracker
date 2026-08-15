import * as React from "react";

import { CaptureIcon } from "./CaptureIcon.jsx";

const VOICE_NOTE_TYPES = [
  "Observation",
  "End-of-day summary",
  "Question / idea",
  "Troubleshooting",
  "Protocol note",
  "Analysis note",
  "Other",
];

// Presentational capture composer: the hidden file inputs, the message/hint
// textarea, the attachment menu + strip, the voice-note-type selector, and the
// send / save-for-later actions. All state and handlers come from the
// controller hook.
function CaptureComposer({
  canWrite,
  navigate,
  returnPath = "",
  attachmentMenuOpen,
  setAttachmentMenuOpen,
  photoFile,
  audioFile,
  captureMode,
  composerTextValue,
  onComposerTextChange,
  onPhotoFileChange,
  onAudioFileChange,
  onClearPhotoFile,
  onClearAudioFile,
  onStartTextCapture,
  onStartBundleCapture,
  readyToCapture,
  needsVoice,
  voiceNoteType,
  setVoiceNoteType,
  onUploadCapture,
}) {
  return (
    <section className="capture-primary" aria-labelledby="capture-primary-title">
      <div className="capture-section-head capture-section-toolbar">
        <h2 id="capture-primary-title">Capture</h2>
        <button type="button" className="btn-secondary" onClick={() => navigate(returnPath || "/app")}>
          {returnPath ? "Back to orientation" : "Workspace"}
        </button>
      </div>
      <input
        accept="image/*"
        aria-label="Photo file"
        className="sr-only"
        disabled={!canWrite}
        id="capture-photo-input"
        onChange={onPhotoFileChange}
        type="file"
      />
      <input
        accept="audio/*"
        aria-label="Voice recording"
        className="sr-only"
        disabled={!canWrite}
        id="capture-audio-input"
        onChange={onAudioFileChange}
        type="file"
      />
      <input
        accept="audio/*"
        aria-label="Record voice note"
        capture
        className="sr-only"
        disabled={!canWrite}
        id="capture-audio-record-input"
        onChange={onAudioFileChange}
        type="file"
      />
      <div className="capture-composer">
        <button
          aria-controls="capture-attachment-menu"
          aria-expanded={attachmentMenuOpen}
          aria-label="Add attachment"
          className="capture-composer-icon"
          disabled={!canWrite}
          onClick={() => setAttachmentMenuOpen((current) => !current)}
          type="button"
        >
          <span aria-hidden="true">+</span>
        </button>
        <label className="sr-only" htmlFor="capture-composer-text">
          Message or hint
        </label>
        <textarea
          className="capture-composer-input"
          disabled={!canWrite}
          id="capture-composer-text"
          onChange={onComposerTextChange}
          placeholder={photoFile || audioFile ? "Add context" : "Lab note"}
          rows={1}
          value={composerTextValue}
        />
        <label
          aria-disabled={!canWrite}
          className={`capture-composer-icon capture-composer-mic${canWrite ? "" : " disabled"}`}
          htmlFor="capture-audio-record-input"
        >
          <CaptureIcon kind="voice" />
        </label>
        <button
          aria-label="Save capture"
          className="capture-composer-send"
          disabled={!canWrite || !readyToCapture}
          onClick={() => onUploadCapture()}
          title="Save capture"
          type="button"
        >
          <svg aria-hidden="true" viewBox="0 0 24 24">
            <path d="M5 12h13" />
            <path d="m13 6 6 6-6 6" />
          </svg>
        </button>
      </div>
      {attachmentMenuOpen ? (
        <div
          aria-label="Attachment options"
          className="capture-attachment-menu"
          id="capture-attachment-menu"
        >
          <label className="capture-attachment-option" htmlFor="capture-photo-input">
            <CaptureIcon kind="photo" />
            <span>
              <strong>Photo or camera</strong>
              <small>{photoFile?.name || "Image file"}</small>
            </span>
          </label>
          <label className="capture-attachment-option" htmlFor="capture-audio-input">
            <CaptureIcon kind="voice" />
            <span>
              <strong>Voice file</strong>
              <small>{audioFile?.name || "Audio file"}</small>
            </span>
          </label>
          <button
            aria-label="Photo + voice"
            className={`capture-attachment-option${captureMode === "bundle" ? " selected" : ""}`}
            disabled={!canWrite}
            onClick={onStartBundleCapture}
            type="button"
          >
            <CaptureIcon kind="bundle" />
            <span>
              <strong>Photo + voice</strong>
              <small>Bundle</small>
            </span>
          </button>
          <button
            aria-label="Text note"
            className={`capture-attachment-option${captureMode === "text" ? " selected" : ""}`}
            disabled={!canWrite}
            onClick={onStartTextCapture}
            type="button"
          >
            <CaptureIcon kind="text" />
            <span>
              <strong>Text note</strong>
              <small>Typed</small>
            </span>
          </button>
        </div>
      ) : null}
      <div className="capture-attachment-strip" aria-live="polite">
        {photoFile ? (
          <span className="capture-attachment-chip">
            <CaptureIcon kind="photo" />
            <span>{photoFile.name}</span>
            <button aria-label="Remove photo" onClick={onClearPhotoFile} type="button">
              x
            </button>
          </span>
        ) : null}
        {audioFile ? (
          <span className="capture-attachment-chip">
            <CaptureIcon kind="voice" />
            <span>{audioFile.name}</span>
            <button aria-label="Remove voice recording" onClick={onClearAudioFile} type="button">
              x
            </button>
          </span>
        ) : null}
        {captureMode === "bundle" && !photoFile ? (
          <label className="capture-attachment-chip missing" htmlFor="capture-photo-input">
            <CaptureIcon kind="photo" />
            <span>Photo needed</span>
          </label>
        ) : null}
        {captureMode === "bundle" && !audioFile ? (
          <label className="capture-attachment-chip missing" htmlFor="capture-audio-input">
            <CaptureIcon kind="voice" />
            <span>Voice needed</span>
          </label>
        ) : null}
      </div>
      {needsVoice ? (
        <label>
          Voice note type
          <select
            disabled={!canWrite}
            onChange={(event) => setVoiceNoteType(event.target.value)}
            value={voiceNoteType}
          >
            {VOICE_NOTE_TYPES.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </label>
      ) : null}
      <div className="capture-actions">
        <button
          className="btn-secondary"
          disabled={!canWrite || !readyToCapture}
          onClick={() => onUploadCapture()}
          type="button"
        >
          Save for later
        </button>
      </div>
    </section>
  );
}

export { CaptureComposer };
