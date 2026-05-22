import { afterEach, describe, expect, it, vi } from "vitest";

import {
  QUICK_CAPTURE_PATH,
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
  it("enqueues a quick-capture payload and reports pending count", async () => {
    const storage = createMemoryStorage();
    const queue = createUploadQueue({ storage, fetch: vi.fn() });

    await queue.enqueue({ projectId: "proj-a", file: makeFile() });
    await queue.enqueue({ projectId: "proj-a", file: makeFile("two.jpg") });

    expect(await queue.pendingCount()).toBe(2);
    const pending = await queue.listPending();
    expect(pending.map((item) => item.filename)).toEqual(["snap.jpg", "two.jpg"]);
  });

  it("requires both projectId and file", async () => {
    const queue = createUploadQueue({ storage: createMemoryStorage(), fetch: vi.fn() });
    await expect(queue.enqueue({ projectId: "", file: makeFile() })).rejects.toThrow(
      /projectId/
    );
    await expect(queue.enqueue({ projectId: "proj-a", file: null })).rejects.toThrow(
      /file/
    );
  });

  it("uploads queued items on drain and removes successful ones", async () => {
    const storage = createMemoryStorage();
    const fetchImpl = vi.fn(async () => ({ ok: true, status: 202 }));
    const queue = createUploadQueue({ storage, fetch: fetchImpl });

    await queue.enqueue({ projectId: "proj-a", file: makeFile(), token: "tok-1" });
    await queue.enqueue({ projectId: "proj-b", file: makeFile("b.jpg") });

    const result = await queue.drain();
    expect(result.uploaded).toHaveLength(2);
    expect(result.stillQueued).toHaveLength(0);
    expect(await queue.pendingCount()).toBe(0);

    expect(fetchImpl).toHaveBeenCalledTimes(2);
    const [path, init] = fetchImpl.mock.calls[0];
    expect(path).toBe(QUICK_CAPTURE_PATH);
    expect(init.method).toBe("POST");
    expect(init.headers.Authorization).toBe("Bearer tok-1");
    const body = init.body;
    expect(body.get("project_id")).toBe("proj-a");
    expect(body.get("file")).toBeInstanceOf(File);
  });

  it("keeps items queued when the network fails", async () => {
    const storage = createMemoryStorage();
    const fetchImpl = vi.fn(async () => {
      throw new Error("offline");
    });
    const queue = createUploadQueue({ storage, fetch: fetchImpl });

    await queue.enqueue({ projectId: "proj-a", file: makeFile() });
    const result = await queue.drain();
    expect(result.uploaded).toHaveLength(0);
    expect(result.stillQueued).toHaveLength(1);
    expect(await queue.pendingCount()).toBe(1);
  });

  it("drops items the server rejects with a 4xx response", async () => {
    const storage = createMemoryStorage();
    const fetchImpl = vi.fn(async () => ({ ok: false, status: 422 }));
    const queue = createUploadQueue({ storage, fetch: fetchImpl });

    await queue.enqueue({ projectId: "proj-a", file: makeFile() });
    const result = await queue.drain();
    expect(result.uploaded).toHaveLength(1);
    expect(result.uploaded[0].rejectedStatus).toBe(422);
    expect(await queue.pendingCount()).toBe(0);
  });

  it("retains items on 5xx so they can retry later", async () => {
    const storage = createMemoryStorage();
    const fetchImpl = vi.fn(async () => ({ ok: false, status: 503 }));
    const queue = createUploadQueue({ storage, fetch: fetchImpl });

    await queue.enqueue({ projectId: "proj-a", file: makeFile() });
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

    await queue.enqueue({ projectId: "proj-a", file: makeFile() });
    expect(listener).toHaveBeenCalledTimes(1);

    await queue.drain();
    expect(listener).toHaveBeenCalledTimes(2);

    unsubscribe();
    await queue.enqueue({ projectId: "proj-a", file: makeFile() });
    expect(listener).toHaveBeenCalledTimes(2);
  });
});
