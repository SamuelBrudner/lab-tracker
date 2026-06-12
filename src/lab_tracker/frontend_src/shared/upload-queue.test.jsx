import { afterEach, describe, expect, it, vi } from "vitest";

import {
  MAX_RETRY_ATTEMPTS,
  QUICK_CAPTURE_PATH,
  UPLOAD_FILE_PATH,
  createIndexedDbStorage,
  createMemoryStorage,
  createUploadQueue,
} from "./upload-queue.js";

function makeFile(name = "snap.jpg", content = "image-bytes", type = "image/jpeg") {
  return new File([content], name, { type });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("createUploadQueue", () => {
  it("enqueues a multipart payload and reports pending count", async () => {
    const storage = createMemoryStorage();
    const queue = createUploadQueue({ storage, fetch: vi.fn() });

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
    const queue = createUploadQueue({ storage, fetch: fetchImpl });

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
    expect(body.get("metadata")).toBe(JSON.stringify({ source: "camera" }));
    expect(body.get("targets")).toBe(JSON.stringify([{ entity_type: "question", entity_id: "q-1" }]));
    expect(body.get("file")).toBeInstanceOf(File);
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

  it.each([401, 408, 429])("keeps retryable %i responses queued", async (status) => {
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
});
