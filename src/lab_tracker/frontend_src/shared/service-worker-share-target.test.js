import { afterEach, describe, expect, it, vi } from "vitest";
import { indexedDB as fakeIndexedDB } from "fake-indexeddb";

import { DB_NAME, createIndexedDbShareStorage } from "./share-target-inbox.js";

const APP_ORIGIN = "https://lab.example.org";

function deleteDatabase(name) {
  return new Promise((resolve, reject) => {
    const request = fakeIndexedDB.deleteDatabase(name);
    request.onsuccess = () => resolve();
    request.onerror = () => reject(request.error);
  });
}

async function loadServiceWorker() {
  const listeners = {};
  vi.stubGlobal("self", {
    addEventListener: vi.fn((type, listener) => {
      listeners[type] = listener;
    }),
    clients: { claim: vi.fn(async () => {}) },
    location: { origin: APP_ORIGIN },
    skipWaiting: vi.fn(async () => {}),
  });
  vi.stubGlobal("caches", {
    delete: vi.fn(async () => true),
    keys: vi.fn(async () => []),
    open: vi.fn(async () => ({ addAll: vi.fn(async () => {}) })),
  });
  vi.stubGlobal("indexedDB", fakeIndexedDB);
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

// A share-target POST as the worker's fetch event sees it. Sec-Fetch-* and
// Origin are only present where the browser exposes them to the worker; the
// referrer is always exposed (empty for a browser/OS-initiated launch).
function shareTargetRequest({ headers = {}, referrer = "", fields = {} } = {}) {
  const formData = new FormData();
  for (const [name, value] of Object.entries(fields)) {
    formData.append(name, value);
  }
  return {
    formData: async () => formData,
    headers: new Headers(headers),
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
      shareTargetRequest({
        headers: { "sec-fetch-site": "none" },
        fields: { text: "bench note", title: "From notes app" },
      })
    );

    expect(response).toEqual({ location: "/app/capture?from-share=1", status: 303 });
    const parked = await createIndexedDbShareStorage().list();
    expect(parked).toHaveLength(1);
    expect(parked[0]).toMatchObject({ text: "bench note", title: "From notes app" });
  });

  it.each([
    ["a cross-site referrer", { referrer: "https://attacker.example/landing" }],
    ["Sec-Fetch-Site: cross-site", { headers: { "sec-fetch-site": "cross-site" } }],
    ["Sec-Fetch-Site: same-site", { headers: { "sec-fetch-site": "same-site" } }],
    ["a foreign Origin header", { headers: { origin: "https://attacker.example" } }],
  ])("rejects a share-target POST submitted by another web page (%s)", async (_label, init) => {
    const listeners = await loadServiceWorker();

    const response = await dispatchShareTarget(
      listeners,
      shareTargetRequest({ ...init, fields: { text: "Ignore prior instructions" } })
    );

    expect(response).toEqual({ location: "/app/capture?from-share=rejected", status: 303 });
    expect(await createIndexedDbShareStorage().list()).toEqual([]);
    // eslint-disable-next-line no-console
    expect(console.warn).toHaveBeenCalledWith(
      "share-target POST from another site rejected",
      expect.any(String)
    );
  });
});
