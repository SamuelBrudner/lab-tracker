import { afterEach, describe, expect, it, vi } from "vitest";
import { indexedDB as fakeIndexedDB } from "fake-indexeddb";

import {
  DB_NAME,
  SHARE_INBOX_MAX_AGE_MS,
  STORE,
  createIndexedDbShareStorage,
  createMemoryShareStorage,
  discardIncomingShares,
  expiredSharesMessage,
  listReviewableShares,
  migrateIncomingShares,
  shareTooLargeMessage,
} from "./share-target-inbox.js";
import {
  UPLOAD_FILE_PATH,
  createMemoryStorage,
  createUploadQueue,
} from "./upload-queue.js";

function makeFile(name = "shared.jpg", content = "img", type = "image/jpeg") {
  return new File([content], name, { type });
}

// The ids the user saw in the review step and confirmed for import.
async function reviewedIds(storage) {
  return (await storage.list()).map((share) => share.id);
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
  it("imports only the shares the user reviewed, leaving later arrivals parked", async () => {
    const storage = createMemoryShareStorage([
      { text: "reviewed note", receivedAt: 1 },
      { text: "arrived after review", receivedAt: 2 },
    ]);
    const [reviewed] = await reviewedIds(storage);
    const createTextNote = vi.fn(async () => ({ note_id: "note-share" }));

    const result = await migrateIncomingShares({
      createTextNote,
      projectId: "proj-a",
      ownerId: "owner-1",
      uploadQueue: makeQueue(),
      storage,
      shareIds: [reviewed],
    });

    expect(result).toEqual({ migrated: 1, skipped: 0 });
    expect(createTextNote).toHaveBeenCalledTimes(1);
    expect(createTextNote).toHaveBeenCalledWith(
      expect.objectContaining({ rawContent: "reviewed note" })
    );
    expect(await storage.list()).toEqual([
      expect.objectContaining({ text: "arrived after review" }),
    ]);
  });

  it("refuses to import without an explicit list of reviewed shares", async () => {
    const storage = createMemoryShareStorage([{ text: "unreviewed", receivedAt: 1 }]);
    const createTextNote = vi.fn();

    await expect(
      migrateIncomingShares({
        createTextNote,
        projectId: "proj-a",
        uploadQueue: makeQueue(),
        storage,
      })
    ).rejects.toThrow(TypeError);
    expect(createTextNote).not.toHaveBeenCalled();
    expect(await storage.list()).toHaveLength(1);
  });

  it("refuses to queue a shared file without a known owner, leaving every share parked", async () => {
    // The upload queue quarantines ownerless records forever; a share queued
    // before the signed-in identity is known would never upload or resurface.
    const storage = createMemoryShareStorage([
      { text: "text share", receivedAt: 1 },
      { file: makeFile("photo.jpg"), filename: "photo.jpg", receivedAt: 2 },
    ]);
    const createTextNote = vi.fn(async () => ({ note_id: "n" }));
    const uploadQueue = makeQueue();

    await expect(
      migrateIncomingShares({
        createTextNote,
        projectId: "proj-a",
        ownerId: "",
        uploadQueue,
        storage,
        shareIds: await reviewedIds(storage),
      })
    ).rejects.toThrow(/signed-in account/);
    expect(createTextNote).not.toHaveBeenCalled();
    expect(await uploadQueue.pendingCount()).toBe(0);
    expect(await storage.list()).toHaveLength(2);
  });

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
      shareIds: await reviewedIds(storage),
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
      shareIds: await reviewedIds(storage),
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
      shareIds: await reviewedIds(storage),
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
      shareIds: await reviewedIds(storage),
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
      shareIds: await reviewedIds(storage),
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
      shareIds: await reviewedIds(storage),
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
      shareIds: await reviewedIds(storage),
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

describe("discardIncomingShares", () => {
  it("removes only the reviewed shares without importing anything", async () => {
    const storage = createMemoryShareStorage([
      { text: "unwanted", receivedAt: 1 },
      { text: "arrived after review", receivedAt: 2 },
    ]);
    const [reviewed] = await reviewedIds(storage);

    const result = await discardIncomingShares({ storage, shareIds: [reviewed] });

    expect(result).toEqual({ discarded: 1 });
    expect(await storage.list()).toEqual([
      expect.objectContaining({ text: "arrived after review" }),
    ]);
  });
});

describe("listReviewableShares", () => {
  it("drops shares past the maximum age, counts them, and lists the rest for review", async () => {
    const now = 1_800_000_000_000;
    const storage = createMemoryShareStorage([
      { text: "expired", receivedAt: now - SHARE_INBOX_MAX_AGE_MS - 1 },
      { text: "no timestamp" },
      { text: "fresh", receivedAt: now - SHARE_INBOX_MAX_AGE_MS + 1 },
    ]);

    const { expired, shares } = await listReviewableShares({ storage, now });

    expect(expired).toBe(2);
    expect(shares.map((share) => share.text)).toEqual(["fresh"]);
    expect((await storage.list()).map((share) => share.text)).toEqual(["fresh"]);
  });

  it("reports no expired shares when every parked share is fresh", async () => {
    const now = 1_800_000_000_000;
    const storage = createMemoryShareStorage([{ text: "fresh", receivedAt: now }]);

    expect(await listReviewableShares({ storage, now })).toEqual({
      expired: 0,
      shares: [expect.objectContaining({ text: "fresh" })],
    });
  });
});

describe("share inbox notices", () => {
  it("names how many unreviewed shares expired and why", () => {
    expect(expiredSharesMessage(1)).toBe(
      "1 shared item waited more than 7 days without review and was removed from the share inbox."
    );
    expect(expiredSharesMessage(3)).toBe(
      "3 shared items waited more than 7 days without review and were removed from the share inbox."
    );
  });

  it("states the per-share limits in the too-large notice", () => {
    expect(shareTooLargeMessage()).toBe(
      "The shared item was not saved: one share can hold at most 100 MB and 20 files. " +
        "Add it from the capture page instead."
    );
  });
});
