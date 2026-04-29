import * as React from "react";

import { apiRequest } from "../shared/api.js";
import { formatDate } from "../shared/formatters.js";
import { useApiResource } from "../hooks/useApiResource.js";

const { useEffect, useMemo, useState } = React;

function NotePanel({
  canWrite,
  busy,
  loading,
  error,
  selectedProjectId,
  noteText,
  onNoteTextChange,
  onCreateTextNote,
  onUploadNote,
  onUploadFileChange,
  uploadTargetQuestionId,
  onUploadTargetQuestionIdChange,
  uploadTranscript,
  onUploadTranscriptChange,
  activeQuestions,
  notes,
}) {
  return (
    <article className="card span-6">
      <h2>Note Capture</h2>
      <form className="form" onSubmit={onCreateTextNote}>
        <h3>Quick text note</h3>
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
            <p>{note.transcribed_text || note.raw_content || "(binary upload)"}</p>
            <p className="mono">{note.note_id}</p>
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
  canWrite,
  setBusy,
  setFlash,
}) {
  const { data: note, error, loading } = useApiResource(
    noteId ? `/notes/${noteId}` : "",
    token,
    "Failed to load note."
  );
  const [imagePreview, setImagePreview] = useState("");
  const isImage = Boolean(note?.raw_asset?.content_type?.startsWith("image/"));

  const project = useMemo(() => {
    if (!note) {
      return null;
    }
    return projects.find((item) => item.project_id === note.project_id) || null;
  }, [projects, note]);

  useEffect(() => {
    let canceled = false;
    setImagePreview("");
    if (!note || !isImage) {
      return () => {
        canceled = true;
      };
    }
    apiRequest(`/notes/${note.note_id}/raw`, { token })
      .then((raw) => {
        if (!canceled && raw?.content_base64 && raw?.content_type) {
          setImagePreview(`data:${raw.content_type};base64,${raw.content_base64}`);
        }
      })
      .catch(() => {
        if (!canceled) {
          setImagePreview("");
        }
      });
    return () => {
      canceled = true;
    };
  }, [isImage, note, token]);

  async function handleDraftGraphUpdate() {
    if (!note || !canWrite || !isImage) {
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      const draft = await apiRequest(`/notes/${note.note_id}/graph-drafts`, {
        method: "POST",
        token,
      });
      if (draft?.status === "failed") {
        setFlash("", draft.error_metadata?.message || "Graph draft failed.");
      } else {
        setFlash("Graph draft ready for review.");
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
          <div className="stack">
            <div>
              <div className="subtle">Transcribed text</div>
              <p>{note.transcribed_text || <span className="subtle">(none)</span>}</p>
            </div>
            <div>
              <div className="subtle">Raw content</div>
              <p>{note.raw_content || <span className="subtle">(binary upload)</span>}</p>
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
        {note && isImage ? (
          <button
            type="button"
            className="btn-primary"
            disabled={!canWrite}
            onClick={handleDraftGraphUpdate}
          >
            Draft graph update
          </button>
        ) : null}
      </div>
    </article>
  );
}

export { NoteDetailCard, NotePanel };
