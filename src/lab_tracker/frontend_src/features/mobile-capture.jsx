import * as React from "react";

import { apiListRequest, apiRequest, buildApiPath } from "../shared/api.js";
import { formatDate } from "../shared/formatters.js";
import { droppedUploadsMessage, getUploadQueue } from "../shared/register-sw.js";
import { migrateIncomingShares } from "../shared/share-target-inbox.js";
import { UPLOAD_FILE_PATH } from "../shared/upload-queue.js";

const { useEffect, useMemo, useState } = React;

const OFFLINE_QUEUED = Symbol("offline-queued");
const INSTALL_PROMPT_DISMISSED_KEY = "lab-tracker-install-prompt-dismissed";

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

function readInstallIntent() {
  try {
    return new URLSearchParams(window.location.search || "").get("install") === "1";
  } catch {
    return false;
  }
}

function readShareTargetStatus() {
  try {
    return new URLSearchParams(window.location.search || "").get("from-share") || "";
  } catch {
    return "";
  }
}

function clearShareTargetStatus() {
  try {
    const url = new URL(window.location.href);
    if (!url.searchParams.has("from-share")) {
      return;
    }
    url.searchParams.delete("from-share");
    window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
  } catch {
    // Query cleanup is cosmetic; the inbox migration still runs independently.
  }
}

function CaptureIcon({ kind }) {
  if (kind === "voice") {
    return (
      <svg aria-hidden="true" className="capture-icon" viewBox="0 0 24 24">
        <rect height="11" rx="4" width="7" x="8.5" y="3.5" />
        <path d="M5.5 11.5v1.2a6.5 6.5 0 0 0 13 0v-1.2" />
        <path d="M12 19.2v2.3" />
        <path d="M8.4 21.5h7.2" />
      </svg>
    );
  }
  if (kind === "bundle") {
    return (
      <svg aria-hidden="true" className="capture-icon" viewBox="0 0 24 24">
        <rect height="9.5" rx="2" width="11" x="3.5" y="6.5" />
        <path d="M6.5 6.5l1.2-2h2.8l1.2 2" />
        <circle cx="9" cy="11.4" r="2.4" />
        <rect height="8.5" rx="3" width="5.5" x="16" y="5" />
        <path d="M14.8 14.5a4 4 0 0 0 8 0" />
        <path d="M18.8 18.5v2" />
      </svg>
    );
  }
  if (kind === "text") {
    return (
      <svg aria-hidden="true" className="capture-icon" viewBox="0 0 24 24">
        <path d="M5 5.5h14" />
        <path d="M12 5.5v13" />
        <path d="M8 18.5h8" />
        <path d="M5.5 9h5" />
        <path d="M13.5 9h5" />
      </svg>
    );
  }
  return (
    <svg aria-hidden="true" className="capture-icon" viewBox="0 0 24 24">
      <rect height="11.5" rx="2.2" width="17" x="3.5" y="7" />
      <path d="M7.2 7l1.5-2.3h6.6L16.8 7" />
      <circle cx="12" cy="12.8" r="3.2" />
      <path d="M17.3 9.8h.1" />
    </svg>
  );
}

