import * as React from "react";

import {
  OFFLINE_QUEUED,
  buildCaptureMetadata,
  createOrQueueTextCapture,
  newCaptureId,
} from "../../shared/capture-upload.js";
import { getUploadQueue } from "../../shared/register-sw.js";
import {
  readCaptureLaunchContext,
  readRememberedCaptureContext,
  writeRememberedCaptureContext,
} from "../mobile-capture/capture-helpers.js";
import {
  BENCH_SCAN_MAX_CHARS,
  CAPTURE_CHANNEL,
  errorMessage,
  sessionLabel,
  sessionTargets,
} from "./bench-helpers.js";

const { useCallback, useEffect, useMemo, useRef, useState } = React;

// The running list shows this many scans; older ones are already saved or
// queued and simply scroll off.
const KIOSK_SCAN_HISTORY = 20;
const SCAN_STATE_LABELS = {
  failed: "Failed",
  queued: "Queued offline",
  saved: "Saved",
  sending: "Sending…",
  synced: "Synced",
};
const NO_SESSIONS = Object.freeze([]);

// Kiosk type is sized for a bench PC read at arm's length.
const KIOSK_INPUT_STYLE = {
  fontSize: "2.25rem",
  letterSpacing: "0.04em",
  padding: "0.55em 0.7em",
  width: "100%",
};
const KIOSK_EXIT_STYLE = { fontSize: "1.15rem", padding: "0.7em 1.2em" };
const KIOSK_SCAN_STYLE = { fontSize: "1.35rem" };

function isKioskSearch(search = window.location.search) {
  try {
    return new URLSearchParams(search || "").get("kiosk") === "1";
  } catch {
    return false;
  }
}

function scanTime(at) {
  try {
    return new Date(at).toLocaleTimeString();
  } catch {
    return "";
  }
}

function onlineNow() {
  return typeof navigator === "undefined" || navigator.onLine !== false;
}

/**
 * Bench scan station for a shared PC with a USB barcode scanner (the scanner
 * types the code and Enter). Each Enter stages one text note through the
 * offline-aware capture queue, recording who scanned (the signed-in person or
 * paired device), when (the scan clock), and where (the chosen session). It is
 * a log of what was scanned, not an inventory: nothing is looked up or linked
 * beyond the session the person picked.
 */
