/* Offline-aware upload queue for /notes/quick-capture.
 *
 * The queue stores failed (or offline-initiated) capture payloads in IndexedDB
 * and replays them when the network returns. Logic is split from storage so
 * the queue can be unit-tested against an in-memory adapter without pulling in
 * an IndexedDB shim.
 */

const DB_NAME = "lab-tracker-upload-queue";
const DB_VERSION = 1;
const STORE = "pending";

const QUICK_CAPTURE_PATH = "/notes/quick-capture";

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

function createIndexedDbStorage() {
  return {
    async add(record) {
      const db = await openIndexedDb();
      try {
        const tx = db.transaction(STORE, "readwrite");
        const store = tx.objectStore(STORE);
        const id = await runRequest(store.add(record));
        await runRequest(tx);
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
        await runRequest(tx);
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
  };
}

function createUploadQueue({ storage, fetch: fetchImpl = globalThis.fetch, now = () => Date.now() } = {}) {
  const adapter = storage || createIndexedDbStorage();
  const listeners = new Set();

  function notify() {
    listeners.forEach((listener) => {
      try {
        listener();
      } catch {
        // Listener errors should never break the queue itself.
      }
    });
  }

  async function enqueue({ projectId, file, token = "" }) {
    if (!projectId) {
      throw new Error("enqueue requires projectId");
    }
    if (!file) {
      throw new Error("enqueue requires a file");
    }
    const record = {
      projectId,
      file,
      filename: file.name || "capture",
      contentType: file.type || "application/octet-stream",
      token,
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

  async function drain() {
    const items = await adapter.list();
    const results = { uploaded: [], stillQueued: [] };
    for (const item of items) {
      const payload = new FormData();
      payload.append("project_id", item.projectId);
      payload.append("file", item.file, item.filename);
      const headers = item.token ? { Authorization: `Bearer ${item.token}` } : {};
      let response;
      try {
        response = await fetchImpl(QUICK_CAPTURE_PATH, {
          method: "POST",
          headers,
          body: payload,
        });
      } catch {
        results.stillQueued.push(item);
        continue;
      }
      if (response && response.ok) {
        await adapter.remove(item.id);
        results.uploaded.push(item);
      } else if (response && response.status >= 400 && response.status < 500) {
        // Client-side rejection: dropping the item is the right call —
        // retrying will not change the outcome. Surface for diagnostics.
        await adapter.remove(item.id);
        results.uploaded.push({ ...item, rejectedStatus: response.status });
      } else {
        results.stillQueued.push(item);
      }
    }
    notify();
    return results;
  }

  function subscribe(listener) {
    listeners.add(listener);
    return () => listeners.delete(listener);
  }

  return { enqueue, pendingCount, listPending, drain, subscribe };
}

export {
  QUICK_CAPTURE_PATH,
  createIndexedDbStorage,
  createMemoryStorage,
  createUploadQueue,
};