function MobileInstallPrompt() {
  const [dismissed, setDismissed] = useState(() => readInstallPromptDismissed());
  const [visible, setVisible] = useState(false);
  const [nativePrompt, setNativePrompt] = useState(null);
  const [showSteps, setShowSteps] = useState(false);

  useEffect(() => {
    function refreshVisibility() {
      setVisible(
        !readInstallPromptDismissed() &&
          !isStandaloneApp() &&
          (readInstallIntent() || isPhoneSizedBrowser())
      );
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
  const [captureMode, setCaptureMode] = useState("text");
  const [attachmentMenuOpen, setAttachmentMenuOpen] = useState(false);
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
  const [uploadedBundleId, setUploadedBundleId] = useState("");
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
    const status = readShareTargetStatus();
    if (!status) {
      return;
    }
    clearShareTargetStatus();
    if (status === "error") {
      setFlash("", "Shared capture could not be saved. Open Lab Tracker and try again.");
    } else if (status === "empty") {
      setFlash("", "Shared content was empty.");
    }
  }, [setFlash]);

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
    migrateIncomingShares({
      createTextNote: ({ metadata, rawContent }) =>
        apiRequest("/notes", {
          body: {
            metadata,
            project_id: selectedProjectId,
            raw_content: rawContent,
            targets: [],
          },
          method: "POST",
          token,
        }),
      projectId: selectedProjectId,
      token,
      uploadQueue: queue,
    })
      .then((result) => {
        if (canceled || result.migrated === 0) {
          return undefined;
        }
        setFlash(
          result.migrated === 1
            ? "1 shared capture imported."
            : `${result.migrated} shared captures imported.`
        );
        return queue
          .drain({ token })
          .then((drainResult) => {
            if (drainResult.dropped.length > 0) {
              setFlash("", droppedUploadsMessage(drainResult.dropped));
            }
            return drainResult;
          })
          .catch(() => undefined);
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

  function clearUploadProgress() {
    setUploadedNoteId("");
    setUploadedVoiceNoteId("");
    setUploadedBundleId("");
  }

  function chooseCaptureMode(mode) {
    setCaptureMode(mode);
    clearUploadProgress();
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

  function composerTextValue() {
    return photoFile || audioFile ? hint : textNote;
  }

  function handleComposerTextChange(event) {
    const value = event.target.value;
    clearUploadProgress();
    if (photoFile || audioFile) {
      setHint(value);
      return;
    }
    if (captureMode !== "text") {
      setCaptureMode("text");
    }
    setTextNote(value);
  }

  function handlePhotoFileChange(event) {
    const file = event.target.files?.[0] || null;
    clearUploadProgress();
    setPhotoFile(file);
    if (file) {
      if (textNote.trim() && !hint.trim()) {
        setHint(textNote.trim());
        setTextNote("");
      }
      setCaptureMode(audioFile ? "bundle" : "photo");
      setAttachmentMenuOpen(false);
    }
  }

  function handleAudioFileChange(event) {
    const file = event.target.files?.[0] || null;
    clearUploadProgress();
    setAudioFile(file);
    if (file) {
      if (textNote.trim() && !hint.trim()) {
        setHint(textNote.trim());
        setTextNote("");
      }
      setCaptureMode(photoFile ? "bundle" : "voice");
      setAttachmentMenuOpen(false);
    }
  }

  function clearPhotoFile() {
    setPhotoFile(null);
    clearUploadProgress();
    if (audioFile) {
      setCaptureMode("voice");
      return;
    }
    if (hint.trim() && !textNote.trim()) {
      setTextNote(hint.trim());
      setHint("");
    }
    setCaptureMode("text");
  }

  function clearAudioFile() {
    setAudioFile(null);
    clearUploadProgress();
    if (photoFile) {
      setCaptureMode("photo");
      return;
    }
    if (hint.trim() && !textNote.trim()) {
      setTextNote(hint.trim());
      setHint("");
    }
    setCaptureMode("text");
  }

  function startTextCapture() {
    chooseCaptureMode("text");
    setPhotoFile(null);
    setAudioFile(null);
    setAttachmentMenuOpen(false);
  }

  function startBundleCapture() {
    chooseCaptureMode("bundle");
    setAttachmentMenuOpen(false);
  }

  function readyToUpload() {
    if (!selectedProjectId) {
      return false;
    }
    return readyToCapture();
  }

  function readyToCapture() {
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
      let voiceNoteId = uploadedVoiceNoteId;
      let queuedOffline = false;
      let noteCreated = false;
      const bundleId =
        captureMode === "bundle" ? uploadedBundleId || newBundleId() : "";
      if (bundleId && !uploadedBundleId) {
        setUploadedBundleId(bundleId);
      }

      if (needsPhoto() && !noteId) {
        const result = await uploadOrQueueRawFile({
          fileToUpload: photoFile,
          metadata: baseMetadata({ draft, kind: "image", bundleId, file: photoFile }),
        });
        if (result === OFFLINE_QUEUED) {
          queuedOffline = true;
        } else {
          noteId = result.note_id;
          noteCreated = true;
          setUploadedNoteId(noteId);
        }
      }

      if (needsVoice() && !voiceNoteId && !queuedOffline) {
        const result = await uploadOrQueueRawFile({
          fileToUpload: audioFile,
          metadata: baseMetadata({ draft, kind: "voice", bundleId, file: audioFile }),
        });
        if (result === OFFLINE_QUEUED) {
          queuedOffline = true;
        } else {
          voiceNoteId = result.note_id;
          noteCreated = true;
          setUploadedVoiceNoteId(voiceNoteId);
          if (!noteId) {
            noteId = voiceNoteId;
            setUploadedNoteId(noteId);
          }
        }
      } else if (needsVoice() && !voiceNoteId && queuedOffline) {
        await queueRawFileNoteOffline({
          fileToUpload: audioFile,
          metadata: baseMetadata({ draft, kind: "voice", bundleId, file: audioFile }),
        });
      }

      if (needsText() && !noteId && !queuedOffline) {
        const textCapture = await createTextCapture({ draft });
        noteId = textCapture.note_id;
        noteCreated = true;
        setUploadedNoteId(noteId);
      }

      if (queuedOffline) {
        setFlash("Capture queued — will upload when you're back online.");
        setPhotoFile(null);
        setAudioFile(null);
        setTextNote("");
        return;
      }

      if (noteCreated) {
        await Promise.all([
          refreshProjectCounts(selectedProjectId),
          refreshRecentNotes(selectedProjectId),
        ]);
      }
      if (draft && voiceNoteId) {
        await apiRequest(`/notes/${voiceNoteId}/transcript`, {
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
      <MobileInstallPrompt />

      <div className="capture-layout">
        <form className="form capture-form" onSubmit={(event) => event.preventDefault()}>
          <section className="capture-primary" aria-labelledby="capture-primary-title">
            <div className="capture-section-head capture-section-toolbar">
              <h2 id="capture-primary-title">Capture</h2>
              <button type="button" className="btn-secondary" onClick={() => navigate("/app")}>
                Workspace
              </button>
            </div>
            <input
              accept="image/*"
              aria-label="Photo file"
              capture="environment"
              className="sr-only"
              disabled={!canWrite}
              id="capture-photo-input"
              onChange={handlePhotoFileChange}
              type="file"
            />
            <input
              accept="audio/*"
              aria-label="Voice recording"
              capture
              className="sr-only"
              disabled={!canWrite}
              id="capture-audio-input"
              onChange={handleAudioFileChange}
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
                onChange={handleComposerTextChange}
                placeholder={photoFile || audioFile ? "Add context" : "Lab note"}
                rows={1}
                value={composerTextValue()}
              />
              <label
                aria-disabled={!canWrite}
                className={`capture-composer-icon capture-composer-mic${
                  canWrite ? "" : " disabled"
                }`}
                htmlFor="capture-audio-input"
              >
                <CaptureIcon kind="voice" />
              </label>
              <button
                aria-label="Upload and draft"
                className="capture-composer-send"
                disabled={!canWrite || !readyToCapture()}
                onClick={() => uploadCapture({ draft: true })}
                title="Upload and draft"
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
                  className={`capture-attachment-option${
                    captureMode === "bundle" ? " selected" : ""
                  }`}
                  disabled={!canWrite}
                  onClick={startBundleCapture}
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
                  className={`capture-attachment-option${
                    captureMode === "text" ? " selected" : ""
                  }`}
                  disabled={!canWrite}
                  onClick={startTextCapture}
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
                  <button aria-label="Remove photo" onClick={clearPhotoFile} type="button">
                    x
                  </button>
                </span>
              ) : null}
              {audioFile ? (
                <span className="capture-attachment-chip">
                  <CaptureIcon kind="voice" />
                  <span>{audioFile.name}</span>
                  <button aria-label="Remove voice recording" onClick={clearAudioFile} type="button">
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
            {needsVoice() ? (
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
                disabled={!canWrite || !readyToCapture()}
                onClick={() => uploadCapture()}
                type="button"
              >
                Save for later
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
          </section>

          <section className="capture-context-fields" aria-labelledby="capture-context-title">
            <div className="capture-section-head">
              <h3 id="capture-context-title">Upload details</h3>
            </div>
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
          </section>
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
