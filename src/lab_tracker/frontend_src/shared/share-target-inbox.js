/* Page-side reader for the OS share-sheet inbox.
 *
 * The service worker (sw.js) parks shared files in a separate IndexedDB
 * database (lab-tracker-share-inbox) because the OS-initiated POST has no
 * auth context. Any web page can also POST to the share target, so parked
 * shares are untrusted until the user reviews them: the capture page lists
 * them, and only when the user confirms does migrateIncomingShares attach the
 * active project + bearer token to exactly the reviewed shares and hand them
 * to the main upload queue, which is responsible for the actual POST + retry.
 * Storage is split from logic so callers can inject an in-memory adapter in
 * tests.
 */

import { UPLOAD_FILE_PATH } from "./upload-queue.js";

const DB_NAME = "lab-tracker-share-inbox";
const DB_VERSION = 1;
const STORE = "pending";

function openShareInbox() {
  return new Promise((resolve, reject) => {
    const request = globalThis.indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE)) {
        db.createObjectStore(STORE, { keyPath: "id", autoIncrement: true });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function runRequest(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function txDone(tx) {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error || new Error("IndexedDB transaction aborted."));
  });
}

function createIndexedDbShareStorage() {
  return {
    async list() {
      const db = await openShareInbox();
      try {
        const tx = db.transaction(STORE, "readonly");
        return await runRequest(tx.objectStore(STORE).getAll());
      } finally {
        db.close();
      }
    },
    async remove(id) {
      const db = await openShareInbox();
      try {
        const tx = db.transaction(STORE, "readwrite");
        await runRequest(tx.objectStore(STORE).delete(id));
        await txDone(tx);
      } finally {
        db.close();
      }
    },
  };
}

function createMemoryShareStorage(initial = []) {
  const items = new Map();
  let nextId = 1;
  for (const entry of initial) {
    const id = nextId++;
    items.set(id, { ...entry, id });
  }
  return {
    async list() {
      return Array.from(items.values()).sort((a, b) => a.id - b.id);
    },
    async remove(id) {
      items.delete(id);
    },
  };
}

function buildShareMetadata(share) {
  const metadata = {
    capture_source: "share_target",
    shared_at: new Date(share.receivedAt || Date.now()).toISOString(),
  };
  if (share.title) {
    metadata.share_title = share.title;
  }
  if (share.text) {
    metadata.share_text = share.text;
  }
  if (share.url) {
    metadata.share_url = share.url;
  }
  return metadata;
}

function shareTextContent(share) {
  const parts = [share.title, share.text, share.url]
    .map((value) => String(value || "").trim())
    .filter(Boolean);
  return Array.from(new Set(parts)).join("\n\n");
}

function shareInboxAvailable() {
  return typeof globalThis.indexedDB !== "undefined";
}

function requireReviewedShareIds(shareIds) {
  if (!Array.isArray(shareIds)) {
    throw new TypeError(
      "shareIds must list the shares the user reviewed; parked shares are never imported implicitly."
    );
  }
  return new Set(shareIds);
}

async function listReviewedShares(storage, shareIds) {
  const reviewed = requireReviewedShareIds(shareIds);
  if (reviewed.size === 0) {
    return [];
  }
  return (await storage.list()).filter((share) => reviewed.has(share.id));
}

// Imports exactly the shares the user reviewed (`shareIds`), so a share that
// lands in the inbox after the review was shown is never imported unseen.
async function migrateIncomingShares({
  createTextNote = null,
  projectId,
  ownerId = "",
  uploadQueue,
  shareIds,
  storage = createIndexedDbShareStorage(),
}) {
  requireReviewedShareIds(shareIds);
  if (!projectId || !uploadQueue) {
    return { migrated: 0, skipped: 0 };
  }
  const shares = await listReviewedShares(storage, shareIds);
  if (shares.length === 0) {
    return { migrated: 0, skipped: 0 };
  }
  let migrated = 0;
  let skipped = 0;
  for (const share of shares) {
    if (!share.file) {
      const rawContent = shareTextContent(share);
      if (!rawContent) {
        await storage.remove(share.id);
        skipped += 1;
        continue;
      }
      if (typeof createTextNote !== "function") {
        skipped += 1;
        continue;
      }
      await createTextNote({
        metadata: buildShareMetadata(share),
        projectId,
        rawContent,
        share,
      });
      await storage.remove(share.id);
      migrated += 1;
      continue;
    }
    const fields = {
      project_id: projectId,
      metadata: JSON.stringify(buildShareMetadata(share)),
    };
    await uploadQueue.enqueue({
      endpoint: UPLOAD_FILE_PATH,
      file: share.file,
      fields,
      filename: share.filename,
      contentType: share.contentType,
      ownerId,
    });
    await storage.remove(share.id);
    migrated += 1;
  }
  return { migrated, skipped };
}

async function discardIncomingShares({ shareIds, storage = createIndexedDbShareStorage() }) {
  const shares = await listReviewedShares(storage, shareIds);
  for (const share of shares) {
    await storage.remove(share.id);
  }
  return { discarded: shares.length };
}

export {
  DB_NAME,
  STORE,
  createIndexedDbShareStorage,
  createMemoryShareStorage,
  discardIncomingShares,
  migrateIncomingShares,
  shareInboxAvailable,
};
