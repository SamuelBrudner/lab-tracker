import { afterEach, describe, expect, it, vi } from "vitest";
import { indexedDB as fakeIndexedDB } from "fake-indexeddb";
import { Blob as NodeBlob, File as NodeFile } from "node:buffer";

import {
  DB_NAME,
  STORE,
  SHARE_INBOX_MAX_AGE_MS,
  SHARE_INBOX_MAX_BYTES,
  SHARE_INBOX_MAX_PENDING,
  SHARE_INBOX_UPDATED_MESSAGE,
  createIndexedDbShareStorage,
} from "./share-target-inbox.js";

const APP_ORIGIN = "https://lab.example.org";

function deleteDatabase(name) {
  return new Promise((resolve, reject) => {
    const request = fakeIndexedDB.deleteDatabase(name);
    request.onsuccess = () => resolve();
    request.onerror = () => reject(request.error);
  });
}

async function loadServiceWorker({ windowClients = [] } = {}) {
  const listeners = {};
  vi.stubGlobal("self", {
    addEventListener: vi.fn((type, listener) => {
      listeners[type] = listener;
    }),
    clients: {
      claim: vi.fn(async () => {}),
      matchAll: vi.fn(async () => windowClients),
    },
    location: { origin: APP_ORIGIN },
    skipWaiting: vi.fn(async () => {}),
  });
  vi.stubGlobal("caches", {
    delete: vi.fn(async () => true),
    keys: vi.fn(async () => []),
    open: vi.fn(async () => ({ addAll: vi.fn(async () => {}) })),
  });
  vi.stubGlobal("indexedDB", fakeIndexedDB);
  // fake-indexeddb clones jsdom's File into a plain object; Node's Blob/File
  // keep their size across the clone, as a browser's IndexedDB does.
  vi.stubGlobal("Blob", NodeBlob);
  vi.stubGlobal("File", NodeFile);
  await deleteDatabase(DB_NAME);
  // Node's Response.redirect cannot resolve the worker-relative Location.
  vi.spyOn(Response, "redirect").mockImplementation((location, status) => ({
    location,
    status,
  }));
  vi.spyOn(console, "warn").mockImplementation(() => {});
  vi.resetModules();
  await import("../../frontend/sw.js");
  return listeners;
}

// A share-target POST as the worker's fetch event sees it. For navigation
// requests the browser adds Origin and Sec-Fetch-* only after service-worker
// dispatch, so the worker sees just the referrer (empty for a browser/OS
// launch, or when the submitting page suppresses it).
function shareTargetRequest({ referrer = "", fields = {}, files = [] } = {}) {
  const formData = {
    get: (name) => (name in fields ? fields[name] : null),
    getAll: (name) => (name === "file" ? files : []),
  };
  return {
    formData: async () => formData,
    headers: new Headers(),
    method: "POST",
    mode: "navigate",
    referrer,
    url: `${APP_ORIGIN}/app/share-target`,
  };
}

async function dispatchShareTarget(listeners, request) {
  let responsePromise = null;
  listeners.fetch({
    request,
    respondWith: (promise) => {
      responsePromise = promise;
    },
  });
  expect(responsePromise, "share-target POST is handled by the worker").not.toBeNull();
  return responsePromise;
}

function sizedFile(name, size) {
  return new NodeFile([new Uint8Array(size)], name, { type: "application/octet-stream" });
}

