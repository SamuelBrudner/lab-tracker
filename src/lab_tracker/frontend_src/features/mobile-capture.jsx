import * as React from "react";

import { apiListRequest, apiRequest, buildApiPath } from "../shared/api.js";
import { formatDate } from "../shared/formatters.js";

const { useEffect, useMemo, useState } = React;

const CAPTURE_MODES = [
  { label: "Photo", value: "photo" },
  { label: "Voice", value: "voice" },
  { label: "Photo + Voice", value: "bundle" },
  { label: "Text note", value: "text" },
];

const VOICE_NOTE_TYPES = [
  "Observation",
  "End-of-day summary",
  "Question / idea",
  "Troubleshooting",
  "Protocol note",
  "Analysis note",
  "Other",
];

function captureNotes(notes) {
  return notes.filter((note) => note.metadata?.capture_source === "mobile_capture");
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
  const [captureMode, setCaptureMode] = useState("photo");
  const [photoFile, setPhotoFile] = useState(null);
  const [audioFile, setAudioFile] = useState(null);
  const [textNote, setTextNote] = useState("");
  const [hint, setHint] = useState("");
  const [voiceNoteType, setVoiceNoteType] = useState("Observation");
  const [questionId, setQuestionId] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [datasetId, setDatasetId] = useState("");
  const [analysisId, setAnalysisId] = useState("");
  const [claimId, setClaimId] = useState("");
  const [uploadedNoteId, setUploadedNoteId] = useState("");
  const [uploadedVoiceNoteId, setUploadedVoiceNoteId] = useState("");
  const [pendingDrafts, setPendingDrafts] = useState([]);
  const [pendingNotes, setPendingNotes] = useState([]);
  const [analyses, setAnalyses] = useState([]);
  const [claims, setClaims] = useState([]);
  const [pendingError, setPendingError] = useState("");
  const activeQuestions = useMemo(
    () => questions.filter((question) => question.status === "active"),
    [questions]
  );

  useEffect(() => {
    let canceled = false;
    setPendingDrafts([]);
    setPendingNotes([]);
    setAnalyses([]);
    setClaims([]);
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
      apiListRequest(buildApiPath("/analyses", { project_id: selectedProjectId, limit: 50 }), {
        token,
      }),
      apiListRequest(buildApiPath("/claims", { project_id: selectedProjectId, limit: 50 }), {
        token,
      }),
    ])
      .then(([draftPage, notePage, analysisPage, claimPage]) => {
        if (canceled) {
          return;
        }
        setPendingDrafts(draftPage.data || []);
        setPendingNotes(captureNotes(notePage.data || []));
        setAnalyses(analysisPage.data || []);
        setClaims(claimPage.data || []);
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
    if (analysisId) {
      targets.push({ entity_id: analysisId, entity_type: "analysis" });
    }
    if (claimId) {
      targets.push({ entity_id: claimId, entity_type: "claim" });
    }
    return targets;
  }

  function needsPhoto() {
    return captureMode === "photo" || captureMode === "bundle";
  }

  function needsVoice() {
    return captureMode === "voice" || captureMode === "bundle";
  }

  function needsText() {
    return captureMode === "text";
  }

  function readyToUpload() {
    if (!selectedProjectId) {
      return false;
    }
    if (uploadedNoteId) {
      return true;
    }
    if (needsPhoto() && !photoFile && !uploadedNoteId) {
      return false;
    }
    if (needsVoice() && !audioFile) {
      return false;
    }
    if (needsText() && !textNote.trim()) {
      return false;
    }
    return true;
  }

  function newBundleId() {
    if (window.crypto?.randomUUID) {
      return window.crypto.randomUUID();
    }
    return `capture-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function baseMetadata({ draft, kind, bundleId = "" }) {
    const metadata = {
      capture_source: "mobile_capture",
      capture_mode: captureMode,
      capture_kind: kind,
      capture_review_status: draft ? "draft_requested" : "pending_review",
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

  async function uploadRawFileNote({ fileToUpload, metadata }) {
    const payload = new FormData();
    payload.append("file", fileToUpload);
    payload.append("project_id", selectedProjectId);
    payload.append("metadata", JSON.stringify(metadata));
    const targets = selectedTargets();
    if (targets.length > 0) {
      payload.append("targets", JSON.stringify(targets));
    }
    return apiRequest("/notes/upload-file", {
      body: payload,
      method: "POST",
      token,
    });
  }

  async function createTextCapture({ draft }) {
    return apiRequest("/notes", {
      body: {
        project_id: selectedProjectId,
        raw_content: textNote.trim(),
        targets: selectedTargets(),
        metadata: baseMetadata({ draft, kind: "text" }),
      },
      method: "POST",
      token,
    });
  }

  async function uploadCapture({ draft = false, imageOnly = false } = {}) {
    if (!canWrite) {
      return;
    }
    if (!selectedProjectId) {
      setFlash("", "Choose a project before capture.");
      return;
    }
    if (!readyToUpload()) {
      setFlash("", "Choose the required capture input before upload.");
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      let noteId = uploadedNoteId;
      if (!noteId) {
        const bundleId = captureMode === "bundle" ? newBundleId() : "";
        let photoNote = null;
        let voiceNote = null;
        let textCapture = null;
        if (needsPhoto()) {
          photoNote = await uploadRawFileNote({
            fileToUpload: photoFile,
            metadata: baseMetadata({ draft, kind: "image", bundleId }),
          });
          noteId = photoNote.note_id;
        }
        if (needsVoice()) {
          voiceNote = await uploadRawFileNote({
            fileToUpload: audioFile,
            metadata: baseMetadata({ draft, kind: "voice", bundleId }),
          });
          setUploadedVoiceNoteId(voiceNote.note_id);
          if (!noteId) {
            noteId = voiceNote.note_id;
          }
          if (draft) {
            voiceNote = await apiRequest(`/notes/${voiceNote.note_id}/transcript`, {
              body: hint.trim() ? { prompt: hint.trim() } : {},
              method: "POST",
              token,
            });
          }
        }
        if (needsText()) {
          textCapture = await createTextCapture({ draft });
          noteId = textCapture.note_id;
        }
        setUploadedNoteId(noteId);
        await Promise.all([
          refreshProjectCounts(selectedProjectId),
          refreshRecentNotes(selectedProjectId),
        ]);
      }
      if (draft && uploadedVoiceNoteId) {
        await apiRequest(`/notes/${uploadedVoiceNoteId}/transcript`, {
          body: hint.trim() ? { prompt: hint.trim() } : {},
          method: "POST",
          token,
        });
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
      setPhotoFile(null);
      setAudioFile(null);
      setTextNote("");
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
          <p className="subtle">Capture photo, voice, bundle, or text notes for later review.</p>
        </div>
        <button type="button" className="btn-secondary" onClick={() => navigate("/app")}>
          Workspace
        </button>
      </div>

      <div className="capture-layout">
        <form className="form capture-form" onSubmit={(event) => event.preventDefault()}>
          <fieldset className="segmented-control">
            <legend>Capture</legend>
            {CAPTURE_MODES.map((mode) => (
              <label key={mode.value}>
                <input
                  checked={captureMode === mode.value}
                  disabled={!canWrite}
                  name="capture-mode"
                  onChange={() => {
                    setCaptureMode(mode.value);
                    setUploadedNoteId("");
                    setUploadedVoiceNoteId("");
                  }}
                  type="radio"
                />
                <span>{mode.label}</span>
              </label>
            ))}
          </fieldset>
          {needsPhoto() ? (
            <label>
              Photo file
              <input
                accept="image/*"
                capture="environment"
                disabled={!canWrite}
                onChange={(event) => {
                  setUploadedNoteId("");
                  setUploadedVoiceNoteId("");
                  setPhotoFile(event.target.files?.[0] || null);
                }}
                type="file"
              />
            </label>
          ) : null}
          {needsVoice() ? (
            <>
              <label>
                Voice recording
                <input
                  accept="audio/*"
                  capture
                  disabled={!canWrite}
                  onChange={(event) => {
                    setUploadedNoteId("");
                    setUploadedVoiceNoteId("");
                    setAudioFile(event.target.files?.[0] || null);
                  }}
                  type="file"
                />
              </label>
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
            </>
          ) : null}
          {needsText() ? (
            <label>
              Text note
              <textarea
                disabled={!canWrite}
                onChange={(event) => setTextNote(event.target.value)}
                value={textNote}
              />
            </label>
          ) : null}
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
          <details className="context-details">
            <summary>More context</summary>
            <label>
              Analysis (optional)
              <select
                disabled={!canWrite || !selectedProjectId}
                onChange={(event) => setAnalysisId(event.target.value)}
                value={analysisId}
              >
                <option value="">No analysis link</option>
                {analyses.map((analysis) => (
                  <option key={analysis.analysis_id} value={analysis.analysis_id}>
                    {compactLabel(analysis.method_hash || analysis.analysis_id)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Claim (optional)
              <select
                disabled={!canWrite || !selectedProjectId}
                onChange={(event) => setClaimId(event.target.value)}
                value={claimId}
              >
                <option value="">No claim link</option>
                {claims.map((claim) => (
                  <option key={claim.claim_id} value={claim.claim_id}>
                    {compactLabel(claim.statement || claim.claim_id)}
                  </option>
                ))}
              </select>
            </label>
          </details>
          <div className="capture-actions">
            <button
              className="btn-secondary"
              disabled={!canWrite || !readyToUpload()}
              onClick={() => uploadCapture()}
              type="button"
            >
              Save for later
            </button>
            <button
              className="btn-primary"
              disabled={!canWrite || !readyToUpload()}
              onClick={() => uploadCapture({ draft: true })}
              type="button"
            >
              Upload and draft
            </button>
            {uploadedNoteId ? (
              <button
                className="btn-secondary"
                disabled={!canWrite || captureMode !== "photo"}
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
                <span className="pill">{note.metadata?.capture_kind || "capture"}</span>
                <strong>{note.raw_asset?.filename || note.raw_content || "Captured note"}</strong>
                <span className="subtle">{formatDate(note.created_at)}</span>
              </button>
            ))}
            {pendingDrafts.length === 0 && pendingNotes.length === 0 ? (
              <p className="subtle">No recent captures for this project.</p>
            ) : null}
          </div>
        </section>
      </div>
    </article>
  );
}

export { MobileCaptureCard };
