import { afterEach, describe, expect, it, vi } from "vitest";
import { indexedDB as fakeIndexedDB } from "fake-indexeddb";

import {
  DB_NAME,
  STORE,
  createIndexedDbShareStorage,
  createMemoryShareStorage,
  migrateIncomingShares,
} from "./share-target-inbox.js";
import {
  UPLOAD_FILE_PATH,
  createMemoryStorage,
  createUploadQueue,
} from "./upload-queue.js";

function makeFile(name = "shared.jpg", content = "img", type = "image/jpeg") {
  return new File([content], name, { type });
}

function makeQueue() {
  return createUploadQueue({
    storage: createMemoryStorage(),
    fetch: vi.fn(),
  });
}

function deleteDatabase(name) {
  return new Promise((resolve, reject) => {
    const request = fakeIndexedDB.deleteDatabase(name);
    request.onsuccess = () => resolve();
    request.onerror = () => reject(request.error);
    request.onblocked = () => reject(new Error(`Timed out deleting ${name}`));
  });
}

function openSeedDb() {
  return new Promise((resolve, reject) => {
    const request = fakeIndexedDB.open(DB_NAME, 1);
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

function txDone(tx) {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error || new Error("IndexedDB transaction aborted."));
  });
}

async function seedShareInbox(shares) {
  vi.stubGlobal("indexedDB", fakeIndexedDB);
  await deleteDatabase(DB_NAME);
  const db = await openSeedDb();
  try {
    const tx = db.transaction(STORE, "readwrite");
    const store = tx.objectStore(STORE);
    for (const share of shares) {
      store.add(share);
    }
    await txDone(tx);
  } finally {
    db.close();
  }
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("migrateIncomingShares", () => {
  it("attaches project + token and hands each share to the upload queue", async () => {
    const storage = createMemoryShareStorage([
      {
        file: makeFile("first.jpg"),
        filename: "first.jpg",
        contentType: "image/jpeg",
        title: "From Camera",
        text: "",
        receivedAt: 1717_000_000_000,
      },
      {
        file: makeFile("second.jpg"),
        filename: "second.jpg",
        contentType: "image/jpeg",
        receivedAt: 1717_000_001_000,
      },
    ]);
    const uploadQueue = makeQueue();

    const result = await migrateIncomingShares({
      projectId: "proj-a",
      ownerId: "owner-1",
      uploadQueue,
      storage,
    });

    expect(result.migrated).toBe(2);
    expect(await storage.list()).toHaveLength(0);
    const queued = await uploadQueue.listPending();
    expect(queued).toHaveLength(2);
    expect(queued.every((item) => item.endpoint === UPLOAD_FILE_PATH)).toBe(true);
    expect(queued.every((item) => item.ownerId === "owner-1")).toBe(true);
    expect(queued.every((item) => item.fields.project_id === "proj-a")).toBe(true);
    const firstMetadata = JSON.parse(queued[0].fields.metadata);
    expect(firstMetadata.capture_source).toBe("share_target");
    expect(firstMetadata.share_title).toBe("From Camera");
  });

  it("leaves shares untouched when no project is available", async () => {
    const storage = createMemoryShareStorage([
      { file: makeFile(), receivedAt: 1 },
    ]);
    const uploadQueue = makeQueue();

    const result = await migrateIncomingShares({
      projectId: "",
      ownerId: "owner-1",
      uploadQueue,
      storage,
    });

    expect(result.migrated).toBe(0);
    expect(await storage.list()).toHaveLength(1);
    expect(await uploadQueue.pendingCount()).toBe(0);
  });

  it("returns zero when the inbox is empty", async () => {
    const storage = createMemoryShareStorage([]);
    const uploadQueue = makeQueue();

    const result = await migrateIncomingShares({
      projectId: "proj-a",
      ownerId: "owner-1",
      uploadQueue,
      storage,
    });

    expect(result.migrated).toBe(0);
    expect(await uploadQueue.pendingCount()).toBe(0);
  });

  it("drops empty shares that have no file rather than blocking the inbox", async () => {
    const storage = createMemoryShareStorage([{ receivedAt: 1 }]);
    const uploadQueue = makeQueue();

    const result = await migrateIncomingShares({
      projectId: "proj-a",
      ownerId: "owner-1",
      uploadQueue,
      storage,
    });

    expect(result).toEqual({ migrated: 0, skipped: 1 });
    expect(await storage.list()).toHaveLength(0);
  });

  it("creates text notes for fileless text and URL shares", async () => {
    const storage = createMemoryShareStorage([
      {
        title: "Paper link",
        text: "Follow up on this protocol",
        url: "https://example.test/protocol",
        receivedAt: 1,
      },
    ]);
    const uploadQueue = makeQueue();
    const createTextNote = vi.fn(async () => ({ note_id: "note-share" }));

    const result = await migrateIncomingShares({
      createTextNote,
      projectId: "proj-a",
      ownerId: "owner-1",
      uploadQueue,
      storage,
    });

    expect(result).toEqual({ migrated: 1, skipped: 0 });
    expect(await storage.list()).toHaveLength(0);
    expect(await uploadQueue.pendingCount()).toBe(0);
    expect(createTextNote).toHaveBeenCalledWith({
      metadata: {
        capture_source: "share_target",
        share_text: "Follow up on this protocol",
        share_title: "Paper link",
        share_url: "https://example.test/protocol",
        shared_at: "1970-01-01T00:00:00.001Z",
      },
      projectId: "proj-a",
      rawContent: "Paper link\n\nFollow up on this protocol\n\nhttps://example.test/protocol",
      share: expect.objectContaining({
        title: "Paper link",
        url: "https://example.test/protocol",
      }),
    });
  });

  it("leaves fileless text shares in the inbox if no note creator is available", async () => {
    const storage = createMemoryShareStorage([{ text: "bench note", receivedAt: 1 }]);
    const uploadQueue = makeQueue();

    const result = await migrateIncomingShares({
      projectId: "proj-a",
      ownerId: "owner-1",
      uploadQueue,
      storage,
    });

    expect(result).toEqual({ migrated: 0, skipped: 1 });
    expect(await storage.list()).toHaveLength(1);
    expect(await uploadQueue.pendingCount()).toBe(0);
  });

  it("waits for IndexedDB transaction completion when removing shares", async () => {
    let activeTx = null;
    const fakeDb = {
      close: vi.fn(),
      objectStoreNames: {
        contains: () => true,
      },
      transaction: vi.fn(() => {
        activeTx = {
          error: null,
          objectStore: () => ({
            delete: () => {
              const request = { result: undefined };
              queueMicrotask(() => request.onsuccess?.());
              return request;
            },
          }),
        };
        return activeTx;
      }),
    };
    vi.stubGlobal("indexedDB", {
      open: vi.fn(() => {
        const request = { result: fakeDb };
        queueMicrotask(() => request.onsuccess?.());
        return request;
      }),
    });

    const storage = createIndexedDbShareStorage();
    let settled = false;
    const removePromise = storage.remove(7).then(() => {
      settled = true;
    });

    for (let tick = 0; tick < 5 && typeof activeTx?.oncomplete !== "function"; tick += 1) {
      await Promise.resolve();
    }
    expect(settled).toBe(false);
    expect(typeof activeTx.oncomplete).toBe("function");

    activeTx.oncomplete();

    await expect(removePromise).resolves.toBeUndefined();
    expect(fakeDb.close).toHaveBeenCalled();
  });

  it("migrates and removes records through the IndexedDB share inbox adapter", async () => {
    await seedShareInbox([
      {
        file: makeFile("idb-share.jpg", "idb-img"),
        filename: "idb-share.jpg",
        contentType: "image/jpeg",
        title: "Shared from OS",
        text: "bench note",
        receivedAt: 1717_000_002_000,
      },
    ]);
    const storage = createIndexedDbShareStorage();
    const uploadQueue = makeQueue();

    const result = await migrateIncomingShares({
      projectId: "proj-a",
      ownerId: "owner-1",
      uploadQueue,
      storage,
    });

    expect(result).toEqual({ migrated: 1, skipped: 0 });
    expect(await storage.list()).toEqual([]);
    const queued = await uploadQueue.listPending();
    expect(queued).toHaveLength(1);
    expect(queued[0]).toMatchObject({
      endpoint: UPLOAD_FILE_PATH,
      filename: "idb-share.jpg",
      ownerId: "owner-1",
      fields: {
        project_id: "proj-a",
      },
    });
    expect(JSON.parse(queued[0].fields.metadata)).toMatchObject({
      capture_source: "share_target",
      share_title: "Shared from OS",
      share_text: "bench note",
    });
  });
});
