import { afterEach, describe, expect, it, vi } from "vitest";
import { indexedDB as fakeIndexedDB } from "fake-indexeddb";

import {
  MAX_RETRY_ATTEMPTS,
  QUICK_CAPTURE_PATH,
  UPLOAD_FILE_PATH,
  createIndexedDbStorage,
  createMemoryStorage,
  createUploadQueue,
} from "./upload-queue.js";

const UPLOAD_QUEUE_DB_NAME = "lab-tracker-upload-queue";

function makeFile(name = "snap.jpg", content = "image-bytes", type = "image/jpeg") {
  return new File([content], name, { type });
}

function deleteDatabase(name) {
  return new Promise((resolve, reject) => {
    const request = fakeIndexedDB.deleteDatabase(name);
    request.onsuccess = () => resolve();
    request.onerror = () => reject(request.error);
    request.onblocked = () => reject(new Error(`Timed out deleting ${name}`));
  });
}

async function useFakeIndexedDb(name) {
  vi.stubGlobal("indexedDB", fakeIndexedDB);
  await deleteDatabase(name);
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("createUploadQueue", () => {
  it("enqueues a multipart payload and reports pending count", async () => {
    const storage = createMemoryStorage();
    let nextCaptureId = 1;
    const queue = createUploadQueue({
      storage,
      fetch: vi.fn(),
      createClientCaptureId: () => `capture-${nextCaptureId++}`,
    });

    await queue.enqueue({
      endpoint: UPLOAD_FILE_PATH,
      file: makeFile(),
      fields: { project_id: "proj-a" },
    });
    await queue.enqueue({
      endpoint: UPLOAD_FILE_PATH,
      file: makeFile("two.jpg"),
      fields: { project_id: "proj-a" },
    });

    expect(await queue.pendingCount()).toBe(2);
    const pending = await queue.listPending();
    expect(pending.map((item) => item.filename)).toEqual(["snap.jpg", "two.jpg"]);
    expect(pending.map((item) => item.fields.client_capture_id)).toEqual([
      "capture-1",
      "capture-2",
    ]);
  });

  it("requires endpoint, file, and project_id field", async () => {
    const queue = createUploadQueue({ storage: createMemoryStorage(), fetch: vi.fn() });
    await expect(
      queue.enqueue({ endpoint: "", file: makeFile(), fields: { project_id: "p" } })
    ).rejects.toThrow(/endpoint/);
    await expect(
      queue.enqueue({ endpoint: UPLOAD_FILE_PATH, file: null, fields: { project_id: "p" } })
    ).rejects.toThrow(/file/);
    await expect(
      queue.enqueue({ endpoint: UPLOAD_FILE_PATH, file: makeFile(), fields: {} })
    ).rejects.toThrow(/project_id/);
  });

  it("uploads queued items on drain and forwards all fields", async () => {
    const storage = createMemoryStorage();
    const fetchImpl = vi.fn(async () => ({ ok: true, status: 201 }));
    const queue = createUploadQueue({
      storage,
      fetch: fetchImpl,
      createClientCaptureId: () => "capture-forward",
    });

    await queue.enqueue({
      endpoint: UPLOAD_FILE_PATH,
      file: makeFile(),
      fields: {
        project_id: "proj-a",
        metadata: JSON.stringify({ source: "camera" }),
        targets: JSON.stringify([{ entity_type: "question", entity_id: "q-1" }]),
      },
      token: "tok-1",
    });

    const result = await queue.drain();
    expect(result.uploaded).toHaveLength(1);
    expect(result.stillQueued).toHaveLength(0);
    expect(await queue.pendingCount()).toBe(0);

    const [path, init] = fetchImpl.mock.calls[0];
    expect(path).toBe(UPLOAD_FILE_PATH);
    expect(init.method).toBe("POST");
    expect(init.headers.Authorization).toBe("Bearer tok-1");
    const body = init.body;
    expect(body.get("project_id")).toBe("proj-a");
    expect(body.get("client_capture_id")).toBe("capture-forward");
    expect(body.get("metadata")).toBe(JSON.stringify({ source: "camera" }));
    expect(body.get("targets")).toBe(JSON.stringify([{ entity_type: "question", entity_id: "q-1" }]));
    expect(body.get("file")).toBeInstanceOf(File);
  });

  it("persists a generated client capture id for legacy queued records before retry", async () => {
    const storage = createMemoryStorage();
    const fetchImpl = vi
      .fn()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce({ ok: true, status: 201 });
    const queue = createUploadQueue({
      storage,
      fetch: fetchImpl,
      createClientCaptureId: () => "legacy-capture-id",
    });
    await storage.add({
      endpoint: UPLOAD_FILE_PATH,
      file: makeFile(),
      fields: { project_id: "proj-a" },
      filename: "snap.jpg",
      contentType: "image/jpeg",
      token: "tok-1",
      enqueuedAt: 123,
    });

    const failed = await queue.drain();
    expect(failed.stillQueued).toHaveLength(1);
    expect(failed.stillQueued[0].fields.client_capture_id).toBe("legacy-capture-id");
    expect((await queue.listPending())[0].fields.client_capture_id).toBe(
      "legacy-capture-id"
    );

    await queue.drain();
    const secondBody = fetchImpl.mock.calls[1][1].body;
    expect(secondBody.get("client_capture_id")).toBe("legacy-capture-id");
    expect(await queue.pendingCount()).toBe(0);
  });

  it("routes each item to its own endpoint", async () => {
    const storage = createMemoryStorage();
    const fetchImpl = vi.fn(async () => ({ ok: true, status: 202 }));
    const queue = createUploadQueue({ storage, fetch: fetchImpl });

    await queue.enqueue({
      endpoint: UPLOAD_FILE_PATH,
      file: makeFile("full.jpg"),
      fields: { project_id: "proj-a" },
    });
    await queue.enqueue({
      endpoint: QUICK_CAPTURE_PATH,
      file: makeFile("share.jpg"),
      fields: { project_id: "proj-b" },
    });

    await queue.drain();
    const paths = fetchImpl.mock.calls.map(([url]) => url);
    expect(paths).toEqual([UPLOAD_FILE_PATH, QUICK_CAPTURE_PATH]);
  });

  it("keeps items queued when the network fails", async () => {
    const storage = createMemoryStorage();
    const fetchImpl = vi.fn(async () => {
      throw new Error("offline");
    });
    const queue = createUploadQueue({ storage, fetch: fetchImpl });

    await queue.enqueue({
      endpoint: UPLOAD_FILE_PATH,
      file: makeFile(),
      fields: { project_id: "proj-a" },
    });
    const result = await queue.drain();
    expect(result.uploaded).toHaveLength(0);
    expect(result.stillQueued).toHaveLength(1);
    expect(await queue.pendingCount()).toBe(1);
  });

  it("drops items the server rejects with a permanent 4xx response", async () => {
    const storage = createMemoryStorage();
    const fetchImpl = vi.fn(async () => ({ ok: false, status: 422 }));
    const queue = createUploadQueue({ storage, fetch: fetchImpl });

    await queue.enqueue({
      endpoint: UPLOAD_FILE_PATH,
      file: makeFile(),
      fields: { project_id: "proj-a" },
    });
    const result = await queue.drain();
    expect(result.dropped).toHaveLength(1);
    expect(result.dropped[0].rejectedStatus).toBe(422);
    expect(await queue.pendingCount()).toBe(0);
  });

  it.each([401, 403])("keeps auth %i responses queued without burning retries", async (status) => {
    const storage = createMemoryStorage();
    const fetchImpl = vi.fn(async () => ({ ok: false, status }));
    const queue = createUploadQueue({ storage, fetch: fetchImpl, now: () => 1234 });

    await queue.enqueue({
      endpoint: UPLOAD_FILE_PATH,
      file: makeFile(),
      fields: { project_id: "proj-a" },
    });
    const result = await queue.drain();
    expect(result.dropped).toHaveLength(0);
    expect(result.stillQueued).toHaveLength(1);
    expect(result.stillQueued[0].lastStatus).toBe(status);
    expect(result.stillQueued[0].retryCount).toBeUndefined();
    expect(await queue.pendingCount()).toBe(1);
  });

  it.each([408, 429])("keeps retryable %i responses queued with a retry cap", async (status) => {
    const storage = createMemoryStorage();
    const fetchImpl = vi.fn(async () => ({ ok: false, status }));
    const queue = createUploadQueue({ storage, fetch: fetchImpl, now: () => 1234 });

    await queue.enqueue({
      endpoint: UPLOAD_FILE_PATH,
      file: makeFile(),
      fields: { project_id: "proj-a" },
    });
    const result = await queue.drain();
    expect(result.dropped).toHaveLength(0);
    expect(result.stillQueued).toHaveLength(1);
    expect(result.stillQueued[0].lastStatus).toBe(status);
    expect(result.stillQueued[0].retryCount).toBe(1);
    expect(await queue.pendingCount()).toBe(1);
  });

  it("uses the live drain token instead of the enqueue-time token", async () => {
    const storage = createMemoryStorage();
    const fetchImpl = vi.fn(async () => ({ ok: true, status: 201 }));
    const queue = createUploadQueue({ storage, fetch: fetchImpl });

    await queue.enqueue({
      endpoint: UPLOAD_FILE_PATH,
      file: makeFile(),
      fields: { project_id: "proj-a" },
      token: "expired-token",
    });

    await queue.drain({ token: "fresh-token" });

    expect(fetchImpl.mock.calls[0][1].headers.Authorization).toBe("Bearer fresh-token");
  });

  it("does not drop auth failures after repeated drains", async () => {
    const storage = createMemoryStorage();
    const fetchImpl = vi.fn(async () => ({ ok: false, status: 401 }));
    const queue = createUploadQueue({ storage, fetch: fetchImpl });

    await queue.enqueue({
      endpoint: UPLOAD_FILE_PATH,
      file: makeFile(),
      fields: { project_id: "proj-a" },
    });

    let result = null;
    for (let attempt = 0; attempt < MAX_RETRY_ATTEMPTS + 2; attempt += 1) {
      result = await queue.drain();
    }

    expect(result.dropped).toHaveLength(0);
    expect(result.stillQueued).toHaveLength(1);
    expect(await queue.pendingCount()).toBe(1);
  });

  it("drops retryable rejections only after the retry cap", async () => {
    const storage = createMemoryStorage();
    const fetchImpl = vi.fn(async () => ({ ok: false, status: 429 }));
    const queue = createUploadQueue({ storage, fetch: fetchImpl });

    await queue.enqueue({
      endpoint: UPLOAD_FILE_PATH,
      file: makeFile(),
      fields: { project_id: "proj-a" },
    });

    let result = null;
    for (let attempt = 0; attempt < MAX_RETRY_ATTEMPTS; attempt += 1) {
      result = await queue.drain();
    }

    expect(result.dropped).toHaveLength(1);
    expect(result.dropped[0].dropReason).toBe("retry_limit");
    expect(result.dropped[0].retryCount).toBe(MAX_RETRY_ATTEMPTS);
    expect(await queue.pendingCount()).toBe(0);
  });

  it("retains items on 5xx so they can retry later", async () => {
    const storage = createMemoryStorage();
    const fetchImpl = vi.fn(async () => ({ ok: false, status: 503 }));
    const queue = createUploadQueue({ storage, fetch: fetchImpl });

    await queue.enqueue({
      endpoint: UPLOAD_FILE_PATH,
      file: makeFile(),
      fields: { project_id: "proj-a" },
    });
    const result = await queue.drain();
    expect(result.stillQueued).toHaveLength(1);
    expect(await queue.pendingCount()).toBe(1);
  });

  it("notifies subscribers on enqueue and drain", async () => {
    const storage = createMemoryStorage();
    const fetchImpl = vi.fn(async () => ({ ok: true, status: 202 }));
    const queue = createUploadQueue({ storage, fetch: fetchImpl });

    const listener = vi.fn();
    const unsubscribe = queue.subscribe(listener);

    await queue.enqueue({
      endpoint: UPLOAD_FILE_PATH,
      file: makeFile(),
      fields: { project_id: "proj-a" },
    });
    expect(listener).toHaveBeenCalledTimes(1);

    await queue.drain();
    expect(listener).toHaveBeenCalledTimes(2);

    unsubscribe();
    await queue.enqueue({
      endpoint: UPLOAD_FILE_PATH,
      file: makeFile(),
      fields: { project_id: "proj-a" },
    });
    expect(listener).toHaveBeenCalledTimes(2);
  });

  it("serializes overlapping drains", async () => {
    const storage = createMemoryStorage();
    let resolveFetch = null;
    const fetchImpl = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveFetch = resolve;
        })
    );
    const queue = createUploadQueue({ storage, fetch: fetchImpl });

    await queue.enqueue({
      endpoint: UPLOAD_FILE_PATH,
      file: makeFile(),
      fields: { project_id: "proj-a" },
    });

    const firstDrain = queue.drain();
    const secondDrain = queue.drain();
    for (let tick = 0; tick < 5 && fetchImpl.mock.calls.length === 0; tick += 1) {
      await Promise.resolve();
    }
    expect(fetchImpl).toHaveBeenCalledTimes(1);

    resolveFetch({ ok: true, status: 201 });
    const [firstResult, secondResult] = await Promise.all([firstDrain, secondDrain]);

    expect(firstResult.uploaded).toHaveLength(1);
    expect(secondResult.uploaded).toHaveLength(1);
    expect(await queue.pendingCount()).toBe(0);
  });

  it("waits for IndexedDB transaction completion when adding records", async () => {
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
            add: () => {
              const request = { result: 42 };
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

    const storage = createIndexedDbStorage();
    let settled = false;
    const addPromise = storage
      .add({
        endpoint: UPLOAD_FILE_PATH,
        fields: { project_id: "proj-a" },
        file: makeFile(),
      })
      .then((id) => {
        settled = true;
        return id;
      });

    for (let tick = 0; tick < 5 && typeof activeTx?.oncomplete !== "function"; tick += 1) {
      await Promise.resolve();
    }
    expect(settled).toBe(false);
    expect(typeof activeTx.oncomplete).toBe("function");

    activeTx.oncomplete();

    await expect(addPromise).resolves.toBe(42);
    expect(fakeDb.close).toHaveBeenCalled();
  });

  it("stores, updates, lists, and removes records through the IndexedDB adapter", async () => {
    await useFakeIndexedDb(UPLOAD_QUEUE_DB_NAME);
    const storage = createIndexedDbStorage();
    const file = makeFile("real-idb.jpg", "real bytes");

    const id = await storage.add({
      endpoint: UPLOAD_FILE_PATH,
      fields: { project_id: "proj-a", note: "from-real-idb" },
      file,
      filename: file.name,
      contentType: file.type,
      token: "tok-1",
    });

    expect(id).toBe(1);
    expect(await storage.list()).toMatchObject([
      {
        id,
        endpoint: UPLOAD_FILE_PATH,
        fields: { project_id: "proj-a", note: "from-real-idb" },
        filename: "real-idb.jpg",
        contentType: "image/jpeg",
        token: "tok-1",
      },
    ]);

    const updated = await storage.update(id, {
      retryCount: 2,
      lastStatus: 429,
    });

    expect(updated).toMatchObject({
      id,
      retryCount: 2,
      lastStatus: 429,
    });
    expect(await storage.list()).toHaveLength(1);

    await storage.remove(id);
    expect(await storage.list()).toEqual([]);
  });
});
