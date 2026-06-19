/* Offline-aware upload queue for capture multipart POSTs.
 *
 * The queue stores failed (or offline-initiated) capture payloads in IndexedDB
 * and replays them when the network returns. Logic is split from storage so
 * the queue can be unit-tested against an in-memory adapter without pulling in
 * an IndexedDB shim. The queue is endpoint-agnostic: each item carries its own
 * target path so the same queue serves /notes/upload-file (full-form capture),
 * /notes/quick-capture (share target), and any future capture variants.
 */

const DB_NAME = "lab-tracker-upload-queue";
const DB_VERSION = 1;
const STORE = "pending";

const QUICK_CAPTURE_PATH = "/notes/quick-capture";
const UPLOAD_FILE_PATH = "/notes/upload-file";
const MAX_RETRY_ATTEMPTS = 5;
const AUTH_RETRY_STATUSES = new Set([401, 403]);
const PERMANENT_CLIENT_REJECTION_STATUSES = new Set([400, 404, 409, 410, 413, 415, 422]);

function defaultClientCaptureId() {
  if (globalThis.crypto?.randomUUID) {
    return globalThis.crypto.randomUUID();
  }
  return `capture-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function openIndexedDb() {
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

function createIndexedDbStorage() {
  return {
    async add(record) {
      const db = await openIndexedDb();
      try {
        const tx = db.transaction(STORE, "readwrite");
        const store = tx.objectStore(STORE);
        const id = await runRequest(store.add(record));
        await txDone(tx);
        return id;
      } finally {
        db.close();
      }
    },
    async list() {
      const db = await openIndexedDb();
      try {
        const tx = db.transaction(STORE, "readonly");
        return await runRequest(tx.objectStore(STORE).getAll());
      } finally {
        db.close();
      }
    },
    async remove(id) {
      const db = await openIndexedDb();
      try {
        const tx = db.transaction(STORE, "readwrite");
        await runRequest(tx.objectStore(STORE).delete(id));
        await txDone(tx);
      } finally {
        db.close();
      }
    },
    async update(id, patch) {
      const db = await openIndexedDb();
      try {
        const tx = db.transaction(STORE, "readwrite");
        const store = tx.objectStore(STORE);
        const existing = await runRequest(store.get(id));
        if (!existing) {
          return null;
        }
        const next = { ...existing, ...patch, id };
        await runRequest(store.put(next));
        await txDone(tx);
        return next;
      } finally {
        db.close();
      }
    },
  };
}

function createMemoryStorage() {
  const items = new Map();
  let nextId = 1;
  return {
    async add(record) {
      const id = nextId++;
      items.set(id, { ...record, id });
      return id;
    },
    async list() {
      return Array.from(items.values()).sort((a, b) => a.id - b.id);
    },
    async remove(id) {
      items.delete(id);
    },
    async update(id, patch) {
      const existing = items.get(id);
      if (!existing) {
        return null;
      }
      const next = { ...existing, ...patch, id };
      items.set(id, next);
      return next;
    },
  };
}

function isPermanentClientRejection(status) {
  return PERMANENT_CLIENT_REJECTION_STATUSES.has(status);
}

async function keepQueuedForRetry(adapter, item, { status, now }) {
  const retryCount = Number(item.retryCount || 0) + 1;
  const patch = {
    lastAttemptAt: now(),
    lastStatus: status,
    retryCount,
  };
  if (typeof adapter.update === "function") {
    return (await adapter.update(item.id, patch)) || { ...item, ...patch };
  }
  return { ...item, ...patch };
}

async function keepQueuedForAuth(adapter, item, { status, now }) {
  const patch = {
    lastAttemptAt: now(),
    lastStatus: status,
  };
  if (typeof adapter.update === "function") {
    return (await adapter.update(item.id, patch)) || { ...item, ...patch };
  }
  return { ...item, ...patch };
}

function createUploadQueue({
  storage,
  fetch: fetchImpl = globalThis.fetch,
  now = () => Date.now(),
  createClientCaptureId = defaultClientCaptureId,
} = {}) {
  const adapter = storage || createIndexedDbStorage();
  const listeners = new Set();
  let activeDrain = null;

  function notify() {
    listeners.forEach((listener) => {
      try {
        listener();
      } catch {
        // Listener errors should never break the queue itself.
      }
    });
  }

  async function enqueue({ endpoint, file, fields = {}, token = "", filename = "", contentType = "" }) {
    if (!endpoint) {
      throw new Error("enqueue requires endpoint");
    }
    if (!file) {
      throw new Error("enqueue requires a file");
    }
    if (!fields.project_id) {
      throw new Error("enqueue requires fields.project_id");
    }
    const clientCaptureId = fields.client_capture_id || createClientCaptureId();
    const record = {
      endpoint,
      file,
      fields: { ...fields, client_capture_id: clientCaptureId },
      filename: filename || file.name || "capture",
      contentType: contentType || file.type || "application/octet-stream",
      token,
      clientCaptureId,
      enqueuedAt: now(),
    };
    const id = await adapter.add(record);
    notify();
    return id;
  }

  async function pendingCount() {
    const items = await adapter.list();
    return items.length;
  }

  async function listPending() {
    return adapter.list();
  }

  async function drainOnce({ token = "" } = {}) {
    const items = await adapter.list();
    const results = { dropped: [], uploaded: [], stillQueued: [] };
    for (const item of items) {
      let queuedItem = item;
      const payload = new FormData();
      payload.append("file", queuedItem.file, queuedItem.filename);
      let fields = queuedItem.fields || { project_id: queuedItem.projectId };
      if (!fields.client_capture_id) {
        const clientCaptureId = queuedItem.clientCaptureId || createClientCaptureId();
        fields = { ...fields, client_capture_id: clientCaptureId };
        const patch = { fields, clientCaptureId };
        if (typeof adapter.update === "function") {
          queuedItem = (await adapter.update(queuedItem.id, patch)) || {
            ...queuedItem,
            ...patch,
          };
        } else {
          queuedItem = { ...queuedItem, ...patch };
        }
      }
      for (const [key, value] of Object.entries(fields)) {
        if (value === undefined || value === null) {
          continue;
        }
        payload.append(key, value);
      }
      const bearerToken = token || queuedItem.token || "";
      const headers = bearerToken ? { Authorization: `Bearer ${bearerToken}` } : {};
      const endpoint = queuedItem.endpoint || QUICK_CAPTURE_PATH;
      let response;
      try {
        response = await fetchImpl(endpoint, {
          method: "POST",
          headers,
          body: payload,
        });
      } catch {
        results.stillQueued.push(queuedItem);
        continue;
      }
      if (response && response.ok) {
        await adapter.remove(queuedItem.id);
        results.uploaded.push(queuedItem);
      } else if (response && response.status >= 400 && response.status < 500) {
        if (AUTH_RETRY_STATUSES.has(response.status)) {
          const authQueuedItem = await keepQueuedForAuth(adapter, queuedItem, {
            now,
            status: response.status,
          });
          results.stillQueued.push(authQueuedItem);
          continue;
        }
        // Keep retryable timeout/rate-limit failures queued; drop permanent validation failures.
        if (isPermanentClientRejection(response.status)) {
          await adapter.remove(queuedItem.id);
          results.dropped.push({ ...queuedItem, rejectedStatus: response.status });
          continue;
        }
        const retryQueuedItem = await keepQueuedForRetry(adapter, queuedItem, {
          now,
          status: response.status,
        });
        if (retryQueuedItem.retryCount >= MAX_RETRY_ATTEMPTS) {
          await adapter.remove(queuedItem.id);
          results.dropped.push({
            ...retryQueuedItem,
            rejectedStatus: response.status,
            dropReason: "retry_limit",
          });
        } else {
          results.stillQueued.push(retryQueuedItem);
        }
      } else {
        results.stillQueued.push(queuedItem);
      }
    }
    notify();
    return results;
  }

  async function drain(options = {}) {
    if (activeDrain) {
      return activeDrain;
    }
    activeDrain = drainOnce(options).finally(() => {
      activeDrain = null;
    });
    return activeDrain;
  }

  function subscribe(listener) {
    listeners.add(listener);
    return () => listeners.delete(listener);
  }

  return { enqueue, pendingCount, listPending, drain, subscribe };
}

export {
  AUTH_RETRY_STATUSES,
  MAX_RETRY_ATTEMPTS,
  QUICK_CAPTURE_PATH,
  UPLOAD_FILE_PATH,
  createIndexedDbStorage,
  createMemoryStorage,
  createUploadQueue,
};
