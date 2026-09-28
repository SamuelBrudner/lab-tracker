import * as React from "react";

import {
  OFFLINE_QUEUED,
  buildCaptureMetadata,
  createOrQueueTextCapture,
  newCaptureId,
  uploadOrQueueRawFile,
} from "../../shared/capture-upload.js";
import { getUploadQueue } from "../../shared/register-sw.js";
import { CAPTURE_CHANNEL, errorMessage, sessionLabel, sessionTargets } from "./bench-helpers.js";

const { useEffect, useMemo, useRef, useState } = React;

// The summary note names at most this many files, then counts the rest.
const SUMMARY_LISTED_FILES = 20;
const ITEM_STATE_LABELS = {
  failed: "Failed",
  pending: "Waiting",
  queued: "Queued offline",
  saved: "Uploaded",
  uploading: "Uploading…",
};
const FINISHED_STATES = new Set(["failed", "queued", "saved"]);

// A photo's own clock (when the camera saved it) is when it was captured;
// the import time stands in when the browser does not report one.
function photoCapturedAt(file, fallbackMs) {
  const lastModified = Number(file?.lastModified);
  return Number.isFinite(lastModified) && lastModified > 0 ? lastModified : fallbackMs;
}

function summaryText(batch) {
  const names = batch.items.map((item) => item.name);
  const listed = names.slice(0, SUMMARY_LISTED_FILES).join(", ");
  const more =
    names.length > SUMMARY_LISTED_FILES ? `, and ${names.length - SUMMARY_LISTED_FILES} more` : "";
  const noun = batch.total === 1 ? "photo" : "photos";
  const where = batch.sessionLabel ? ` from ${batch.sessionLabel}` : "";
  return `Imported ${batch.total} ${noun}${where}: ${listed}${more}.`;
}

/**
 * End-of-session photo import: pick many photos at once and upload them as one
 * grouped capture. The server takes one file per upload, so each photo becomes
 * its own staged note sharing a capture_bundle_id (capture_channel=import),
 * plus one short summary note, all targeting the session and all through the
 * offline-aware queue. Each file shows its own state and can be retried under
 * the same client capture id, so a retry never duplicates a photo.
 */
