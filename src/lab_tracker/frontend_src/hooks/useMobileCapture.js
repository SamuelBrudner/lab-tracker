import * as React from "react";

import { apiListRequest, apiRequest, buildApiPath } from "../shared/api.js";
import {
  OFFLINE_QUEUED,
  buildCaptureMetadata,
  buildTargets,
  createTextCapture,
  newCaptureId,
  queueRawFileNoteOffline,
  uploadOrQueueRawFile,
} from "../shared/capture-upload.js";
import { droppedUploadsMessage, getUploadQueue } from "../shared/register-sw.js";
import {
  SHARE_INBOX_UPDATED_MESSAGE,
  createIndexedDbShareStorage,
  discardIncomingShares as discardParkedShares,
  listReviewableShares,
  migrateIncomingShares,
  shareInboxAvailable,
} from "../shared/share-target-inbox.js";
import { captureHint, captureNotes, isAudioCapture } from "../features/mobile-capture/capture-helpers.js";

const { useCallback, useEffect, useMemo, useRef, useState } = React;

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

function errorDetail(error) {
  return (error && error.message) || String(error) || "unknown error";
}

// Controller for the mobile capture surface: owns capture-composer state, the
// pending-review queue, and the upload/offline command workflow. The component
// consumes this and renders; the network/offline mechanics live in
// shared/capture-upload.js so the workflow is testable in isolation.
function useMobileCapture({
  token,
  ownerId = "",
  authEnabled = true,
  canWrite,
  selectedProjectId,
  questions,
  navigate,
  setBusy,
  setFlash,
  refreshProjectCounts,
  refreshRecentNotes,
  lockedCheckpointNoteId = "",
  returnPath = "",
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
  // A capture save spans several awaited requests; `uploading` disables the
  // composer actions, and the ref closes the window before React re-renders
  // (two taps delivered to the same render closure).
  const [uploading, setUploading] = useState(false);
  const uploadInFlightRef = useRef(false);
  // OS share-sheet items parked by the service worker. Anyone's web page can
  // POST to the share target, so they are only listed for review here and
  // imported when the user explicitly confirms (importIncomingShares).
  const shareStorage = useMemo(
    () => (shareInboxAvailable() ? createIndexedDbShareStorage() : null),
    []
  );
  const [incomingShares, setIncomingShares] = useState([]);
  const [sharesBusy, setSharesBusy] = useState(false);
  const shareActionInFlightRef = useRef(false);
  // Inbox reads can overlap (mount, visibility, worker message, post-action);
  // only the latest one may set the listed shares.
  const shareReadSeqRef = useRef(0);
  const mountedRef = useRef(false);
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
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

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
    } else if (status === "rejected") {
      setFlash(
        "",
        "A share sent from another website was blocked. " +
          "Only your device's share sheet can send items to Lab Tracker."
      );
    } else if (status === "full") {
      setFlash(
        "",
        "The shared item was not saved: the share inbox is full. " +
          "Import or discard the shared items waiting for review, then share again."
      );
    } else if (status === "too-large") {
      setFlash(
        "",
        "The shared item was not saved: it is larger than the share inbox accepts. " +
          "Add it from the capture page instead."
      );
    }
  }, [setFlash]);

  const reloadIncomingShares = useCallback(async () => {
    if (!shareStorage) {
      return;
    }
    const readSeq = shareReadSeqRef.current + 1;
    shareReadSeqRef.current = readSeq;
    const shares = await listReviewableShares({ storage: shareStorage });
    if (mountedRef.current && shareReadSeqRef.current === readSeq) {
      setIncomingShares(shares);
    }
  }, [shareStorage]);

  const reportShareInboxReadFailure = useCallback(
    (error) => {
      // eslint-disable-next-line no-console
      console.error("Shared capture inbox could not be read:", error);
      if (mountedRef.current) {
        setFlash("", `Shared items could not be loaded for review: ${errorDetail(error)}.`);
      }
    },
    [setFlash]
  );

  useEffect(() => {
    // List (never import) whatever the OS share sheet handed off via the
    // service worker. IndexedDB-less environments have no inbox to read.
    reloadIncomingShares().catch(reportShareInboxReadFailure);
  }, [reloadIncomingShares, reportShareInboxReadFailure]);

  useEffect(() => {
    // The service worker can park a share while this page stays open (e.g. a
    // share sheet launched from another app): re-read the inbox when the
    // worker says so, and whenever the page becomes visible again.
    if (!shareStorage) {
      return undefined;
    }
    const refresh = () => {
      reloadIncomingShares().catch(reportShareInboxReadFailure);
    };
    const handleVisibilityChange = () => {
      if (document.visibilityState === "visible") {
        refresh();
      }
    };
    const handleWorkerMessage = (event) => {
      if (event.data?.type === SHARE_INBOX_UPDATED_MESSAGE) {
        refresh();
      }
    };
    const serviceWorker = typeof navigator === "undefined" ? undefined : navigator.serviceWorker;
    document.addEventListener("visibilitychange", handleVisibilityChange);
    serviceWorker?.addEventListener("message", handleWorkerMessage);
    return () => {
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      serviceWorker?.removeEventListener("message", handleWorkerMessage);
    };
  }, [reloadIncomingShares, reportShareInboxReadFailure, shareStorage]);

  async function refreshImportedProject(projectId) {
    try {
      await Promise.all([refreshProjectCounts(projectId), refreshRecentNotes(projectId)]);
    } catch (error) {
      // eslint-disable-next-line no-console
      console.error("Project refresh after shared capture import failed:", error);
      if (mountedRef.current) {
        setFlash(
          "",
          "Shared captures were imported, but the project view could not be refreshed: " +
            `${errorDetail(error)}.`
        );
      }
    }
  }

  function drainImportedShares(queue) {
    return queue
      .drain({ token, ownerId, authEnabled })
      .then((drainResult) => {
        if (drainResult.dropped.length > 0 && mountedRef.current) {
          setFlash("", droppedUploadsMessage(drainResult.dropped));
        }
        return drainResult;
      })
      .catch((error) => {
        // The imported captures stay queued for the next online/boot
        // retry; make the failure visible rather than silently holding them.
        // eslint-disable-next-line no-console
        console.error("Shared capture upload failed:", error);
        if (mountedRef.current) {
          setFlash(
            "",
            `Shared captures were imported but could not be uploaded yet: ${errorDetail(error)}. ` +
              "They stay queued and will retry when you're back online."
          );
        }
      });
  }

  async function runShareAction(action) {
    shareActionInFlightRef.current = true;
    setSharesBusy(true);
    try {
      await action();
    } finally {
      shareActionInFlightRef.current = false;
      if (mountedRef.current) {
        setSharesBusy(false);
      }
      await reloadIncomingShares().catch(reportShareInboxReadFailure);
    }
  }

  // Imports exactly the shares currently shown for review into the selected
  // project. Only ever called from an explicit user action.
  async function importIncomingShares() {
    if (shareActionInFlightRef.current || !canWrite || incomingShares.length === 0) {
      return;
    }
    if (!selectedProjectId) {
      setFlash("", "Choose a project before importing shared items.");
      return;
    }
    const queue = getUploadQueue();
    if (!queue || !shareStorage) {
      setFlash("", "Shared items cannot be imported: this browser has no offline upload storage.");
      return;
    }
    const projectId = selectedProjectId;
    const shareIds = incomingShares.map((share) => share.id);
    setFlash("", "");
    await runShareAction(async () => {
      let result;
      try {
        result = await migrateIncomingShares({
          createTextNote: ({ metadata, rawContent }) =>
            apiRequest("/notes", {
              body: {
                metadata,
                project_id: projectId,
                raw_content: rawContent,
                targets: [],
              },
              method: "POST",
              token,
            }),
          projectId,
          ownerId,
          shareIds,
          storage: shareStorage,
          uploadQueue: queue,
        });
      } catch (error) {
        // eslint-disable-next-line no-console
        console.error("Shared capture import failed:", error);
        if (mountedRef.current) {
          setFlash(
            "",
            `Shared captures could not be imported: ${errorDetail(error)}. ` +
              "Shares not yet imported stay in the share inbox for review."
          );
        }
        // The import can fail partway, after earlier shares were already
        // queued: upload those now instead of holding them until the next
        // online/boot drain.
        await drainImportedShares(queue);
        return;
      }
      if (result.migrated === 0) {
        return;
      }
      if (mountedRef.current) {
        setFlash(
          result.migrated === 1
            ? "1 shared capture imported."
            : `${result.migrated} shared captures imported.`
        );
      }
      await drainImportedShares(queue);
      await refreshImportedProject(projectId);
    });
  }

  async function discardIncomingShares() {
    if (shareActionInFlightRef.current || !shareStorage || incomingShares.length === 0) {
      return;
    }
    const shareIds = incomingShares.map((share) => share.id);
    await runShareAction(async () => {
      try {
        const { discarded } = await discardParkedShares({ shareIds, storage: shareStorage });
        if (mountedRef.current) {
          setFlash(
            discarded === 1 ? "1 shared item discarded." : `${discarded} shared items discarded.`
          );
        }
      } catch (error) {
        // eslint-disable-next-line no-console
        console.error("Shared capture discard failed:", error);
        if (mountedRef.current) {
          setFlash("", `Shared items could not be discarded: ${errorDetail(error)}.`);
        }
      }
    });
  }

  function currentTargets() {
    return buildTargets({
      questionId,
      sessionId,
      datasetId,
      analysisId,
      claimId,
      noteId: lockedCheckpointNoteId,
    });
  }

  function captureMetadata({ kind, bundleId = "", file = null }) {
    // The checkpoint relationship is a retained note target, not client-authored
    // onboarding metadata. `member_onboarding_*` keys are server-reserved.
    return buildCaptureMetadata({ captureMode, kind, bundleId, file, hint, voiceNoteType });
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

  function readyToUpload() {
    if (!selectedProjectId) {
      return false;
    }
    return readyToCapture();
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

  async function uploadCapture() {
    if (uploadInFlightRef.current || !canWrite) {
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
    uploadInFlightRef.current = true;
    setUploading(true);
    setBusy(true);
    setFlash("", "");
    try {
      let noteId = uploadedNoteId;
      let voiceNoteId = uploadedVoiceNoteId;
      let queuedOffline = false;
      let noteCreated = false;
      const bundleId = captureMode === "bundle" ? uploadedBundleId || newCaptureId() : "";
      if (bundleId && !uploadedBundleId) {
        setUploadedBundleId(bundleId);
      }

      if (needsPhoto() && !noteId) {
        const result = await uploadOrQueueRawFile({
          token,
          projectId: selectedProjectId,
          ownerId,
          fileToUpload: photoFile,
          metadata: captureMetadata({ kind: "image", bundleId, file: photoFile }),
          targets: currentTargets(),
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
          token,
          projectId: selectedProjectId,
          ownerId,
          fileToUpload: audioFile,
          metadata: captureMetadata({ kind: "voice", bundleId, file: audioFile }),
          targets: currentTargets(),
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
          ownerId,
          projectId: selectedProjectId,
          fileToUpload: audioFile,
          metadata: captureMetadata({ kind: "voice", bundleId, file: audioFile }),
          targets: currentTargets(),
        });
      }

      if (needsText() && !noteId && !queuedOffline) {
        const textCapture = await createTextCapture({
          token,
          projectId: selectedProjectId,
          rawContent: textNote.trim(),
          targets: currentTargets(),
          metadata: captureMetadata({ kind: "text" }),
        });
        noteId = textCapture.note_id;
        noteCreated = true;
        setUploadedNoteId(noteId);
      }

      if (queuedOffline) {
        setFlash("Capture queued — will upload when you're back online.");
        setPhotoFile(null);
        setAudioFile(null);
        setTextNote("");
        clearUploadProgress();
        if (returnPath) {
          navigate(returnPath);
        }
        return;
      }

      if (noteCreated) {
        await Promise.all([
          refreshProjectCounts(selectedProjectId),
          refreshRecentNotes(selectedProjectId),
        ]);
      }
      setFlash("Capture saved for review.");
      setPhotoFile(null);
      setAudioFile(null);
      setTextNote("");
      // The composer is reset, so the finished capture's ids must not keep
      // readyToCapture() true and let an empty Save report another success.
      clearUploadProgress();
      if (returnPath) {
        navigate(returnPath);
      }
    } catch (err) {
      setFlash("", err.message || "Capture failed.");
    } finally {
      uploadInFlightRef.current = false;
      setUploading(false);
      setBusy(false);
    }
  }

  return {
    // capture-composer state
    captureMode,
    attachmentMenuOpen,
    setAttachmentMenuOpen,
    photoFile,
    audioFile,
    hint,
    setHint,
    voiceNoteType,
    setVoiceNoteType,
    // context-field selections
    questionId,
    setQuestionId,
    sessionId,
    setSessionId,
    datasetId,
    setDatasetId,
    analysisId,
    setAnalysisId,
    claimId,
    setClaimId,
    activeQuestions,
    analyses,
    claims,
    // OS share-sheet items awaiting explicit review
    incomingShares,
    sharesBusy,
    importIncomingShares,
    discardIncomingShares,
    // pending-review queue
    pendingDrafts,
    pendingNotes,
    pendingActionById,
    pendingActionErrors,
    pendingError,
    // derived predicates
    uploading,
    composerTextValue,
    needsVoice,
    readyToCapture,
    // commands
    handleComposerTextChange,
    handlePhotoFileChange,
    handleAudioFileChange,
    clearPhotoFile,
    clearAudioFile,
    startTextCapture,
    startBundleCapture,
    uploadCapture,
    transcribePendingNote,
    navigate,
  };
}

export { useMobileCapture };