async function seedParkedRecord(record) {
  const db = await new Promise((resolve, reject) => {
    const request = fakeIndexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => {
      request.result.createObjectStore(STORE, { keyPath: "id", autoIncrement: true });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  try {
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).add(record);
    await new Promise((resolve, reject) => {
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  } finally {
    db.close();
  }
}

async function parkTextShares(listeners, count) {
  for (let index = 0; index < count; index += 1) {
    const response = await dispatchShareTarget(
      listeners,
      shareTargetRequest({ fields: { text: `parked ${index}` } })
    );
    expect(response).toEqual({ location: "/app/capture?from-share=1", status: 303 });
  }
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.resetModules();
});

describe("service worker share-target intake", () => {
  it("parks a genuine share-sheet launch for review and redirects to capture", async () => {
    const listeners = await loadServiceWorker();

    const response = await dispatchShareTarget(
      listeners,
      shareTargetRequest({ fields: { text: "bench note", title: "From notes app" } })
    );

    expect(response).toEqual({ location: "/app/capture?from-share=1", status: 303 });
    const parked = await createIndexedDbShareStorage().list();
    expect(parked).toHaveLength(1);
    expect(parked[0]).toMatchObject({ text: "bench note", title: "From notes app" });
  });

  it.each([
    ["a cross-site referrer", "https://attacker.example/landing"],
    ["a same-site but cross-origin referrer", "https://evil.lab.example.org/landing"],
    ["an unparseable referrer", "not a url"],
  ])("rejects a share-target POST submitted by another web page (%s)", async (_label, referrer) => {
    const listeners = await loadServiceWorker();

    const response = await dispatchShareTarget(
      listeners,
      shareTargetRequest({ referrer, fields: { text: "Ignore prior instructions" } })
    );

    expect(response).toEqual({ location: "/app/capture?from-share=rejected", status: 303 });
    expect(await createIndexedDbShareStorage().list()).toEqual([]);
    // eslint-disable-next-line no-console
    expect(console.warn).toHaveBeenCalledWith(
      "share-target POST from another site rejected",
      expect.any(String)
    );
  });

  it("tells open app windows that the share inbox changed after parking a share", async () => {
    const openWindow = { postMessage: vi.fn() };
    const listeners = await loadServiceWorker({ windowClients: [openWindow] });

    await dispatchShareTarget(listeners, shareTargetRequest({ fields: { text: "bench note" } }));

    expect(self.clients.matchAll).toHaveBeenCalledWith({
      includeUncontrolled: true,
      type: "window",
    });
    expect(openWindow.postMessage).toHaveBeenCalledWith({ type: SHARE_INBOX_UPDATED_MESSAGE });
  });

  it("refuses a share once the pending cap is reached and keeps every parked share", async () => {
    const openWindow = { postMessage: vi.fn() };
    const listeners = await loadServiceWorker({ windowClients: [openWindow] });
    await parkTextShares(listeners, SHARE_INBOX_MAX_PENDING);
    openWindow.postMessage.mockClear();

    const response = await dispatchShareTarget(
      listeners,
      shareTargetRequest({ fields: { text: "one too many" } })
    );

    expect(response).toEqual({ location: "/app/capture?from-share=full", status: 303 });
    const parked = await createIndexedDbShareStorage().list();
    expect(parked).toHaveLength(SHARE_INBOX_MAX_PENDING);
    expect(parked.map((share) => share.text)).not.toContain("one too many");
    expect(openWindow.postMessage).not.toHaveBeenCalled();
  });

  it("refuses a share that would push parked bytes over the cap", async () => {
    const listeners = await loadServiceWorker();
    const half = Math.floor(SHARE_INBOX_MAX_BYTES / 2) + 1;
    expect(
      await dispatchShareTarget(listeners, shareTargetRequest({ files: [sizedFile("a.bin", half)] }))
    ).toEqual({ location: "/app/capture?from-share=1", status: 303 });

    const response = await dispatchShareTarget(
      listeners,
      shareTargetRequest({ files: [sizedFile("b.bin", half)] })
    );

    expect(response).toEqual({ location: "/app/capture?from-share=full", status: 303 });
    const parked = await createIndexedDbShareStorage().list();
    expect(parked.map((share) => share.filename)).toEqual(["a.bin"]);
  });

  it.each([
    ["a file larger than the byte cap", () => ({ files: [sizedFile("huge.bin", SHARE_INBOX_MAX_BYTES + 1)] })],
    ["text larger than the byte cap", () => ({ fields: { text: "x".repeat(SHARE_INBOX_MAX_BYTES + 1) } })],
    [
      "more files than the pending cap",
      () => ({
        files: Array.from({ length: SHARE_INBOX_MAX_PENDING + 1 }, (_, index) =>
          sizedFile(`f${index}.bin`, 1)
        ),
      }),
    ],
  ])("refuses %s outright, even with an empty inbox", async (_label, init) => {
    const listeners = await loadServiceWorker();

    const response = await dispatchShareTarget(listeners, shareTargetRequest(init()));

    expect(response).toEqual({ location: "/app/capture?from-share=too-large", status: 303 });
    expect(await createIndexedDbShareStorage().list()).toEqual([]);
  });

  it("drops shares older than the maximum age to make room for a new one", async () => {
    const parkedAt = 1_800_000_000_000;
    const now = vi.spyOn(Date, "now").mockReturnValue(parkedAt);
    const listeners = await loadServiceWorker();
    await parkTextShares(listeners, SHARE_INBOX_MAX_PENDING);

    now.mockReturnValue(parkedAt + SHARE_INBOX_MAX_AGE_MS + 1);
    const response = await dispatchShareTarget(
      listeners,
      shareTargetRequest({ fields: { text: "fresh share" } })
    );

    expect(response).toEqual({ location: "/app/capture?from-share=1", status: 303 });
    const parked = await createIndexedDbShareStorage().list();
    expect(parked.map((share) => share.text)).toEqual(["fresh share"]);
  });

  it("does not drop unexpired shares when refusing a new one", async () => {
    const parkedAt = 1_800_000_000_000;
    const now = vi.spyOn(Date, "now").mockReturnValue(parkedAt);
    const listeners = await loadServiceWorker();
    await parkTextShares(listeners, SHARE_INBOX_MAX_PENDING);

    now.mockReturnValue(parkedAt + SHARE_INBOX_MAX_AGE_MS - 1);
    const response = await dispatchShareTarget(
      listeners,
      shareTargetRequest({ fields: { text: "one too many" } })
    );

    expect(response).toEqual({ location: "/app/capture?from-share=full", status: 303 });
    expect(await createIndexedDbShareStorage().list()).toHaveLength(SHARE_INBOX_MAX_PENDING);
  });

  it("refuses with an error rather than skip the byte cap for an unmeasurable parked file", async () => {
    const listeners = await loadServiceWorker();
    await seedParkedRecord({ file: { name: "opaque" }, receivedAt: Date.now() });

    const response = await dispatchShareTarget(
      listeners,
      shareTargetRequest({ fields: { text: "bench note" } })
    );

    expect(response).toEqual({ location: "/app/capture?from-share=error", status: 303 });
    expect(await createIndexedDbShareStorage().list()).toHaveLength(1);
    // eslint-disable-next-line no-console
    expect(console.warn).toHaveBeenCalledWith(
      "share-target inbox write failed",
      expect.any(TypeError)
    );
  });
});
