import * as React from "react";

import { apiListRequest, apiRequest, buildApiPath } from "../shared/api.js";
import { formatDate } from "../shared/formatters.js";
import { getUploadQueue } from "../shared/register-sw.js";
import { migrateIncomingShares } from "../shared/share-target-inbox.js";
import { UPLOAD_FILE_PATH } from "../shared/upload-queue.js";

const { useEffect, useMemo, useState } = React;

const OFFLINE_QUEUED = Symbol("offline-queued");
const INSTALL_PROMPT_DISMISSED_KEY = "lab-tracker-install-prompt-dismissed";

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

function readInstallPromptDismissed() {
  try {
    return localStorage.getItem(INSTALL_PROMPT_DISMISSED_KEY) === "true";
  } catch {
    return false;
  }
}

function rememberInstallPromptDismissed() {
  try {
    localStorage.setItem(INSTALL_PROMPT_DISMISSED_KEY, "true");
  } catch {
    // Storage may be unavailable in private browsing; session state still hides it.
  }
}

function isStandaloneApp() {
  if (typeof window === "undefined") {
    return false;
  }
  return Boolean(
    window.matchMedia?.("(display-mode: standalone)")?.matches ||
      window.navigator?.standalone
  );
}

function isPhoneSizedBrowser() {
  if (typeof window === "undefined" || typeof navigator === "undefined") {
    return false;
  }
  const ua = String(navigator.userAgent || "");
  return (
    /Android|iPhone|iPad|iPod/i.test(ua) ||
    window.matchMedia?.("(pointer: coarse)")?.matches ||
    window.innerWidth <= 760
  );
}

