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
import { migrateIncomingShares } from "../shared/share-target-inbox.js";
import { captureHint, captureNotes, isAudioCapture } from "../features/mobile-capture/capture-helpers.js";

const { useEffect, useMemo, useState } = React;

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

// Controller for the mobile capture surface: owns capture-composer state, the
// pending-review queue, and the upload/offline command workflow. The component
// consumes this and renders; the network/offline mechanics live in
// shared/capture-upload.js so the workflow is testable in isolation.
function useMobileCapture({
  token,
  ownerId = "",
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
      ownerId,
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
          .drain({ token, ownerId })
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
  }, [selectedProjectId, token, ownerId, setFlash]);

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
      if (returnPath) {
        navigate(returnPath);
      }
    } catch (err) {
      setFlash("", err.message || "Capture failed.");
    } finally {
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
    // pending-review queue
    pendingDrafts,
    pendingNotes,
    pendingActionById,
    pendingActionErrors,
    pendingError,
    // derived predicates
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