function PhotoImportPanel({
  token,
  ownerId = "",
  projectId,
  session,
  canWrite,
  queue: queueOverride = undefined,
  now = Date.now,
}) {
  const queue = useMemo(
    () => (queueOverride === undefined ? getUploadQueue() : queueOverride),
    [queueOverride]
  );
  const inputRef = useRef(null);
  const mountedRef = useRef(false);
  const runningRef = useRef(false);
  const [batch, setBatch] = useState(null);
  const [running, setRunning] = useState(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  function updateBatch(update) {
    if (mountedRef.current) {
      setBatch((current) => (current ? update(current) : current));
    }
  }

  function updateItem(itemId, patch) {
    updateBatch((current) => ({
      ...current,
      items: current.items.map((item) => (item.id === itemId ? { ...item, ...patch } : item)),
    }));
  }

  function updateSummary(patch) {
    updateBatch((current) => ({ ...current, summary: { ...current.summary, ...patch } }));
  }

  async function uploadItem(context, item) {
    updateItem(item.id, { error: "", state: "uploading" });
    try {
      const metadata = {
        ...buildCaptureMetadata({
          captureMode: "photo",
          kind: "image",
          bundleId: context.bundleId,
          file: item.file,
          captureChannel: CAPTURE_CHANNEL.IMPORT,
          now: () => photoCapturedAt(item.file, context.startedAt),
        }),
        capture_import_index: item.index,
        capture_import_total: context.total,
      };
      const result = await uploadOrQueueRawFile({
        token,
        projectId: context.projectId,
        ownerId,
        fileToUpload: item.file,
        metadata,
        targets: sessionTargets(context.sessionId),
        queue,
        clientCaptureId: item.clientCaptureId,
      });
      updateItem(item.id, { state: result === OFFLINE_QUEUED ? "queued" : "saved" });
    } catch (error) {
      updateItem(item.id, { error: errorMessage(error, "Upload failed."), state: "failed" });
    }
  }

  async function sendSummary(context) {
    updateSummary({ error: "", state: "uploading" });
    try {
      const result = await createOrQueueTextCapture({
        token,
        projectId: context.projectId,
        ownerId,
        rawContent: summaryText(context),
        targets: sessionTargets(context.sessionId),
        metadata: {
          ...buildCaptureMetadata({
            captureMode: "text",
            kind: "text",
            bundleId: context.bundleId,
            captureChannel: CAPTURE_CHANNEL.IMPORT,
            now: () => context.startedAt,
          }),
          capture_import_summary: true,
          capture_import_total: context.total,
        },
        queue,
        clientCaptureId: context.summary.clientCaptureId,
      });
      updateSummary({ state: result === OFFLINE_QUEUED ? "queued" : "saved" });
    } catch (error) {
      updateSummary({ error: errorMessage(error, "Summary note failed."), state: "failed" });
    }
  }

  async function runExclusive(work) {
    if (runningRef.current) {
      return;
    }
    runningRef.current = true;
    setRunning(true);
    try {
      await work();
    } finally {
      runningRef.current = false;
      if (mountedRef.current) {
        setRunning(false);
      }
    }
  }

  async function handleFiles(event) {
    const files = Array.from(event.target.files || []).filter(
      (file) => !file.type || file.type.startsWith("image/")
    );
    // Let the same photos be picked again later.
    event.target.value = "";
    if (files.length === 0 || !canWrite || !projectId || !session || runningRef.current) {
      return;
    }
    const bundleId = newCaptureId();
    const context = {
      bundleId,
      items: files.map((file, index) => ({
        clientCaptureId: newCaptureId(),
        error: "",
        file,
        id: `${bundleId}-${index}`,
        index: index + 1,
        name: file.name || `photo-${index + 1}`,
        state: "pending",
      })),
      projectId,
      sessionId: session.session_id,
      sessionLabel: sessionLabel(session),
      startedAt: now(),
      summary: { clientCaptureId: newCaptureId(), error: "", state: "pending" },
      total: files.length,
    };
    setBatch(context);
    await runExclusive(async () => {
      for (const item of context.items) {
        await uploadItem(context, item);
      }
      await sendSummary(context);
    });
  }

  async function retryItem(item) {
    if (batch) {
      const context = batch;
      await runExclusive(() => uploadItem(context, item));
    }
  }

  async function retryFailed() {
    if (!batch) {
      return;
    }
    const context = batch;
    await runExclusive(async () => {
      for (const item of context.items.filter((candidate) => candidate.state === "failed")) {
        await uploadItem(context, item);
      }
      if (context.summary.state === "failed") {
        await sendSummary(context);
      }
    });
  }

  if (!session) {
    return null;
  }

  const finished = batch
    ? batch.items.filter((item) => FINISHED_STATES.has(item.state)).length
    : 0;
  const counts = batch
    ? batch.items.reduce((totals, item) => {
        totals[item.state] = (totals[item.state] || 0) + 1;
        return totals;
      }, {})
    : {};
  const anyFailed = Boolean(
    batch && (counts.failed > 0 || batch.summary.state === "failed")
  );

  return (
    <section className="stack photo-import" aria-labelledby="photo-import-title">
      <div className="item-head">
        <h3 id="photo-import-title">Import photos</h3>
      </div>
      <p className="subtle">
        Pick the session&apos;s photos at once; they upload as one group linked to this session,
        and queue on this device if the network drops.
      </p>
      <input
        ref={inputRef}
        accept="image/*"
        aria-label="Photos to import"
        className="sr-only"
        disabled={!canWrite || running}
        multiple
        onChange={handleFiles}
        type="file"
      />
      <div className="inline">
        <button
          type="button"
          className="btn-secondary"
          disabled={!canWrite || running}
          onClick={() => inputRef.current?.click()}
        >
          {running ? "Importing…" : "Import photos"}
        </button>
        {anyFailed && !running ? (
          <button type="button" className="btn-secondary" onClick={retryFailed}>
            Retry failed
          </button>
        ) : null}
      </div>
      {batch ? (
        <div className="stack">
          <progress
            aria-label="Photo import progress"
            max={batch.total}
            value={finished}
          />
          <p className="subtle" role="status">
            {`${finished} of ${batch.total} done`}
            {counts.saved ? ` · ${counts.saved} uploaded` : ""}
            {counts.queued ? ` · ${counts.queued} queued offline` : ""}
            {counts.failed ? ` · ${counts.failed} failed` : ""}
            {batch.summary.state === "saved" || batch.summary.state === "queued"
              ? " · summary note saved"
              : ""}
            {batch.summary.state === "failed" ? ` · summary note failed` : ""}
          </p>
          <ul className="list-clean" aria-label="Imported photos">
            {batch.items.map((item) => (
              <li key={item.id} className="row-between">
                <span>
                  {item.name}
                  {item.error ? <span className="subtle"> · {item.error}</span> : null}
                </span>
                <span className="inline">
                  <span className="pill">{ITEM_STATE_LABELS[item.state] || item.state}</span>
                  {item.state === "failed" && !running ? (
                    <button
                      type="button"
                      className="btn-secondary"
                      aria-label={`Retry ${item.name}`}
                      onClick={() => retryItem(item)}
                    >
                      Retry
                    </button>
                  ) : null}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}

export { PhotoImportPanel, summaryText };
