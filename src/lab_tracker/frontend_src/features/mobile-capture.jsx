import * as React from "react";

import { apiListRequest, apiRequest, buildApiPath } from "../shared/api.js";
import { formatDate } from "../shared/formatters.js";

const { useEffect, useMemo, useState } = React;

function imageNotes(notes) {
  return notes.filter((note) => note.raw_asset?.content_type?.startsWith("image/"));
}

function compactLabel(value, fallback = "(untitled)") {
  const text = String(value || fallback);
  return text.length > 90 ? `${text.slice(0, 87)}...` : text;
}

function MobileCaptureCard({
  token,
  canWrite,
  projects,
  selectedProjectId,
  onSelectedProjectChange,
  questions,
  datasets,
  sessions,
  navigate,
  setBusy,
  setFlash,
  refreshProjectCounts,
  refreshRecentNotes,
}) {
  const [file, setFile] = useState(null);
  const [hint, setHint] = useState("");
  const [questionId, setQuestionId] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [datasetId, setDatasetId] = useState("");
  const [uploadedNoteId, setUploadedNoteId] = useState("");
  const [pendingDrafts, setPendingDrafts] = useState([]);
  const [pendingNotes, setPendingNotes] = useState([]);
  const [pendingError, setPendingError] = useState("");
  const activeQuestions = useMemo(
    () => questions.filter((question) => question.status === "active"),
    [questions]
  );

  useEffect(() => {
    let canceled = false;
    setPendingDrafts([]);
    setPendingNotes([]);
    setPendingError("");
    if (!selectedProjectId) {
      return () => {
        canceled = true;
      };
    }
    Promise.all([
      apiListRequest(buildApiPath("/graph-drafts", { project_id: selectedProjectId, limit: 10 }), {
        token,
      }),
      apiListRequest(buildApiPath("/notes", { project_id: selectedProjectId, limit: 10 }), {
        token,
      }),
    ])
      .then(([draftPage, notePage]) => {
        if (canceled) {
          return;
        }
        setPendingDrafts(draftPage.data || []);
        setPendingNotes(imageNotes(notePage.data || []));
      })
      .catch((err) => {
        if (!canceled) {
          setPendingError(err.message || "Unable to load pending captures.");
        }
      });
    return () => {
      canceled = true;
    };
  }, [selectedProjectId, token]);

  function selectedTargets() {
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
    return targets;
  }

  async function uploadCapture({ draft = false, imageOnly = false } = {}) {
    if (!canWrite) {
      return;
    }
    if (!selectedProjectId) {
      setFlash("", "Choose a project before capture.");
      return;
    }
    if (!file && !uploadedNoteId) {
      setFlash("", "Take or choose a photo before upload.");
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      let noteId = uploadedNoteId;
      if (!noteId) {
        const metadata = {
          capture_source: "mobile_capture",
          capture_review_status: draft ? "draft_requested" : "pending_review",
        };
        if (hint.trim()) {
          metadata.capture_hint = hint.trim();
        }
        const payload = new FormData();
        payload.append("file", file);
        payload.append("project_id", selectedProjectId);
        payload.append("metadata", JSON.stringify(metadata));
        const targets = selectedTargets();
        if (targets.length > 0) {
          payload.append("targets", JSON.stringify(targets));
        }
        const note = await apiRequest("/notes/upload-file", {
          body: payload,
          method: "POST",
          token,
        });
        noteId = note.note_id;
        setUploadedNoteId(noteId);
        await Promise.all([
          refreshProjectCounts(selectedProjectId),
          refreshRecentNotes(selectedProjectId),
        ]);
      }
      if (draft) {
        const graphDraft = await apiRequest(`/notes/${noteId}/graph-drafts`, {
          body: {
            mode: imageOnly ? "image_only" : "graph_context",
            user_hint: hint.trim() || undefined,
          },
          method: "POST",
          token,
        });
        if (graphDraft?.change_set_id) {
          navigate(`/app/graph-drafts/${graphDraft.change_set_id}`);
          setFlash(
            imageOnly ? "Image-only draft ready for review." : "Graph-aware draft ready for review."
          );
          return;
        }
      }
      setFlash("Capture saved for review.");
      setFile(null);
    } catch (err) {
      setFlash("", err.message || "Capture failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className="card span-12 capture-card">
      <div className="item-head capture-head">
        <div>
          <h2>Phone Capture</h2>
          <p className="subtle">Capture an image note now; review graph updates later.</p>
        </div>
        <button type="button" className="btn-secondary" onClick={() => navigate("/app")}>
          Workspace
        </button>
      </div>

      <div className="capture-layout">
        <form className="form capture-form" onSubmit={(event) => event.preventDefault()}>
          <label>
            Photo
            <input
              accept="image/*"
              capture="environment"
              disabled={!canWrite}
              onChange={(event) => {
                setUploadedNoteId("");
                setFile(event.target.files?.[0] || null);
              }}
              type="file"
            />
          </label>
          <label>
            Project
            <select
              disabled={!canWrite}
              onChange={(event) => onSelectedProjectChange(event.target.value)}
              value={selectedProjectId}
            >
              <option value="">Choose project</option>
              {projects.map((project) => (
                <option key={project.project_id} value={project.project_id}>
                  {project.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Active question (optional)
            <select
              disabled={!canWrite || !selectedProjectId}
              onChange={(event) => setQuestionId(event.target.value)}
              value={questionId}
            >
              <option value="">No question link</option>
              {activeQuestions.map((question) => (
                <option key={question.question_id} value={question.question_id}>
                  {compactLabel(question.text)}
                </option>
              ))}
            </select>
          </label>
          <label>
            Session (optional)
            <select
              disabled={!canWrite || !selectedProjectId}
              onChange={(event) => setSessionId(event.target.value)}
              value={sessionId}
            >
              <option value="">No session link</option>
              {sessions.map((session) => (
                <option key={session.session_id} value={session.session_id}>
                  {session.session_type} {formatDate(session.started_at)}
                </option>
              ))}
            </select>
          </label>
          <label>
            Dataset (optional)
            <select
              disabled={!canWrite || !selectedProjectId}
              onChange={(event) => setDatasetId(event.target.value)}
              value={datasetId}
            >
              <option value="">No dataset link</option>
              {datasets.map((dataset) => (
                <option key={dataset.dataset_id} value={dataset.dataset_id}>
                  {dataset.commit_hash || dataset.dataset_id}
                </option>
              ))}
            </select>
          </label>
          <label>
            Short hint (optional)
            <textarea
              disabled={!canWrite}
              onChange={(event) => setHint(event.target.value)}
              placeholder="e.g. Rig 2, Fly 12, same gradient protocol as last week"
              value={hint}
            />
          </label>
          <div className="capture-actions">
            <button
              className="btn-secondary"
              disabled={!canWrite || !selectedProjectId || (!file && !uploadedNoteId)}
              onClick={() => uploadCapture()}
              type="button"
            >
              Save for later
            </button>
            <button
              className="btn-primary"
              disabled={!canWrite || !selectedProjectId || (!file && !uploadedNoteId)}
              onClick={() => uploadCapture({ draft: true })}
              type="button"
            >
              Upload and draft
            </button>
            {uploadedNoteId ? (
              <button
                className="btn-secondary"
                disabled={!canWrite}
                onClick={() => uploadCapture({ draft: true, imageOnly: true })}
                type="button"
              >
                Draft image-only
              </button>
            ) : null}
          </div>
        </form>

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
            {pendingNotes.map((note) => (
              <button
                className="review-queue-item"
                key={note.note_id}
                onClick={() => navigate(`/app/notes/${note.note_id}`)}
                type="button"
              >
                <span className="pill">image note</span>
                <strong>{note.raw_asset?.filename || "Captured image"}</strong>
                <span className="subtle">{formatDate(note.created_at)}</span>
              </button>
            ))}
            {pendingDrafts.length === 0 && pendingNotes.length === 0 ? (
              <p className="subtle">No recent image captures for this project.</p>
            ) : null}
          </div>
        </section>
      </div>
    </article>
  );
}

export { MobileCaptureCard };