function KioskCaptureCard({
  token,
  ownerId = "",
  authEnabled = true,
  canWrite,
  projects,
  selectedProjectId,
  onSelectedProjectChange,
  sessions = NO_SESSIONS,
  navigate,
  queue: queueOverride = undefined,
  now = Date.now,
}) {
  const queue = useMemo(
    () => (queueOverride === undefined ? getUploadQueue() : queueOverride),
    [queueOverride]
  );
  const launchSessionId = useMemo(() => readCaptureLaunchContext().sessionId, []);
  const [sessionId, setSessionId] = useState("");
  const [scanValue, setScanValue] = useState("");
  const [scans, setScans] = useState([]);
  const [online, setOnline] = useState(onlineNow);
  const inputRef = useRef(null);
  const mountedRef = useRef(false);
  const sessionChoiceRef = useRef({ projectId: null, candidate: "", applied: true });
  const project = projects.find((item) => item.project_id === selectedProjectId) || null;
  const session = sessions.find((item) => item.session_id === sessionId) || null;

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // Preselect the session a capture link named, else the one this device last
  // captured into for the project, once the active-session list confirms it.
  useEffect(() => {
    if (sessionChoiceRef.current.projectId !== selectedProjectId) {
      const candidate =
        launchSessionId || readRememberedCaptureContext(selectedProjectId).sessionId;
      sessionChoiceRef.current = {
        projectId: selectedProjectId,
        candidate,
        applied: !candidate,
      };
      setSessionId("");
    }
    const choice = sessionChoiceRef.current;
    if (!choice.applied && sessions.some((item) => item.session_id === choice.candidate)) {
      choice.applied = true;
      setSessionId(choice.candidate);
    }
  }, [launchSessionId, selectedProjectId, sessions]);

  const focusInput = useCallback(() => {
    inputRef.current?.focus();
  }, []);

  useEffect(() => {
    // A scanner types into whatever has focus: take it back whenever the
    // kiosk window regains focus.
    window.addEventListener("focus", focusInput);
    return () => window.removeEventListener("focus", focusInput);
  }, [focusInput]);

  const updateScan = useCallback((id, patch) => {
    if (!mountedRef.current) {
      return;
    }
    setScans((current) => current.map((scan) => (scan.id === id ? { ...scan, ...patch } : scan)));
  }, []);

  const markQueuedScansFrom = useCallback((pendingItems) => {
    if (!mountedRef.current) {
      return;
    }
    const pending = new Set(pendingItems.map((item) => item.clientCaptureId));
    setScans((current) =>
      current.map((scan) =>
        scan.state === "queued" && !pending.has(scan.clientCaptureId)
          ? { ...scan, state: "synced" }
          : scan
      )
    );
  }, []);

  useEffect(() => {
    // Queued scans leave the queue when a drain (boot, back online) sends
    // them; reflect that in the list without polling.
    if (!queue || typeof queue.subscribe !== "function") {
      return undefined;
    }
    return queue.subscribe(() => {
      Promise.resolve(queue.listPending())
        .then((items) => markQueuedScansFrom(items || []))
        .catch(() => {
          // The list only mirrors sync state; the queue keeps the scans.
        });
    });
  }, [markQueuedScansFrom, queue]);

  useEffect(() => {
    const handleOnline = () => {
      setOnline(true);
      if (!queue) {
        return;
      }
      Promise.resolve(queue.drain({ token, ownerId, authEnabled }))
        .then((result) => {
          const dropped = new Set((result?.dropped || []).map((item) => item.clientCaptureId));
          if (dropped.size === 0 || !mountedRef.current) {
            return;
          }
          setScans((current) =>
            current.map((scan) =>
              dropped.has(scan.clientCaptureId)
                ? { ...scan, state: "failed", error: "The server refused this scan." }
                : scan
            )
          );
        })
        .catch(() => {
          // Still queued; the next online event or boot retries.
        });
    };
    const handleOffline = () => setOnline(false);
    window.addEventListener("online", handleOnline);
    window.addEventListener("offline", handleOffline);
    return () => {
      window.removeEventListener("online", handleOnline);
      window.removeEventListener("offline", handleOffline);
    };
  }, [authEnabled, ownerId, queue, token]);

  async function sendScan(scan) {
    updateScan(scan.id, { error: "", state: "sending" });
    try {
      const result = await createOrQueueTextCapture({
        token,
        projectId: scan.projectId,
        ownerId,
        rawContent: `Bench scan: ${scan.value}`,
        targets: sessionTargets(scan.sessionId),
        metadata: scan.metadata,
        queue,
        clientCaptureId: scan.clientCaptureId,
      });
      if (result === OFFLINE_QUEUED) {
        updateScan(scan.id, { state: "queued" });
      } else {
        updateScan(scan.id, { noteId: result?.note_id || "", state: "saved" });
      }
    } catch (error) {
      updateScan(scan.id, {
        error: errorMessage(error, "The scan could not be saved."),
        state: "failed",
      });
    }
  }

  function handleSubmit(event) {
    event.preventDefault();
    const value = scanValue.trim();
    setScanValue("");
    focusInput();
    if (!value || !canWrite || !selectedProjectId) {
      return;
    }
    const at = now();
    const scanned = value.slice(0, BENCH_SCAN_MAX_CHARS);
    const metadata = {
      ...buildCaptureMetadata({
        captureMode: "text",
        kind: "text",
        captureChannel: CAPTURE_CHANNEL.KIOSK,
        now: () => at,
      }),
      bench_scan_value: scanned,
    };
    if (scanned.length < value.length) {
      metadata.bench_scan_truncated = true;
    }
    const scan = {
      at,
      clientCaptureId: newCaptureId(),
      error: "",
      id: `${at}-${Math.random().toString(16).slice(2)}`,
      metadata,
      noteId: "",
      projectId: selectedProjectId,
      sessionId,
      state: "sending",
      value: scanned,
    };
    setScans((current) => [scan, ...current].slice(0, KIOSK_SCAN_HISTORY));
    sendScan(scan);
  }

  function handleSessionChange(event) {
    const nextSessionId = event.target.value;
    setSessionId(nextSessionId);
    if (selectedProjectId) {
      const remembered = readRememberedCaptureContext(selectedProjectId);
      writeRememberedCaptureContext(selectedProjectId, {
        questionId: remembered.questionId,
        sessionId: nextSessionId,
      });
    }
    focusInput();
  }

  function exitKiosk() {
    navigate(
      selectedProjectId
        ? `/app/capture?project_id=${encodeURIComponent(selectedProjectId)}`
        : "/app"
    );
  }

  const latest = scans[0] || null;
  const ready = Boolean(canWrite && selectedProjectId);

  return (
    <article className="card span-12 kiosk-capture" aria-labelledby="kiosk-title">
      <div className="item-head">
        <div>
          <h2 id="kiosk-title">Bench scan station</h2>
          <p className="subtle">
            {project ? project.name : "Choose a project"}
            {" · "}
            {session ? sessionLabel(session) : "No session"}
          </p>
        </div>
        <button type="button" className="btn-secondary" style={KIOSK_EXIT_STYLE} onClick={exitKiosk}>
          Exit kiosk
        </button>
      </div>

      {!online ? (
        <p className="flash warning" role="status">
          Offline: scans are kept on this computer and upload when the network is back.
        </p>
      ) : null}
      {!canWrite && selectedProjectId ? (
        <p className="warn">You need write access to this project to record scans.</p>
      ) : null}

      <div className="inline">
        {!project ? (
          <label>
            Project
            <select
              value={selectedProjectId}
              onChange={(event) => onSelectedProjectChange(event.target.value)}
            >
              <option value="">Choose project</option>
              {projects.map((item) => (
                <option key={item.project_id} value={item.project_id}>
                  {item.name}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <label>
          Session
          <select value={sessionId} onChange={handleSessionChange} disabled={!selectedProjectId}>
            <option value="">No session link</option>
            {sessions.map((item) => (
              <option key={item.session_id} value={item.session_id}>
                {sessionLabel(item)}
              </option>
            ))}
          </select>
        </label>
      </div>

      <form className="form" onSubmit={handleSubmit}>
        <label htmlFor="kiosk-scan-input">Scan a barcode, or type a code and press Enter</label>
        <input
          ref={inputRef}
          id="kiosk-scan-input"
          autoFocus
          autoComplete="off"
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
          enterKeyHint="send"
          disabled={!ready}
          maxLength={BENCH_SCAN_MAX_CHARS * 4}
          style={KIOSK_INPUT_STYLE}
          value={scanValue}
          onChange={(event) => setScanValue(event.target.value)}
        />
      </form>

      <p className="subtle" role="status" aria-live="polite">
        {latest
          ? `${latest.value} · ${SCAN_STATE_LABELS[latest.state] || latest.state}`
          : "Each scan is saved for review with the time and session."}
      </p>

      <h3>Recent scans</h3>
      {scans.length === 0 ? (
        <p className="subtle">No scans yet.</p>
      ) : (
        <ol className="list-clean" aria-label="Recent scans">
          {scans.map((scan) => (
            <li key={scan.id} className="row-between">
              <div>
                <strong className="mono" style={KIOSK_SCAN_STYLE}>
                  {scan.value}
                </strong>
                <div className="subtle">
                  {scanTime(scan.at)}
                  {scan.error ? ` · ${scan.error}` : ""}
                </div>
              </div>
              <div className="inline">
                <span className="pill">{SCAN_STATE_LABELS[scan.state] || scan.state}</span>
                {scan.state === "failed" ? (
                  <button
                    type="button"
                    className="btn-secondary"
                    aria-label={`Retry scan ${scan.value}`}
                    onClick={() => {
                      sendScan(scan);
                      focusInput();
                    }}
                  >
                    Retry
                  </button>
                ) : null}
              </div>
            </li>
          ))}
        </ol>
      )}
    </article>
  );
}

export { KIOSK_SCAN_HISTORY, KioskCaptureCard, isKioskSearch };
