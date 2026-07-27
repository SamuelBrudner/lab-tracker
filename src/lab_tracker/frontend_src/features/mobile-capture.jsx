import * as React from "react";

import { apiListRequest, apiRequest, buildApiPath } from "../shared/api.js";
import { formatDate } from "../shared/formatters.js";
import { droppedUploadsMessage, getUploadQueue } from "../shared/register-sw.js";
import { migrateIncomingShares } from "../shared/share-target-inbox.js";
import { UPLOAD_FILE_PATH } from "../shared/upload-queue.js";
import { DraftRecoveryNotice } from "../shared/ui.jsx";
import { useLocalDraft } from "../hooks/useLocalDraft.js";

const { useEffect, useMemo, useRef, useState } = React;

const OFFLINE_QUEUED = Symbol("offline-queued");
const INSTALL_PROMPT_DISMISSED_KEY = "lab-tracker-install-prompt-dismissed";
const TAG_LOG_STORAGE_PREFIX = "lab-tracker:tag-log:";
const TAG_LOG_DEDUPE_MS = 120000;
const UNBOUND_TAG_MESSAGE = "This tag isn't bound yet — capture works as usual.";
const BENCH_CHECKIN_STORAGE_KEY = "lab-tracker:bench-checkin";
const BENCH_CHECKIN_DEFAULT_TTL_HOURS = 8;
const BENCH_CHECKIN_MIN_TTL_HOURS = 1;
const BENCH_CHECKIN_MAX_TTL_HOURS = 24;
const HOUR_MS = 3600000;

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

function readCaptureUrlParams() {
  // Unknown params are ignored so stale tag URLs keep working.
  try {
    const params = new URLSearchParams(window.location.search || "");
    return {
      checkin: params.get("checkin") === "1",
      hint: params.get("hint") || "",
      log: params.get("log") === "1",
      mode: params.get("mode") || "",
      projectId: params.get("project") || "",
      sessionLink: params.get("session-link") || "",
      tagSlug: params.get("tag") || "",
      tagStatus: params.get("tag-status") || "",
      ttlHours: params.get("ttl-hours") || "",
    };
  } catch {
    return {
      checkin: false,
      hint: "",
      log: false,
      mode: "",
      projectId: "",
      sessionLink: "",
      tagSlug: "",
      tagStatus: "",
      ttlHours: "",
    };
  }
}

function clampCheckinTtlHours(value) {
  const hours = Number(value);
  if (!Number.isFinite(hours)) {
    return BENCH_CHECKIN_DEFAULT_TTL_HOURS;
  }
  return Math.min(BENCH_CHECKIN_MAX_TTL_HOURS, Math.max(BENCH_CHECKIN_MIN_TTL_HOURS, hours));
}

function readBenchCheckin() {
  // Read path for the TTL sticky context; an expired or malformed entry is
  // deleted so the next mount reverts to ask-on-arrival.
  try {
    const raw = localStorage.getItem(BENCH_CHECKIN_STORAGE_KEY);
    if (!raw) {
      return null;
    }
    const entry = JSON.parse(raw);
    if (
      !entry ||
      typeof entry !== "object" ||
      !entry.projectId ||
      !Number.isFinite(entry.expiresAt) ||
      entry.expiresAt <= Date.now()
    ) {
      localStorage.removeItem(BENCH_CHECKIN_STORAGE_KEY);
      return null;
    }
    return {
      expiresAt: entry.expiresAt,
      hint: String(entry.hint || ""),
      label: String(entry.label || ""),
      projectId: String(entry.projectId),
      sessionId: String(entry.sessionId || ""),
    };
  } catch {
    return null;
  }
}

function writeBenchCheckin(entry) {
  try {
    localStorage.setItem(BENCH_CHECKIN_STORAGE_KEY, JSON.stringify(entry));
  } catch {
    // Storage may be unavailable in private browsing; the in-memory banner
    // still reflects the check-in for this visit.
  }
}

function clearBenchCheckin() {
  try {
    localStorage.removeItem(BENCH_CHECKIN_STORAGE_KEY);
  } catch {
    // Ignore storage failures; an unreadable entry is dropped on read.
  }
}

