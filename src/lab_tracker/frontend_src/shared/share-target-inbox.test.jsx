import { afterEach, describe, expect, it, vi } from "vitest";

import {
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

afterEach(() => {
  vi.restoreAllMocks();
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
      token: "tok-1",
      uploadQueue,
      storage,
    });

    expect(result.migrated).toBe(2);
    expect(await storage.list()).toHaveLength(0);
    const queued = await uploadQueue.listPending();
    expect(queued).toHaveLength(2);
    expect(queued.every((item) => item.endpoint === UPLOAD_FILE_PATH)).toBe(true);
    expect(queued.every((item) => item.token === "tok-1")).toBe(true);
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
      token: "tok-1",
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
      token: "tok-1",
      uploadQueue,
      storage,
    });

    expect(result.migrated).toBe(0);
    expect(await uploadQueue.pendingCount()).toBe(0);
  });

  it("drops shares that have no file rather than blocking the inbox", async () => {
    const storage = createMemoryShareStorage([{ receivedAt: 1 }]);
    const uploadQueue = makeQueue();

    const result = await migrateIncomingShares({
      projectId: "proj-a",
      token: "tok-1",
      uploadQueue,
      storage,
    });

    expect(result.migrated).toBe(0);
    expect(await storage.list()).toHaveLength(0);
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
});