function MobileInstallPrompt() {
  const [dismissed, setDismissed] = useState(() => readInstallPromptDismissed());
  const [visible, setVisible] = useState(false);
  const [nativePrompt, setNativePrompt] = useState(null);
  const [showSteps, setShowSteps] = useState(false);

  useEffect(() => {
    function refreshVisibility() {
      setVisible(!readInstallPromptDismissed() && !isStandaloneApp() && isPhoneSizedBrowser());
    }

    function handleBeforeInstallPrompt(event) {
      event.preventDefault();
      setNativePrompt(event);
      refreshVisibility();
    }

    function handleInstalled() {
      rememberInstallPromptDismissed();
      setDismissed(true);
      setVisible(false);
    }

    refreshVisibility();
    window.addEventListener("resize", refreshVisibility);
    window.addEventListener("orientationchange", refreshVisibility);
    window.addEventListener("beforeinstallprompt", handleBeforeInstallPrompt);
    window.addEventListener("appinstalled", handleInstalled);
    return () => {
      window.removeEventListener("resize", refreshVisibility);
      window.removeEventListener("orientationchange", refreshVisibility);
      window.removeEventListener("beforeinstallprompt", handleBeforeInstallPrompt);
      window.removeEventListener("appinstalled", handleInstalled);
    };
  }, []);

  if (dismissed || !visible) {
    return null;
  }

  function dismiss({ remember = false } = {}) {
    if (remember) {
      rememberInstallPromptDismissed();
    }
    setDismissed(true);
    setVisible(false);
  }

  async function installOrShowSteps() {
    if (nativePrompt?.prompt) {
      nativePrompt.prompt();
      try {
        const choice = await nativePrompt.userChoice;
        if (choice?.outcome === "accepted") {
          dismiss({ remember: true });
          return;
        }
      } catch {
        // Fall back to manual steps below.
      } finally {
        setNativePrompt(null);
      }
    }
    setShowSteps(true);
  }

  return (
    <aside className="install-nudge" role="status">
      <div>
        <h3>Add Lab Tracker to this phone</h3>
        <p className="subtle">Open capture from the Home Screen instead of rescanning the QR.</p>
      </div>
      {showSteps ? (
        <ol className="install-steps">
          <li>Tap the Safari share button.</li>
          <li>Choose Add to Home Screen.</li>
          <li>Tap Add.</li>
        </ol>
      ) : null}
      <div className="install-actions">
        <button className="btn-primary" onClick={installOrShowSteps} type="button">
          Add icon
        </button>
        <button className="btn-secondary" onClick={() => dismiss()} type="button">
          Not now
        </button>
        <button
          className="btn-link"
          onClick={() => dismiss({ remember: true })}
          type="button"
        >
          Don't show again
        </button>
      </div>
    </aside>
  );
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
  const [pendingActionById, setPendingActionById] = useState({});
  const [pendingActionErrors, setPendingActionErrors] = useState({});
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

  useEffect(() => {
    // Pick up anything the OS share sheet handed off via the service worker
    // and route it through the standard offline upload queue. Runs only once
    // a project is selected so the migrated shares get attached to a real
    // project. IndexedDB-less environments (jsdom in unit tests) silently
    // no-op via the queue's null check.
    if (!selectedProjectId) {
      return undefined;
    }
    const queue = getUploadQueue();
    if (!queue) {
      return undefined;
    }
    let canceled = false;
    migrateIncomingShares({ projectId: selectedProjectId, token, uploadQueue: queue })
      .then((result) => {
        if (canceled || result.migrated === 0) {
          return undefined;
        }
        setFlash(
          result.migrated === 1
            ? "1 shared capture queued — uploading now."
            : `${result.migrated} shared captures queued — uploading now.`
        );
        return queue.drain().catch(() => undefined);
      })
      .catch(() => {
        // Migration failures shouldn't block the rest of the capture UI;
        // the shares stay in the inbox for the next attempt.
      });
    return () => {
      canceled = true;
    };
  }, [selectedProjectId, token, setFlash]);

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

  function baseMetadata({ draft, kind, bundleId = "", file = null }) {
    const metadata = {
      capture_source: "mobile_capture",
      capture_mode: captureMode,
      capture_kind: kind,
      capture_review_status: draft ? "draft_requested" : "pending_review",
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

  async function queueRawFileNoteOffline({ fileToUpload, metadata }) {
    const queue = getUploadQueue();
    if (!queue) {
      return false;
    }
    const fields = {
      project_id: selectedProjectId,
      metadata: JSON.stringify(metadata),
    };
    const targets = selectedTargets();
    if (targets.length > 0) {
      fields.targets = JSON.stringify(targets);
    }
    await queue.enqueue({
      endpoint: UPLOAD_FILE_PATH,
      file: fileToUpload,
      fields,
      token,
    });
    return true;
  }

  async function uploadOrQueueRawFile({ fileToUpload, metadata }) {
    try {
      return await uploadRawFileNote({ fileToUpload, metadata });
    } catch (err) {
      // err.status is set by apiFetch for server-rejected responses; absence
      // means the fetch itself failed (offline, DNS, CORS, etc.). Only queue
      // in that case — real validation/auth errors must surface as before.
      if (err && err.status === undefined) {
        const queued = await queueRawFileNoteOffline({ fileToUpload, metadata });
        if (queued) {
          return OFFLINE_QUEUED;
        }
      }
      throw err;
    }
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

  function setPendingAction(noteId, action) {
    setPendingActionById((current) => ({ ...current, [noteId]: action }));
    setPendingActionErrors((current) => ({ ...current, [noteId]: "" }));
  }

  function clearPendingAction(noteId) {
    setPendingActionById((current) => {
      const next = { ...current };
      delete next[noteId];
      return next;
    });
  }

  function replacePendingNote(updatedNote) {
    if (!updatedNote?.note_id) {
      return;
    }
    setPendingNotes((current) =>
      current.map((item) => (item.note_id === updatedNote.note_id ? updatedNote : item))
    );
  }

  async function transcribePendingNote(note) {
    if (!note || !canWrite || !isAudioCapture(note)) {
      return;
    }
    setPendingAction(note.note_id, "transcribing");
    setFlash("", "");
    try {
      const updated = await apiRequest(`/notes/${note.note_id}/transcript`, {
        body: captureHint(note) ? { prompt: captureHint(note) } : {},
        method: "POST",
        token,
      });
      replacePendingNote(updated);
      setFlash("Voice transcript ready.");
    } catch (err) {
      setPendingActionErrors((current) => ({
        ...current,
        [note.note_id]: err.message || "Failed to transcribe voice note.",
      }));
      setFlash("", err.message || "Failed to transcribe voice note.");
    } finally {
      clearPendingAction(note.note_id);
    }
  }

  async function draftPendingNote(note) {
    if (!note || !canWrite) {
      return;
    }
    if (isAudioCapture(note) && !hasTranscript(note)) {
      setPendingActionErrors((current) => ({
        ...current,
        [note.note_id]: "Transcribe this voice note before drafting.",
      }));
      return;
    }
    if (missingBundleTranscript(note, pendingNotes)) {
      setPendingActionErrors((current) => ({
        ...current,
        [note.note_id]: "Transcribe the bundled voice note before drafting.",
      }));
      return;
    }
    setPendingAction(note.note_id, "drafting");
    setFlash("", "");
    try {
      const graphDraft = await apiRequest(`/notes/${note.note_id}/graph-drafts`, {
        body: {
          mode: "graph_context",
          user_hint: captureHint(note) || undefined,
        },
        method: "POST",
        token,
      });
      if (graphDraft?.change_set_id) {
        navigate(`/app/graph-drafts/${graphDraft.change_set_id}`);
        setFlash("Graph-aware draft ready for review.");
      }
    } catch (err) {
      setPendingActionErrors((current) => ({
        ...current,
        [note.note_id]: err.message || "Failed to draft graph update.",
      }));
      setFlash("", err.message || "Failed to draft graph update.");
    } finally {
      clearPendingAction(note.note_id);
    }
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
      let queuedOffline = false;
      if (!noteId) {
        const bundleId = captureMode === "bundle" ? newBundleId() : "";
        let photoNote = null;
        let voiceNote = null;
        let textCapture = null;
        if (needsPhoto()) {
          const result = await uploadOrQueueRawFile({
            fileToUpload: photoFile,
            metadata: baseMetadata({ draft, kind: "image", bundleId, file: photoFile }),
          });
          if (result === OFFLINE_QUEUED) {
            queuedOffline = true;
          } else {
            photoNote = result;
            noteId = photoNote.note_id;
          }
        }
        if (needsVoice() && !queuedOffline) {
          const result = await uploadOrQueueRawFile({
            fileToUpload: audioFile,
            metadata: baseMetadata({ draft, kind: "voice", bundleId, file: audioFile }),
          });
          if (result === OFFLINE_QUEUED) {
            queuedOffline = true;
          } else {
            voiceNote = result;
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
        } else if (needsVoice() && queuedOffline) {
          await queueRawFileNoteOffline({
            fileToUpload: audioFile,
            metadata: baseMetadata({ draft, kind: "voice", bundleId, file: audioFile }),
          });
        }
        if (needsText() && !queuedOffline) {
          textCapture = await createTextCapture({ draft });
          noteId = textCapture.note_id;
        }
        if (queuedOffline) {
          setFlash("Capture queued — will upload when you're back online.");
          setPhotoFile(null);
          setAudioFile(null);
          setTextNote("");
          return;
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
      <MobileInstallPrompt />

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
            {pendingNotes.map((note) => {
              const action = pendingActionById[note.note_id] || "";
              const audioCapture = isAudioCapture(note);
              const transcriptReady = hasTranscript(note);
              const bundleBlocked = missingBundleTranscript(note, pendingNotes);
              const draftDisabled =
                !canWrite ||
                Boolean(action) ||
                (audioCapture && !transcriptReady) ||
                bundleBlocked;
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
                  {transcriptReady ? (
                    <p className="source-snippet">{note.transcribed_text}</p>
                  ) : null}
                  {pendingActionErrors[note.note_id] ? (
                    <p className="flash error">{pendingActionErrors[note.note_id]}</p>
                  ) : null}
                  <div className="inline">
                    {audioCapture && !transcriptReady ? (
                      <button
                        className="btn-primary"
                        disabled={!canWrite || Boolean(action)}
                        onClick={() => transcribePendingNote(note)}
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
                    <button
                      className="btn-secondary"
                      disabled={draftDisabled}
                      onClick={() => draftPendingNote(note)}
                      type="button"
                    >
                      {action === "drafting" ? "Drafting..." : "Draft"}
                    </button>
                  </div>
                </article>
              );
            })}
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
