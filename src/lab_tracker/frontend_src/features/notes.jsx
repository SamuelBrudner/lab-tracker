import * as React from "react";

import { apiRequest } from "../shared/api.js";
import { noteShape } from "../shared/gateways/notes.js";
import { formatDate } from "../shared/formatters.js";
import { DraftRecoveryNotice } from "../shared/ui.jsx";
import { useApiResource } from "../hooks/useApiResource.js";
import { useLocalDraft } from "../hooks/useLocalDraft.js";
import { useProjectAccess } from "../hooks/useProjectAccess.js";

const { useEffect, useMemo, useRef, useState } = React;

function NotePanel({
  canWrite,
  busy,
  loading,
  error,
  selectedProjectId,
  noteText,
  onNoteTextChange,
  onRestoreNoteText,
  onCreateTextNote,
  onUploadNote,
  onUploadFileChange,
  uploadTargetQuestionId,
  onUploadTargetQuestionIdChange,
  uploadTranscript,
  onUploadTranscriptChange,
  activeQuestions,
  notes,
  navigate,
}) {
  const noteDraft = useLocalDraft({
    baseline: "",
    key: selectedProjectId ? `note-text:${selectedProjectId}` : "",
    value: noteText,
  });

  return (
    <article className="card span-6">
      <div className="item-head">
        <h2>Note Capture</h2>
        <div className="inline">
          <button
            type="button"
            className="btn-secondary"
            onClick={() => navigate("/app/devices")}
          >
            Paired devices
          </button>
          <button
            type="button"
            className="btn-secondary"
            onClick={() => navigate("/app/capture")}
          >
            Phone capture
          </button>
        </div>
      </div>
      <form className="form" onSubmit={onCreateTextNote}>
        <h3>Quick text note</h3>
        <DraftRecoveryNotice
          label="an unsaved note"
          savedAt={noteDraft.recoveredAt}
          onRestore={() => {
            const restored = noteDraft.restore();
            if (restored !== null && onRestoreNoteText) {
              onRestoreNoteText(restored);
            }
          }}
          onDiscard={noteDraft.discard}
        />
        <label>
          Raw note text
          <textarea
            value={noteText}
            onChange={onNoteTextChange}
            disabled={!canWrite || !selectedProjectId}
          />
        </label>
        <button className="btn-secondary" disabled={!canWrite || !selectedProjectId || busy}>
          Save text note
        </button>
      </form>

      <form className="form" onSubmit={onUploadNote}>
        <h3>File upload</h3>
        <label>
          Select file
          <input
            type="file"
            onChange={onUploadFileChange}
            disabled={!canWrite || !selectedProjectId}
          />
        </label>
        <label>
          Link to active question (optional)
          <select
            value={uploadTargetQuestionId}
            onChange={onUploadTargetQuestionIdChange}
            disabled={!canWrite || !selectedProjectId}
          >
            <option value="">No question link</option>
            {activeQuestions.map((question) => (
              <option value={question.question_id} key={question.question_id}>
                {question.text}
              </option>
            ))}
          </select>
        </label>
        <label>
          Manual transcript (optional)
          <textarea
            value={uploadTranscript}
            onChange={onUploadTranscriptChange}
            disabled={!canWrite || !selectedProjectId}
          />
        </label>
        <button className="btn-primary" disabled={!canWrite || !selectedProjectId || busy}>
          Upload note file
        </button>
      </form>

      <h3>Recent Notes</h3>
      {loading ? <p className="subtle">Loading recent notes...</p> : null}
      {error ? <p className="flash error">{error}</p> : null}
      {!loading && !error && notes.length === 0 ? (
        <p className="subtle">No recent notes for this project.</p>
      ) : null}
      <div className="stack">
        {notes.map((note) => (
          <article className="item" key={note.note_id}>
            <div className="item-head">
              <span className="pill">{note.status}</span>
              <span className="subtle">{formatDate(note.created_at)}</span>
            </div>
            <p>
              {note.transcribed_text ||
                note.raw_content ||
                note.raw_asset?.filename ||
                "(binary upload)"}
            </p>
            <p className="mono">{note.note_id}</p>
            <button
              type="button"
              className="btn-secondary"
              onClick={() => navigate(`/app/notes/${note.note_id}`)}
            >
              Open note
            </button>
          </article>
        ))}
      </div>
    </article>
  );
}