function checkinTimeLabel(expiresAt) {
  try {
    return new Date(expiresAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  } catch {
    return "";
  }
}

function shouldFireTagBreadcrumb(slug) {
  const key = `${TAG_LOG_STORAGE_PREFIX}${slug}`;
  try {
    const lastFired = Number(sessionStorage.getItem(key));
    if (Number.isFinite(lastFired) && Date.now() - lastFired < TAG_LOG_DEDUPE_MS) {
      return false;
    }
    sessionStorage.setItem(key, String(Date.now()));
    return true;
  } catch {
    // Storage may be unavailable in private browsing; fire without dedupe.
    return true;
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
  // Both fields are persisted, not just the visible one: attaching a photo
  // switches the composer from textNote to hint, and storing only what is on
  // screen would overwrite a long lab note with a short hint.
  const captureDraft = useLocalDraft({
    baseline: JSON.stringify(["", ""]),
    key: selectedProjectId ? `capture-text:${selectedProjectId}` : "",
    value: JSON.stringify([textNote, hint]),
  });
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
  // Set once from the mount-time URL params; feeds capture_entry / tag_slug /
  // tag_label into baseMetadata. Stays null on a param-less mount so metadata
  // is byte-identical to a plain visit.
  const [urlCaptureContext, setUrlCaptureContext] = useState(null);
  const [pendingBreadcrumb, setPendingBreadcrumb] = useState(null);
  // Voice-first layout (?mode=voice or a binding with mode "voice"): one big
  // record control on top; the normal composer stays fully functional below.
  const [voiceFirst, setVoiceFirst] = useState(false);
  const [benchCheckin, setBenchCheckin] = useState(null);
  // Flips true once the mount-time URL/check-in context has been applied (or
  // there was none). Gates the share-inbox migration so shared captures land
  // in the checked-in / tag-bound project, never the stale mount-time value.
  const [captureContextReady, setCaptureContextReady] = useState(false);
  // True while uploadCapture is in flight; disables the save controls so a
  // second tap cannot start a duplicate upload.
  const [uploading, setUploading] = useState(false);
  const urlParamsAppliedRef = useRef(false);
  // Unmount guard for the URL-param effect's async continuation. The effect
  // legitimately re-runs when its own project apply changes
  // selectedProjectId, so the flag is reset at the top of every run and only
  // a final unmount leaves it set.
  const urlParamsCanceledRef = useRef(false);
  const breadcrumbFiredRef = useRef(false);
  // Holds the recording that landed while the voice-first layout was active.
  // Keyed to the file (not a boolean) so the auto-save decision is made
  // exactly once, in the commit where that file reaches state.
  const autoUploadFileRef = useRef(null);
  // Synchronous re-entry guard for uploadCapture: a ref (not state) so a tap
  // landing while an upload is already in flight is rejected before React
  // re-renders the disabled buttons.
  const uploadInFlightRef = useRef(false);
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
    // project, and only after the mount-time URL/check-in context has been
    // applied so the first migration run targets the checked-in / tag-bound
    // project rather than the stale localStorage selection. IndexedDB-less
    // environments (jsdom in unit tests) silently no-op via the queue's null
    // check.
    if (!captureContextReady) {
      return undefined;
    }
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
  }, [captureContextReady, selectedProjectId, token, setFlash]);

  useEffect(() => {
    // Tag/QR entry: apply capture URL params exactly once per mount. Params
    // stay in the URL so a refresh re-applies them. Order: parse -> apply
    // check-in defaults -> resolve tag binding -> merge (explicit param beats
    // binding field; both beat check-in) -> apply project -> resolve
    // session-link -> set hint -> voice mode -> persist check-in ->
    // breadcrumb.
    //
    // The async continuation below must never apply state after unmount:
    // onSelectedProjectChange is an app-wide setter that outlives this card,
    // so an un-canceled continuation would flip the workspace project after
    // the user navigated away. Cancellation is keyed to unmount alone (see
    // urlParamsCanceledRef): the effect re-runs — without cancelling the
    // continuation — when its own project apply changes selectedProjectId.
    urlParamsCanceledRef.current = false;
    const cancelOnCleanup = () => {
      urlParamsCanceledRef.current = true;
    };
    if (urlParamsAppliedRef.current) {
      return cancelOnCleanup;
    }
    urlParamsAppliedRef.current = true;
    const params = readCaptureUrlParams();
    const hasPrefillParams = Boolean(
      params.projectId ||
        params.hint ||
        params.sessionLink ||
        params.log ||
        params.checkin ||
        params.mode === "voice"
    );
    if (params.tagStatus === "unbound") {
      setFlash("", UNBOUND_TAG_MESSAGE);
    }
    // Bench check-in defaults apply first so explicit params and tag
    // bindings override them field by field below. The hint box is always
    // empty at mount, so the check-in hint never clobbers anything typed;
    // the merge below only replaces the hint when it still holds the
    // check-in value (or nothing), never text typed mid-flight.
    const storedCheckin = readBenchCheckin();
    const checkinHint = storedCheckin?.hint || "";
    if (storedCheckin) {
      setBenchCheckin(storedCheckin);
      onSelectedProjectChange(storedCheckin.projectId);
      if (storedCheckin.sessionId) {
        setSessionId(storedCheckin.sessionId);
      }
      if (storedCheckin.hint) {
        setHint(storedCheckin.hint);
      }
    }
    if (!params.tagSlug && !hasPrefillParams) {
      // No URL context to apply: the capture context is settled immediately.
      setCaptureContextReady(true);
      return cancelOnCleanup;
    }
    (async () => {
      try {
        let binding = null;
        let tagUnbound = false;
        if (params.tagSlug) {
          try {
            binding = await apiRequest(`/tags/${encodeURIComponent(params.tagSlug)}`, { token });
          } catch (err) {
            if (err?.status === 404) {
              tagUnbound = true;
              setFlash("", UNBOUND_TAG_MESSAGE);
            }
            // Any other failure (network blip, auth refresh) keeps the URL
            // slug as best-effort witness context below; the tag is never a
            // gate.
          }
          if (urlParamsCanceledRef.current) {
            return;
          }
        }
        setUrlCaptureContext({
          entry: params.tagSlug ? "tag_tap" : "url_params",
          tagLabel: binding?.label || "",
          tagSlug: tagUnbound ? "" : binding?.slug || params.tagSlug || "",
        });
        const mergedProjectId = params.projectId || binding?.project_id || "";
        let appliedProjectId = mergedProjectId || storedCheckin?.projectId || selectedProjectId;
        if (mergedProjectId) {
          onSelectedProjectChange(mergedProjectId);
        }
        let resolvedSessionId = params.sessionLink ? "" : binding?.session_id || "";
        if (params.sessionLink) {
          let linkedSession = null;
          try {
            linkedSession = await apiRequest(
              `/sessions/by-link/${encodeURIComponent(params.sessionLink)}`,
              { token }
            );
          } catch {
            // Unresolvable link codes leave capture without a session link.
          }
          if (urlParamsCanceledRef.current) {
            return;
          }
          if (linkedSession?.session_id) {
            resolvedSessionId = linkedSession.session_id;
            if (linkedSession.project_id && linkedSession.project_id !== appliedProjectId) {
              // The session's own project wins over any earlier selection.
              appliedProjectId = linkedSession.project_id;
              onSelectedProjectChange(linkedSession.project_id);
            }
          }
        }
        if (resolvedSessionId) {
          setSessionId(resolvedSessionId);
        }
        const mergedHint = params.hint || binding?.hint || "";
        if (mergedHint) {
          // Functional update: text typed while the tag/session fetches were
          // in flight wins; the check-in hint applied at mount still yields
          // to the merged param/binding hint.
          setHint((prev) => (prev && prev !== checkinHint ? prev : mergedHint));
        }
        if (params.mode === "voice" || binding?.mode === "voice") {
          setVoiceFirst(true);
          setCaptureMode("voice");
        }
        if (params.checkin && appliedProjectId) {
          const entry = {
            expiresAt: Date.now() + clampCheckinTtlHours(params.ttlHours) * HOUR_MS,
            hint: mergedHint,
            label: binding?.label || "",
            projectId: appliedProjectId,
            sessionId: resolvedSessionId,
          };
          writeBenchCheckin(entry);
          setBenchCheckin(entry);
        }
        // No slug means there is no physical tag to witness, so no
        // breadcrumb — a plain ?log=1 URL must not fabricate a tag_tap note.
        if ((params.log || binding?.log_breadcrumb) && params.tagSlug) {
          setPendingBreadcrumb({
            label: binding?.label || params.tagSlug,
            projectId: appliedProjectId,
            sessionId: resolvedSessionId,
            slug: params.tagSlug,
          });
        }
      } finally {
        setCaptureContextReady(true);
      }
    })();
    return cancelOnCleanup;
  }, [onSelectedProjectChange, selectedProjectId, setFlash, token]);

  useEffect(() => {
    // Best-effort tag-tap breadcrumb: waits until token + write access are
    // available, fires at most once per mount, and dedupes repeat taps
    // across mounts via sessionStorage. The target project is the one the
    // mount-time merge resolved — never a project the user picks later — so
    // a breadcrumb can only land where the tag actually pointed. Failures
    // stay silent.
    if (!pendingBreadcrumb || breadcrumbFiredRef.current) {
      return;
    }
    const projectId = pendingBreadcrumb.projectId;
    if (!token || !canWrite || !projectId) {
      return;
    }
    breadcrumbFiredRef.current = true;
    if (!shouldFireTagBreadcrumb(pendingBreadcrumb.slug)) {
      return;
    }
    const metadata = {
      capture_kind: "tag_breadcrumb",
      capture_review_status: "pending_review",
      capture_source: "tag_tap",
    };
    if (pendingBreadcrumb.slug) {
      metadata.tag_slug = pendingBreadcrumb.slug;
    }
    apiRequest("/notes", {
      body: {
        metadata,
        project_id: projectId,
        raw_content: pendingBreadcrumb.label
          ? `Tag tap: ${pendingBreadcrumb.label}`
          : "Tag tap",
        targets: pendingBreadcrumb.sessionId
          ? [{ entity_id: pendingBreadcrumb.sessionId, entity_type: "session" }]
          : [],
      },
      method: "POST",
      token,
    }).catch(() => {
      // Breadcrumbs are best-effort; a failed POST never disturbs capture.
    });
  }, [canWrite, pendingBreadcrumb, token]);

  useEffect(() => {
    // Voice-first auto-save: tap, talk, pocket. Deliberately no dependency
    // list — the ref guard below makes every run a no-op except the single
    // commit where a voice-first recording reaches state, and the decision
    // must see that commit's fresh readyToUpload()/uploadCapture closures.
    // If the capture is not ready at that moment (e.g. no project), the
    // untouched manual flow takes over.
    if (!audioFile || autoUploadFileRef.current !== audioFile) {
      return;
    }
    autoUploadFileRef.current = null;
    if (!canWrite || !readyToUpload()) {
      return;
    }
    uploadCapture();
  });

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

  function applyComposerText(value) {
    if (photoFile || audioFile) {
      setHint(value);
      return;
    }
    if (captureMode !== "text") {
      setCaptureMode("text");
    }
    setTextNote(value);
  }

  function handleComposerTextChange(event) {
    clearUploadProgress();
    applyComposerText(event.target.value);
  }

  function handlePhotoFileChange(event) {
    const file = event.target.files?.[0] || null;
    // Reset the input so re-selecting the same file fires another change.
    event.target.value = "";
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

  // autoSave is only ever true for the dedicated voice-first record input:
  // the shared mic / file-picker inputs stage the recording for a manual
  // send even while the voice-first layout is active.
  function handleAudioFileChange(event, { autoSave = false } = {}) {
    const file = event.target.files?.[0] || null;
    // Reset the input so re-selecting the same file fires another change.
    event.target.value = "";
    clearUploadProgress();
    setAudioFile(file);
    if (file) {
      if (textNote.trim() && !hint.trim()) {
        setHint(textNote.trim());
        setTextNote("");
      }
      setCaptureMode(photoFile ? "bundle" : "voice");
      setAttachmentMenuOpen(false);
      if (autoSave) {
        autoUploadFileRef.current = file;
      }
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

  function baseMetadata({ kind, bundleId = "", file = null }) {
    const metadata = {
      capture_source: "mobile_capture",
      capture_mode: captureMode,
      capture_kind: kind,
      capture_review_status: "pending_review",
      ...sourceFileMetadata(file),
    };
    if (bundleId) {
      metadata.capture_bundle_id = bundleId;
    }
    if (hint.trim()) {
      metadata.capture_hint = hint.trim();
    }
    if (urlCaptureContext?.entry) {
      metadata.capture_entry = urlCaptureContext.entry;
    }
    if (urlCaptureContext?.tagSlug) {
      metadata.tag_slug = urlCaptureContext.tagSlug;
      if (urlCaptureContext.tagLabel) {
        metadata.tag_label = urlCaptureContext.tagLabel;
      }
    }
    if (kind === "voice") {
      metadata.voice_note_type = voiceNoteType;
      metadata.transcript_status = "pending";
    }
    return metadata;
  }

  async function uploadRawFileNote({ fileToUpload, metadata, clientCaptureId }) {
    const payload = new FormData();
    payload.append("file", fileToUpload);
    payload.append("project_id", selectedProjectId);
    payload.append("metadata", JSON.stringify(metadata));
    payload.append("client_capture_id", clientCaptureId);
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

  async function queueRawFileNoteOffline({ fileToUpload, metadata, clientCaptureId }) {
    const queue = getUploadQueue();
    if (!queue) {
      return false;
    }
    const fields = {
      project_id: selectedProjectId,
      metadata: JSON.stringify(metadata),
      client_capture_id: clientCaptureId,
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
    const clientCaptureId = newBundleId();
    try {
      return await uploadRawFileNote({ fileToUpload, metadata, clientCaptureId });
    } catch (err) {
      // err.status is set by apiFetch for server-rejected responses; absence
      // means the fetch itself failed (offline, DNS, CORS, etc.). Only queue
      // in that case — real validation/auth errors must surface as before.
      if (err && err.status === undefined) {
        const queued = await queueRawFileNoteOffline({
          fileToUpload,
          metadata,
          clientCaptureId,
        });
        if (queued) {
          return OFFLINE_QUEUED;
        }
      }
      throw err;
    }
  }

  async function createTextCapture() {
    return apiRequest("/notes", {
      body: {
        project_id: selectedProjectId,
        raw_content: textNote.trim(),
        targets: selectedTargets(),
        metadata: baseMetadata({ kind: "text" }),
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

  function benchCheckinDisplayLabel(entry) {
    if (entry.label) {
      return entry.label;
    }
    const match = projects.find((item) => item.project_id === entry.projectId);
    return match?.name || "this project";
  }

  function handleStayCheckedIn() {
    // Manual check-in: persists the CURRENT selection, so it works with
    // zero tags and no URL params.
    if (!selectedProjectId) {
      return;
    }
    const entry = {
      expiresAt: Date.now() + BENCH_CHECKIN_DEFAULT_TTL_HOURS * HOUR_MS,
      hint: hint.trim(),
      label: "",
      projectId: selectedProjectId,
      sessionId,
    };
    writeBenchCheckin(entry);
    setBenchCheckin(entry);
  }

  function handleCheckOut() {
    clearBenchCheckin();
    setBenchCheckin(null);
  }

  async function uploadCapture() {
    // Synchronous re-entry guard: a second tap (or the voice-first auto-fire
    // racing a manual tap) while an upload is in flight must not POST the
    // same capture again under a fresh client_capture_id.
    if (uploadInFlightRef.current) {
      return;
    }
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
    uploadInFlightRef.current = true;
    setUploading(true);
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
          metadata: baseMetadata({ kind: "image", bundleId, file: photoFile }),
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
          metadata: baseMetadata({ kind: "voice", bundleId, file: audioFile }),
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
          metadata: baseMetadata({ kind: "voice", bundleId, file: audioFile }),
        });
      }

      if (needsText() && !noteId && !queuedOffline) {
        const textCapture = await createTextCapture();
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
      setFlash("Capture saved for review.");
      setPhotoFile(null);
      setAudioFile(null);
      setTextNote("");
    } catch (err) {
      setFlash("", err.message || "Capture failed.");
    } finally {
      uploadInFlightRef.current = false;
      setUploading(false);
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
            <div aria-live="polite" className="capture-checkin">
              {benchCheckin ? (
                <>
                  <span className="capture-checkin-status">
                    Checked in: {benchCheckinDisplayLabel(benchCheckin)} until{" "}
                    {checkinTimeLabel(benchCheckin.expiresAt)}
                  </span>
                  <button className="btn-secondary" onClick={handleCheckOut} type="button">
                    Check out
                  </button>
                </>
              ) : (
                <button
                  className="btn-secondary"
                  disabled={!canWrite || !selectedProjectId}
                  onClick={handleStayCheckedIn}
                  type="button"
                >
                  Stay checked in
                </button>
              )}
            </div>
            <input
              accept="image/*"
              aria-label="Photo file"
              className="sr-only"
              disabled={!canWrite}
              id="capture-photo-input"
              onChange={handlePhotoFileChange}
              type="file"
            />
            <input
              accept="audio/*"
              aria-label="Voice recording"
              className="sr-only"
              disabled={!canWrite}
              id="capture-audio-input"
              onChange={handleAudioFileChange}
              type="file"
            />
            <input
              accept="audio/*"
              aria-label="Record voice note"
              capture
              className="sr-only"
              disabled={!canWrite}
              id="capture-audio-record-input"
              onChange={handleAudioFileChange}
              type="file"
            />
            {voiceFirst ? (
              <div className="capture-voice-first">
                {/* Dedicated input so ONLY the big record control arms the
                    auto-upload; the shared mic and file-picker inputs above
                    always stage for a manual send. */}
                <input
                  accept="audio/*"
                  aria-label="Record voice note (voice-first)"
                  capture
                  className="sr-only"
                  disabled={!canWrite}
                  id="capture-voice-first-record-input"
                  onChange={(event) => handleAudioFileChange(event, { autoSave: true })}
                  type="file"
                />
                <label
                  aria-disabled={!canWrite}
                  className={`capture-voice-first-record${canWrite ? "" : " disabled"}`}
                  htmlFor="capture-voice-first-record-input"
                >
                  <CaptureIcon kind="voice" />
                  <span>Record voice note</span>
                </label>
                <p className="capture-voice-first-status subtle" role="status">
                  {selectedProjectId
                    ? "Tap to record — capture saves automatically."
                    : "Choose a project below, then record to save."}
                </p>
              </div>
            ) : null}
            {/* Only the typed text is recoverable; an attached photo or
                recording cannot be held in local storage. */}
            <DraftRecoveryNotice
              label="an unsent capture"
              savedAt={captureDraft.recoveredAt}
              onRestore={() => {
                const restored = captureDraft.restore();
                if (restored === null) {
                  return;
                }
                let parsed = null;
                try {
                  parsed = JSON.parse(restored);
                } catch {
                  parsed = null;
                }
                if (!Array.isArray(parsed)) {
                  return;
                }
                const [restoredText, restoredHint] = parsed;
                if (typeof restoredText === "string") {
                  setTextNote(restoredText);
                }
                if (typeof restoredHint === "string") {
                  setHint(restoredHint);
                }
                if (restoredText && !photoFile && !audioFile) {
                  setCaptureMode("text");
                }
              }}
              onDiscard={captureDraft.discard}
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
                htmlFor="capture-audio-record-input"
              >
                <CaptureIcon kind="voice" />
              </label>
              <button
                aria-label="Save capture"
                className="capture-composer-send"
                disabled={!canWrite || uploading || !readyToCapture()}
                onClick={() => uploadCapture()}
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
                disabled={!canWrite || uploading || !readyToCapture()}
                onClick={() => uploadCapture()}
                type="button"
              >
                Save for later
              </button>
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
                onChange={(event) => {
                  onSelectedProjectChange(event.target.value);
                  // A manual re-aim invalidates the physical-anchor claim:
                  // later captures keep the entry channel (capture_entry)
                  // but must not carry the tag witness into a project the
                  // tag never pointed at.
                  setUrlCaptureContext((current) =>
                    current?.tagSlug ? { ...current, tagLabel: "", tagSlug: "" } : current
                  );
                }}
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
                {/* A prefilled session (tag binding, session link, or
                    check-in) may no longer be in the active list; render it
                    so the link is visible and clearable instead of silently
                    attaching to captures. */}
                {sessionId && !sessions.some((item) => item.session_id === sessionId) ? (
                  <option value={sessionId}>Linked session (no longer active)</option>
                ) : null}
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