function NoteDetailCard({
  token,
  noteId,
  projects,
  navigate,
  onSetActiveProject,
  canWrite: dashboardCanWrite,
  user = null,
  setBusy,
  setFlash,
}) {
  const {
    data: note,
    error,
    loading,
    setData: setNote,
  } = useApiResource(
    noteId ? `/notes/${noteId}` : "",
    token,
    "Failed to load note.",
    { validate: noteShape }
  );
  // Key write access to the loaded note's OWN project (a direct-linked note may
  // belong to a different project than the dashboard selection); fall back to
  // the passed permission until the note's project is known.
  const noteAccess = useProjectAccess(note?.project_id, {
    token,
    user,
    enabled: Boolean(note?.project_id),
  });
  const canWrite = note?.project_id ? noteAccess.canContribute : dashboardCanWrite;
  const [imagePreview, setImagePreview] = useState("");
  const [audioPreview, setAudioPreview] = useState("");
  const [textPreview, setTextPreview] = useState(null);
  const [textPreviewError, setTextPreviewError] = useState("");
  const [transcriptText, setTranscriptText] = useState("");
  const isImage = Boolean(note?.raw_asset?.content_type?.startsWith("image/"));
  const isAudio = Boolean(note?.raw_asset?.content_type?.startsWith("audio/"));
  const isText = Boolean(note?.raw_asset?.is_text);
  // Previews are keyed by note identity so refreshing `note` after a mutation
  // does not re-download the raw asset.
  const loadedNoteId = note?.note_id || "";
  const isMemberOnboardingCheckpoint =
    note?.metadata?.member_onboarding_role === "checkpoint";
  const canDraft = Boolean(
    !isMemberOnboardingCheckpoint && (isImage || isAudio || isText || note?.raw_content)
  );

  const project = useMemo(() => {
    if (!note) {
      return null;
    }
    return projects.find((item) => item.project_id === note.project_id) || null;
  }, [projects, note]);

  useEffect(() => {
    let canceled = false;
    setImagePreview("");
    setAudioPreview("");
    setTextPreview(null);
    setTextPreviewError("");
    if (!loadedNoteId || (!isImage && !isAudio && !isText)) {
      return () => {
        canceled = true;
      };
    }
    const path = isText ? `/notes/${loadedNoteId}/raw-text` : `/notes/${loadedNoteId}/raw`;
    apiRequest(path, { token })
      .then((raw) => {
        if (!canceled && isText && typeof raw?.text === "string") {
          setTextPreview(raw);
          return;
        }
        if (!canceled && raw?.content_base64 && raw?.content_type) {
          const dataUrl = `data:${raw.content_type};base64,${raw.content_base64}`;
          if (raw.content_type.startsWith("image/")) {
            setImagePreview(dataUrl);
          }
          if (raw.content_type.startsWith("audio/")) {
            setAudioPreview(dataUrl);
          }
        }
      })
      .catch(() => {
        if (!canceled) {
          setImagePreview("");
          setAudioPreview("");
          if (isText) {
            setTextPreviewError("Text preview is unavailable.");
          }
        }
      });
    return () => {
      canceled = true;
    };
  }, [isAudio, isImage, isText, loadedNoteId, token]);

  // Sync the editor from the server copy without discarding unsaved edits. A
  // different note always resets it. For the same note, a new server
  // transcript replaces the editor text only when the editor still holds the
  // previously synced text; this effect runs after the render that adopted
  // the response, so the user may already have typed in between.
  const syncedTranscriptRef = useRef({ noteId: "", text: "" });
  const serverTranscript = note?.transcribed_text || "";
  useEffect(() => {
    const synced = syncedTranscriptRef.current;
    syncedTranscriptRef.current = { noteId: loadedNoteId, text: serverTranscript };
    if (synced.noteId !== loadedNoteId) {
      setTranscriptText(serverTranscript);
      return;
    }
    if (synced.text !== serverTranscript) {
      setTranscriptText((current) => (current === synced.text ? serverTranscript : current));
    }
  }, [loadedNoteId, serverTranscript]);

  // A mutation response is adopted only while the card still shows that note:
  // the card is reused across note routes, so a slow response for the note the
  // user just left must not replace the one now loaded. Returns whether the
  // response belongs to the routed note.
  const routedNoteIdRef = useRef(noteId);
  useEffect(() => {
    routedNoteIdRef.current = noteId;
  }, [noteId]);

  function adoptUpdatedNote(updated) {
    if (routedNoteIdRef.current !== updated.note_id) {
      return false;
    }
    setNote((current) => (current?.note_id === updated.note_id ? updated : current));
    return true;
  }

  // The transcript editor is locked while a save or transcription is in
  // flight: its response replaces the text, which would drop anything typed.
  const [transcriptLocked, setTranscriptLocked] = useState(false);

  async function saveTranscript({ silent = false } = {}) {
    if (!note || !canWrite) {
      return null;
    }
    setBusy(true);
    setTranscriptLocked(true);
    if (!silent) {
      setFlash("", "");
    }
    try {
      // PATCH replaces the whole metadata bag, so build it from the server's
      // current copy: provenance written since this page loaded (by /transcript
      // or background auto-transcription) must survive a transcript edit.
      const latest = noteShape(await apiRequest(`/notes/${note.note_id}`, { token }));
      const metadata = {
        ...(latest.metadata || {}),
        transcript_status: transcriptText.trim() ? "ready" : "pending",
        transcript_edited_at: new Date().toISOString(),
      };
      const updated = noteShape(
        await apiRequest(`/notes/${note.note_id}`, {
          body: {
            metadata,
            transcribed_text: transcriptText,
          },
          method: "PATCH",
          token,
        })
      );
      adoptUpdatedNote(updated);
      if (!silent) {
        setFlash("Transcript saved.");
      }
      return updated;
    } catch (err) {
      setFlash("", err.message || "Failed to save transcript.");
      return null;
    } finally {
      setTranscriptLocked(false);
      setBusy(false);
    }
  }

  async function handleTranscribeVoiceNote() {
    if (!note || !canWrite || !isAudio) {
      return;
    }
    setBusy(true);
    setTranscriptLocked(true);
    setFlash("", "");
    try {
      const updated = noteShape(
        await apiRequest(`/notes/${note.note_id}/transcript`, {
          body: {},
          method: "POST",
          token,
        })
      );
      // Adopt the server copy (text plus provider provenance) so later saves
      // and the draft flow's "transcript changed?" check start from it. The
      // editor was locked while the request was in flight, so nothing typed can
      // be lost by showing the new transcript in the same render; the sync
      // effect then leaves any edit made after this render alone.
      if (adoptUpdatedNote(updated)) {
        setTranscriptText(updated.transcribed_text || "");
      }
      setFlash("Voice transcript ready.");
    } catch (err) {
      setFlash("", err.message || "Failed to transcribe voice note.");
    } finally {
      setTranscriptLocked(false);
      setBusy(false);
    }
  }

  async function handleDraftGraphUpdate(mode = "graph_context") {
    if (!note || !canWrite || !canDraft) {
      return;
    }
    if (isAudio && !transcriptText.trim()) {
      setFlash("", "Transcribe or enter a transcript before drafting from voice.");
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      if (isAudio && transcriptText !== (note.transcribed_text || "")) {
        const updated = await saveTranscript({ silent: true });
        if (!updated) {
          return;
        }
      }
      const draftPath = isText
        ? `/notes/${note.note_id}/analysis-graph-drafts`
        : `/notes/${note.note_id}/graph-drafts`;
      const draft = await apiRequest(draftPath, {
        ...(isText ? {} : { body: { mode } }),
        method: "POST",
        token,
      });
      if (draft?.status === "failed") {
        setFlash("", draft.error_metadata?.message || "Graph draft failed.");
      } else {
        setFlash(
          mode === "image_only"
            ? "Image-only draft ready for review."
            : "Graph draft ready for review."
        );
      }
      if (draft?.change_set_id) {
        navigate(`/app/graph-drafts/${draft.change_set_id}`);
      }
    } catch (err) {
      setFlash("", err.message || "Failed to draft graph update.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className="card span-8">
      <div className="item-head">
        <h2>Note Detail</h2>
        {loading ? <span className="pill">Loading...</span> : null}
      </div>
      {error ? <p className="flash error">{error}</p> : null}
      {note ? (
        <div className="stack">
          <div className="inline">
            <span className="pill">{note.status}</span>
            {project ? <span className="pill">{project.name}</span> : null}
            {note.raw_asset ? <span className="pill">{note.raw_asset.content_type}</span> : null}
          </div>
          {imagePreview ? (
            <img
              className="note-image"
              src={imagePreview}
              alt={note.raw_asset?.filename || "Uploaded note"}
            />
          ) : null}
          {audioPreview ? (
            <audio className="note-audio" controls src={audioPreview} />
          ) : null}
          <div className="stack">
            <div>
              <div className="subtle">Transcribed text</div>
              <textarea
                className="transcript-editor"
                disabled={!canWrite || isMemberOnboardingCheckpoint || transcriptLocked}
                onChange={(event) => setTranscriptText(event.target.value)}
                value={transcriptText}
              />
            </div>
            <div>
              <div className="subtle">Raw content</div>
              {note.raw_content ? <p>{note.raw_content}</p> : null}
              {!note.raw_content && textPreview ? (
                <div className="stack">
                  <pre className="note-text">{textPreview.text}</pre>
                  {textPreview.truncated ? (
                    <p className="subtle">
                      Preview truncated; {textPreview.omitted_bytes} byte(s) omitted.
                    </p>
                  ) : null}
                </div>
              ) : null}
              {!note.raw_content && isText && !textPreview && !textPreviewError ? (
                <p className="subtle">Loading text preview...</p>
              ) : null}
              {textPreviewError ? <p className="flash error">{textPreviewError}</p> : null}
              {!note.raw_content && !isText ? (
                <p className="subtle">(binary upload)</p>
              ) : null}
            </div>
          </div>
          <div className="stack">
            <div className="subtle">Note ID</div>
            <div className="mono">{note.note_id}</div>
            <div className="subtle">Project ID</div>
            <div className="mono">{note.project_id}</div>
            <div className="subtle">Created</div>
            <div className="mono">{formatDate(note.created_at)}</div>
            <div className="subtle">Updated</div>
            <div className="mono">{formatDate(note.updated_at)}</div>
          </div>
        </div>
      ) : null}

      <div className="inline detail-actions">
        <button type="button" className="btn-secondary" onClick={() => navigate("/app")}>
          Back
        </button>
        {note ? (
          <button
            type="button"
            className="btn-primary"
            onClick={() => {
              onSetActiveProject(note.project_id);
              navigate("/app");
            }}
          >
            Set active project
          </button>
        ) : null}
        {note && isMemberOnboardingCheckpoint ? (
          <button
            type="button"
            className="btn-secondary"
            onClick={() => navigate(`/app/projects/${note.project_id}/onboarding`)}
          >
            Open member onboarding
          </button>
        ) : null}
        {note && isAudio && !isMemberOnboardingCheckpoint ? (
          <button
            type="button"
            className="btn-secondary"
            disabled={!canWrite}
            onClick={handleTranscribeVoiceNote}
          >
            Transcribe voice
          </button>
        ) : null}
        {note && !isMemberOnboardingCheckpoint ? (
          <button
            type="button"
            className="btn-secondary"
            disabled={!canWrite}
            onClick={() => saveTranscript()}
          >
            Save transcript
          </button>
        ) : null}
        {note && canDraft ? (
          <button
            type="button"
            className="btn-primary"
            disabled={!canWrite}
            onClick={() => handleDraftGraphUpdate("graph_context")}
          >
            Draft graph update
          </button>
        ) : null}
        {note && isImage && !isMemberOnboardingCheckpoint ? (
          <button
            type="button"
            className="btn-secondary"
            disabled={!canWrite}
            onClick={() => handleDraftGraphUpdate("image_only")}
          >
            Draft image-only
          </button>
        ) : null}
      </div>
      {note && isMemberOnboardingCheckpoint ? (
        <p className="subtle">
          This attributed onboarding checkpoint is immutable. Review its question map and
          capture progress in the member-onboarding workflow.
        </p>
      ) : null}
    </article>
  );
}

export { NoteDetailCard, NotePanel };
